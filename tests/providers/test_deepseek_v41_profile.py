"""DeepSeek V4.1 Flash compatibility contracts."""

from hermes_cli.model_normalize import normalize_model_for_provider
from providers import get_provider_profile


def test_deepseek_flash_is_preserved_for_direct_provider():
    assert normalize_model_for_provider("deepseek-flash", "deepseek") == "deepseek-flash"


def test_deepseek_flash_thinking_max_is_forwarded():
    profile = get_provider_profile("deepseek")
    extra_body, top_level = profile.build_api_kwargs_extras(
        model="deepseek-flash",
        reasoning_config={"enabled": True, "effort": "max"},
    )
    assert extra_body == {"thinking": {"type": "enabled"}}
    assert top_level == {"reasoning_effort": "max"}


def test_deepseek_flash_can_disable_thinking_explicitly():
    profile = get_provider_profile("deepseek")
    extra_body, top_level = profile.build_api_kwargs_extras(
        model="deepseek-flash",
        reasoning_config={"enabled": False, "effort": "max"},
    )
    assert extra_body == {"thinking": {"type": "disabled"}}
    assert top_level == {}


def test_deepseek_profile_prefers_current_flash_model():
    profile = get_provider_profile("deepseek")
    assert profile.default_aux_model == "deepseek-flash"
    assert profile.fallback_models[0] == "deepseek-flash"


def test_deepseek_offline_catalog_prefers_current_flash_model():
    from hermes_cli.models_catalog_static import _PROVIDER_MODELS

    assert _PROVIDER_MODELS["deepseek"][0] == "deepseek-flash"
