"""Unit tests for the DeepSeek provider profile's thinking-mode wiring.

Modern DeepSeek models expect every request to carry an explicit
``extra_body.thinking`` parameter. Omitting it makes the server default to
thinking-mode ON, which then enforces the ``reasoning_content`` echo contract
on subsequent turns. These tests pin the provider wire shape without going
live and cover V4.1 Flash's canonical ``deepseek-flash`` id.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def deepseek_profile():
    """Resolve the registered DeepSeek profile through normal plugin discovery."""
    import model_tools  # noqa: F401
    import providers

    profile = providers.get_provider_profile("deepseek")
    assert profile is not None, "deepseek provider profile must be registered"
    return profile


class TestDeepSeekThinkingWireShape:
    """``build_api_kwargs_extras`` produces DeepSeek's exact wire format."""

    def test_v41_flash_default_enables_thinking_without_effort(self, deepseek_profile):
        extra_body, top_level = deepseek_profile.build_api_kwargs_extras(
            reasoning_config=None, model="deepseek-flash"
        )
        assert extra_body == {"thinking": {"type": "enabled"}}
        assert top_level == {}

    @pytest.mark.parametrize("effort", ["low", "medium", "high"])
    def test_standard_efforts_pass_through(self, deepseek_profile, effort):
        _, top_level = deepseek_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": effort},
            model="deepseek-flash",
        )
        assert top_level == {"reasoning_effort": effort}

    @pytest.mark.parametrize("effort", ["xhigh", "max", "MAX", "  Max  "])
    def test_xhigh_and_max_normalize_to_max(self, deepseek_profile, effort):
        _, top_level = deepseek_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": effort},
            model="deepseek-flash",
        )
        assert top_level == {"reasoning_effort": "max"}

    def test_explicitly_disabled_sends_disabled_marker(self, deepseek_profile):
        extra_body, top_level = deepseek_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False}, model="deepseek-flash"
        )
        assert extra_body == {"thinking": {"type": "disabled"}}
        assert top_level == {}

    def test_disabled_ignores_effort_field(self, deepseek_profile):
        _, top_level = deepseek_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False, "effort": "high"},
            model="deepseek-flash",
        )
        assert top_level == {}

    def test_unknown_effort_omits_top_level(self, deepseek_profile):
        _, top_level = deepseek_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "garbage"},
            model="deepseek-flash",
        )
        assert top_level == {}

    def test_empty_effort_omits_top_level(self, deepseek_profile):
        _, top_level = deepseek_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": ""},
            model="deepseek-flash",
        )
        assert top_level == {}


class TestDeepSeekModelGating:
    """V4.1/V4+ families get thinking; V3 and unknown models stay untouched."""

    @pytest.mark.parametrize(
        "model",
        [
            "deepseek-flash",
            "DEEPSEEK-FLASH",
            "deepseek-v4-pro",
            "deepseek-v4-flash",
            "deepseek-v4-future-variant",
            "DEEPSEEK-V4-PRO",
        ],
    )
    def test_thinking_capable_models_emit_thinking(self, deepseek_profile, model):
        extra_body, _ = deepseek_profile.build_api_kwargs_extras(
            reasoning_config=None, model=model
        )
        assert extra_body == {"thinking": {"type": "enabled"}}

    @pytest.mark.parametrize(
        "model",
        [
            "deepseek-v3-0324",
            "deepseek-v3.1",
            "",
            None,
            "deepseek-unknown",
        ],
    )
    def test_non_thinking_models_emit_nothing(self, deepseek_profile, model):
        extra_body, top_level = deepseek_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "high"}, model=model
        )
        assert extra_body == {}
        assert top_level == {}


class TestDeepSeekFullKwargsIntegration:
    """End-to-end transport kwargs for canonical V4.1 Flash."""

    def test_full_kwargs_match_v41_flash_wire_shape(self, deepseek_profile):
        from agent.transports.chat_completions import ChatCompletionsTransport

        kwargs = ChatCompletionsTransport().build_kwargs(
            model="deepseek-flash",
            messages=[{"role": "user", "content": "ping"}],
            tools=None,
            provider_profile=deepseek_profile,
            reasoning_config={"enabled": True, "effort": "max"},
            base_url="https://api.deepseek.com/v1",
            provider_name="deepseek",
        )
        assert kwargs["model"] == "deepseek-flash"
        assert kwargs["reasoning_effort"] == "max"
        assert kwargs["extra_body"] == {"thinking": {"type": "enabled"}}

    def test_v3_full_kwargs_omit_thinking(self, deepseek_profile):
        from agent.transports.chat_completions import ChatCompletionsTransport

        kwargs = ChatCompletionsTransport().build_kwargs(
            model="deepseek-v3-0324",
            messages=[{"role": "user", "content": "ping"}],
            tools=None,
            provider_profile=deepseek_profile,
            reasoning_config={"enabled": True, "effort": "high"},
            base_url="https://api.deepseek.com/v1",
            provider_name="deepseek",
        )
        assert "reasoning_effort" not in kwargs
        assert "extra_body" not in kwargs or "thinking" not in kwargs.get("extra_body", {})


class TestDeepSeekAuxModel:
    """Auxiliary and fallback defaults follow the canonical V4.1 Flash id."""

    def test_profile_advertises_deepseek_flash(self, deepseek_profile):
        assert deepseek_profile.default_aux_model == "deepseek-flash"

    def test_fallback_models_use_current_canonical(self, deepseek_profile):
        assert deepseek_profile.fallback_models == ("deepseek-flash",)

    def test_consumer_api_returns_deepseek_flash(self):
        from agent.auxiliary_client import _get_aux_model_for_provider
        assert _get_aux_model_for_provider("deepseek") == "deepseek-flash"
