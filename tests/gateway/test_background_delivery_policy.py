"""Owner-facing background delivery policy.

Pins the rule that a background event contributes only the agent's useful
summary: transport envelopes and no-material-result replies never become
owner-facing Bot Chat messages, while real events always do.
"""

import pytest

from gateway.background_delivery import (
    BACKGROUND_DELIVERY_ENV,
    background_delivery_active,
    is_no_material_result,
    is_placeholder_payload,
    is_transport_envelope,
    should_suppress_owner_facing_message,
)
from gateway.response_filters import is_unambiguous_silence_control

# Verbatim from the live Bot Chat rows this policy exists to stop.
REAL_WEBHOOK_ENVELOPE = (
    '[Webhook "fares-cro-events" output — inbound event, not the user. '
    'Review it, act on anything that needs action, and summarize for the chat.]\n\n'
    '{"event":"supplier.created","id":42}'
)


@pytest.fixture
def background(monkeypatch):
    monkeypatch.setenv(BACKGROUND_DELIVERY_ENV, "1")


def test_inactive_by_default(monkeypatch):
    monkeypatch.delenv(BACKGROUND_DELIVERY_ENV, raising=False)
    assert background_delivery_active() is False


def test_env_falsey_values_do_not_activate(monkeypatch):
    for value in ("", "0", "false", "no", "  "):
        monkeypatch.setenv(BACKGROUND_DELIVERY_ENV, value)
        assert background_delivery_active() is False


def test_interactive_transport_envelopes_are_not_suppressed(monkeypatch):
    """Normal text remains untouched outside a background delivery."""
    monkeypatch.delenv(BACKGROUND_DELIVERY_ENV, raising=False)
    assert should_suppress_owner_facing_message("user", REAL_WEBHOOK_ENVELOPE) is False


def test_only_unambiguous_silence_controls_are_global(monkeypatch):
    """Bare human prose stays visible; machine tokens take the hidden lane."""
    monkeypatch.delenv(BACKGROUND_DELIVERY_ENV, raising=False)
    assert is_unambiguous_silence_control("[SILENT]") is True
    assert is_unambiguous_silence_control("NO_REPLY") is True
    assert is_unambiguous_silence_control("Silent.") is False
    assert is_unambiguous_silence_control("No reply") is False
    assert should_suppress_owner_facing_message("assistant", "[SILENT]") is False


def test_real_webhook_envelope_is_suppressed(background):
    assert is_transport_envelope(REAL_WEBHOOK_ENVELOPE) is True
    assert should_suppress_owner_facing_message("user", REAL_WEBHOOK_ENVELOPE) is True


# Verbatim from cron/scheduler.py::_deliver_to_bot_chat — the lane cron AND
# Bot Mode agent-to-agent delivery share.
REAL_CRON_ENVELOPE = (
    '[Cronjob "morning-check" output — scheduled job, not the user. '
    'Review it, act on anything that needs action, and summarize for the chat.]\n\nnothing new'
)


@pytest.mark.parametrize("text", [
    '[Webhook "badr-engineering-event-desk" output — inbound event, not the user. Review it.]',
    '[Routine "morning-check" output — inbound event, not the user.]',
    '[Cron job output — inbound event, not the user.]',
    '[Scheduled task output — inbound event, not the user.]',
    REAL_CRON_ENVELOPE,
])
def test_envelope_variants(background, text):
    assert should_suppress_owner_facing_message("user", text) is True


@pytest.mark.parametrize("text", ["Placeholder — ignore", "placeholder - ignore", "[Placeholder — ignore]"])
def test_placeholders_suppressed(background, text):
    assert is_placeholder_payload(text) is True
    assert should_suppress_owner_facing_message("user", text) is True


@pytest.mark.parametrize("text", ["[SILENT]", "  [SILENT]  ", "NO_REPLY", "SILENT", "[SILENT] No changes detected"])
def test_no_material_result_suppressed(background, text):
    assert is_no_material_result(text) is True
    assert should_suppress_owner_facing_message("assistant", text) is True


def test_empty_assistant_reply_suppressed(background):
    assert should_suppress_owner_facing_message("assistant", "   ") is True


# ── the half that must NOT be suppressed ────────────────────────────────────

MEANINGFUL = [
    "سجل مورد جديد: شركة X — يحتاج مراجعتك قبل الاعتماد.",
    "ظهر خطأ جديد يؤثر على checkout — 42 حدث في آخر ساعة.",
    "PR #123 اندمج، الاختبارات ناجحة.",
    "Deployment failed on production — rolling back now.",
    "Blocked: I need your approval on the supplier terms before continuing.",
]


@pytest.mark.parametrize("text", MEANINGFUL)
def test_meaningful_events_always_delivered(background, text):
    assert should_suppress_owner_facing_message("assistant", text) is False


def test_summary_mentioning_the_marker_is_delivered(background):
    """A real report that merely mentions the token must survive."""
    text = "I considered replying [SILENT] but the checkout error is material, so: 3 new issues."
    assert should_suppress_owner_facing_message("assistant", text) is False


def test_summary_quoting_an_envelope_later_is_delivered(background):
    text = 'Supplier X registered.\n\nSource envelope was [Webhook "fares-cro-events" output — ...]'
    assert is_transport_envelope(text) is False
    assert should_suppress_owner_facing_message("assistant", text) is False


def test_user_message_that_is_real_text_is_delivered(background):
    assert should_suppress_owner_facing_message("user", "شنو وضع الطلب؟") is False


def test_other_roles_untouched(background):
    for role in ("tool", "system", None):
        assert should_suppress_owner_facing_message(role, "[SILENT]") is False


def test_non_string_content_is_safe(background):
    assert should_suppress_owner_facing_message("assistant", None) is False
    assert should_suppress_owner_facing_message("user", {"a": 1}) is False


def test_real_cron_envelope_is_suppressed(background):
    """The cron/agent-relay wording differs from the webhook one ('scheduled
    job, not the user' vs 'inbound event, not the user') and must also match."""
    assert is_transport_envelope(REAL_CRON_ENVELOPE) is True
    assert should_suppress_owner_facing_message("user", REAL_CRON_ENVELOPE) is True


def test_agent_relay_envelope_is_visible_in_recipient_bot_chat(background):
    text = '[Message from @badr — agent relay, not the user. Review and act.]\n\nPR merged'
    assert should_suppress_owner_facing_message("user", text) is False


def test_live_bot_mode_agent_message_is_visible(background):
    """Local message_agent communication belongs in the recipient's Bot Chat."""
    text = (
        "Message from 🤖 abu-saud (@abu-saud): Executive Operator readiness "
        "probe. No action and no reply needed."
    )
    assert should_suppress_owner_facing_message("user", text) is False
