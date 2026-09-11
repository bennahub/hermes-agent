"""Tests for hermes_cli.model_normalize — provider-aware model name normalization.

Covers issue #5211: opencode-go model names with dots (e.g. minimax-m2.7)
must NOT be mangled to hyphens (minimax-m2-7).
"""
import pytest

from hermes_cli.model_normalize import (
    normalize_model_for_provider,
    _DOT_TO_HYPHEN_PROVIDERS,
    _normalize_for_deepseek,
    detect_vendor,
)


# ── Regression: issue #5211 ────────────────────────────────────────────

class TestIssue5211OpenCodeGoDotPreservation:
    """OpenCode Go model names with dots must pass through unchanged."""

    @pytest.mark.parametrize("model,expected", [
        ("minimax-m2.7", "minimax-m2.7"),
        ("minimax-m2.5", "minimax-m2.5"),
        ("glm-4.5", "glm-4.5"),
        ("kimi-k2.5", "kimi-k2.5"),
        ("some-model-1.0.3", "some-model-1.0.3"),
    ])
    def test_opencode_go_preserves_dots(self, model, expected):
        result = normalize_model_for_provider(model, "opencode-go")
        assert result == expected, f"Expected {expected!r}, got {result!r}"

    def test_opencode_go_not_in_dot_to_hyphen_set(self):
        """opencode-go must NOT be in the dot-to-hyphen provider set."""
        assert "opencode-go" not in _DOT_TO_HYPHEN_PROVIDERS


# ── Anthropic dot-to-hyphen conversion (regression) ────────────────────

class TestAnthropicDotToHyphen:
    """Anthropic API still needs dots→hyphens."""


# ── OpenCode Zen regression ────────────────────────────────────────────

class TestOpenCodeZenModelNormalization:
    """OpenCode Zen preserves dots for most models, but Claude stays hyphenated."""


# ── Copilot dot preservation (regression) ──────────────────────────────

class TestCopilotDotPreservation:
    """Copilot preserves dots in model names."""


# ── Copilot model-name normalization (issue #6879 regression) ──────────

class TestCopilotModelNormalization:
    """Copilot requires bare dot-notation model IDs.

    Regression coverage for issue #6879 and the broken Copilot branch
    that previously left vendor-prefixed Anthropic IDs (e.g.
    ``anthropic/claude-sonnet-4.6``) and dash-notation Claude IDs (e.g.
    ``claude-sonnet-4-6``) unchanged, causing the Copilot API to reject
    the request with HTTP 400 "model_not_supported".
    """

    def test_openai_codex_still_strips_openai_prefix(self):
        """Regression: openai-codex must still strip the openai/ prefix."""
        assert normalize_model_for_provider("openai/gpt-5.4", "openai-codex") == "gpt-5.4"


# ── Aggregator providers (regression) ──────────────────────────────────

class TestAggregatorProviders:
    """Aggregators need vendor/model slugs."""


class TestCustomProviderIsNotAVendorIdentity:
    """``custom`` is a generic bucket, not a vendor -- an alias that merely
    *resolves to* ``custom`` (e.g. ``ollama`` -> ``custom`` in
    ``_PROVIDER_ALIASES``) must not be treated as a redundant prefix the
    way ``zai/``, ``gemini/``, etc. are for their own native providers.

    Regression for: a named custom provider (e.g. a LiteLLM proxy fronting
    Ollama) registers its own routing name as ``ollama/glm-5.2``. Stripping
    the ``ollama/`` prefix because it happens to alias to ``custom``
    produced a bare ``glm-5.2`` the proxy doesn't recognise.
    """


# ── detect_vendor ──────────────────────────────────────────────────────


# ── DeepSeek current and V-series pass-through ─────────────────────────

class TestDeepseekVSeriesPassThrough:
    """DeepSeek's canonical V4.1 Flash id and explicit V-series ids are
    first-class model IDs accepted directly by DeepSeek's API."""

    def test_deepseek_provider_preserves_v41_flash(self):
        result = normalize_model_for_provider("deepseek-flash", "deepseek")
        assert result == "deepseek-flash"

    def test_deepseek_provider_preserves_v4_pro(self):
        result = normalize_model_for_provider("deepseek-v4-pro", "deepseek")
        assert result == "deepseek-v4-pro"


# ── DeepSeek retired alias remapping ────────────────────────────────────

class TestDeepseekCanonicalAndReasonerMapping:
    """Retired aliases and fuzzy names rewrite to canonical V4.1 Flash."""

    def test_provider_path_rewrites_reasoner(self):
        assert (
            normalize_model_for_provider("deepseek-reasoner", "deepseek")
            == "deepseek-flash"
        )

    @pytest.mark.parametrize("model", [
        "deepseek-r1",
        "deepseek-r1-0528",
        "deepseek-think-v3",
        "deepseek-reasoning-preview",
        "deepseek-cot-experimental",
    ])
    def test_reasoner_keywords_map_to_v41_flash(self, model):
        assert _normalize_for_deepseek(model) == "deepseek-flash"


# ── Regression: issue #78796 ───────────────────────────────────────────

class TestIssue78796NvidiaPrefixRepair:
    """A bare NVIDIA model id must regain its ``vendor/`` prefix.

    build.nvidia.com serves ``nvidia/nemotron-…``; a bare
    ``nemotron-3-ultra-550b-a55b`` returns a naked ``404 page not found``
    that never names the model, so the failure reads like an outage.
    """

    @pytest.mark.parametrize("model,expected", [
        ("nemotron-3-ultra-550b-a55b", "nvidia/nemotron-3-ultra-550b-a55b"),
        ("nemotron-3-super-120b-a12b", "nvidia/nemotron-3-super-120b-a12b"),
        (
            "nemotron-3-nano-omni-30b-a3b-reasoning",
            "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
        ),
    ])
    def test_bare_nemotron_regains_prefix(self, model, expected):
        assert normalize_model_for_provider(model, "nvidia") == expected

    def test_third_party_model_gets_its_own_vendor(self):
        """NIM also hosts third-party models — the prefix is the catalogue's,
        not a hardcoded ``nvidia/``."""
        assert normalize_model_for_provider("glm-5.2", "nvidia") == "z-ai/glm-5.2"

    @pytest.mark.parametrize("model", [
        "nvidia/nemotron-3-ultra-550b-a55b",
        "z-ai/glm-5.2",
    ])
    def test_already_prefixed_is_untouched(self, model):
        assert normalize_model_for_provider(model, "nvidia") == model

    @pytest.mark.parametrize("model", [
        "my-local-nim-container",
        "some-finetune-v2",
    ])
    def test_unknown_names_pass_through(self, model):
        """The same provider id fronts local NIM containers. An id absent from
        the catalogue is a lookup miss, not a guess — leave it alone."""
        assert normalize_model_for_provider(model, "nvidia") == model

    def test_other_providers_unaffected(self):
        assert normalize_model_for_provider("my-model", "custom") == "my-model"
        assert (
            normalize_model_for_provider("claude-sonnet-4.6", "openrouter")
            == "anthropic/claude-sonnet-4.6"
        )
