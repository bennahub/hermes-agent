"""Integration: the policy applied at the real persistence choke points.

Unit tests pin the classifier; these pin that HermesState actually drops the
suppressed rows — and, just as importantly, that a meaningful summary still
lands and that interactive prose stays untouched while exact silence control
tokens never become owner-facing rows.
"""

import pytest

from gateway.background_delivery import BACKGROUND_DELIVERY_ENV

ENVELOPE = (
    '[Webhook "fares-cro-events" output — inbound event, not the user. '
    'Review it, act on anything that needs action, and summarize for the chat.]\n\n'
    '{"event":"supplier_registered"}'
)
SUMMARY = "سجل مورد جديد: شركة الرياض للتوريدات — يحتاج مراجعتك."


@pytest.fixture
def db(tmp_path):
    from hermes_state import SessionDB

    yield SessionDB(db_path=tmp_path / "state.db")


def _session(db, name="s1"):
    db.create_session(name, source="cli")
    return name


def _rows(db, sid):
    return db.get_messages(sid)


def test_batch_drops_envelope_and_silence_keeps_summary(db, monkeypatch, tmp_path):
    monkeypatch.setenv(BACKGROUND_DELIVERY_ENV, "1")
    sid = _session(db)
    inserted = db.append_messages_batch(sid, [
        {"role": "user", "content": ENVELOPE},
        {"role": "assistant", "content": SUMMARY},
    ])
    rows = _rows(db, sid)
    assert inserted == 1, "only the summary should be written"
    assert len(rows) == 1
    assert rows[0]["role"] == "assistant"
    assert rows[0]["content"] == SUMMARY


def test_batch_drops_everything_for_a_noop_event(db, monkeypatch):
    monkeypatch.setenv(BACKGROUND_DELIVERY_ENV, "1")
    sid = _session(db)
    inserted = db.append_messages_batch(sid, [
        {"role": "user", "content": ENVELOPE},
        {"role": "assistant", "content": "[SILENT]"},
    ])
    assert inserted == 0
    assert _rows(db, sid) == []


def test_message_count_not_bumped_when_everything_suppressed(db, monkeypatch):
    """A suppressed turn must not move the counter, preview, or unread cursor."""
    monkeypatch.setenv(BACKGROUND_DELIVERY_ENV, "1")
    sid = _session(db)
    before = db.get_session(sid)["message_count"]
    db.append_messages_batch(sid, [
        {"role": "user", "content": ENVELOPE},
        {"role": "assistant", "content": "[SILENT]"},
    ])
    assert db.get_session(sid)["message_count"] == before


def test_interactive_turn_hides_exact_silent_control_token(db, monkeypatch):
    """Control text is hidden while its assistant row preserves alternation."""
    monkeypatch.delenv(BACKGROUND_DELIVERY_ENV, raising=False)
    sid = _session(db)
    inserted = db.append_messages_batch(sid, [
        {"role": "user", "content": ENVELOPE},
        {"role": "assistant", "content": "[SILENT]"},
    ])
    assert inserted == 2
    rows = _rows(db, sid)
    assert [r["content"] for r in rows] == [ENVELOPE, ""]
    assert rows[-1]["api_content"] == "[SILENT]"
    assert rows[-1]["display_kind"] == "control"

    db.append_message(sid, "user", "next real turn")
    assert [r["role"] for r in _rows(db, sid)] == ["user", "assistant", "user"]


def test_agent_to_agent_message_persists_in_recipient_bot_chat(db, monkeypatch):
    monkeypatch.setenv(BACKGROUND_DELIVERY_ENV, "1")
    sid = _session(db)
    dm = "Message from 🤖 abu-saud (@abu-saud): Check readiness and report back."
    inserted = db.append_messages_batch(sid, [
        {"role": "user", "content": dm},
        {"role": "assistant", "content": "[SILENT]"},
    ])
    assert inserted == 2
    rows = _rows(db, sid)
    assert [r["content"] for r in rows] == [dm, ""]
    assert rows[-1]["api_content"] == "[SILENT]"
    assert rows[-1]["display_kind"] == "control"


def test_append_message_single_path_also_guarded(db, monkeypatch):
    monkeypatch.setenv(BACKGROUND_DELIVERY_ENV, "1")
    sid = _session(db)
    assert db.append_message(sid, "assistant", "[SILENT]") == -1
    assert db.append_message(sid, "user", ENVELOPE) == -1
    good = db.append_message(sid, "assistant", SUMMARY)
    assert good > 0
    rows = _rows(db, sid)
    assert len(rows) == 1 and rows[0]["content"] == SUMMARY


def test_tool_and_system_rows_survive_background_turns(db, monkeypatch):
    """Only user envelopes and no-op assistant replies are policy targets."""
    monkeypatch.setenv(BACKGROUND_DELIVERY_ENV, "1")
    sid = _session(db)
    inserted = db.append_messages_batch(sid, [
        {"role": "user", "content": ENVELOPE},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "function": {"name": "x", "arguments": "{}"}}]},
        {"role": "tool", "content": "tool output", "tool_call_id": "1"},
        {"role": "assistant", "content": SUMMARY},
    ])
    roles = [r["role"] for r in _rows(db, sid)]
    assert inserted == 3
    assert roles == ["assistant", "tool", "assistant"]


@pytest.mark.parametrize("source", ["webhook", "cron"])
def test_background_execution_session_is_internal_but_queryable(db, source):
    """Execution history remains addressable but never enters owner recents."""
    sid = f"{source}-execution"
    db.create_session(sid, source=source, hidden=True)
    db.append_message(sid, "user", "raw execution payload")
    db.append_message(sid, "assistant", "[SILENT]")
    db.append_message(sid, "assistant", "material internal result")
    db.append_message(sid, "assistant", "3 checks, 1 failed\n\n[SILENT]")
    db.append_message(
        sid,
        "assistant",
        "",
        tool_calls=[{"id": "call-1", "function": {"name": "probe", "arguments": "{}"}}],
    )

    row = db.get_session(sid)
    assert row["hidden"] == 1
    assert [m["content"] for m in db.get_messages(sid)] == [
        "raw execution payload",
        "material internal result",
        "3 checks, 1 failed\n\n[SILENT]",
        "",
    ]
    visible_ids = {r["id"] for r in db.list_sessions_rich(limit=100)}
    internal_ids = {
        r["id"] for r in db.list_sessions_rich(limit=100, include_hidden=True)
    }
    assert sid not in visible_ids
    assert sid in internal_ids


def test_interactive_session_stays_visible(db):
    sid = _session(db, "interactive")
    db.append_message(sid, "user", "real owner turn")
    assert db.get_session(sid)["hidden"] == 0

    # A source label alone is not authority to hide a session; gateway webhook
    # execution passes hidden=True explicitly at its creation seam.
    db.create_session("visible-webhook", source="webhook")
    assert db.get_session("visible-webhook")["hidden"] == 0


def test_gateway_peer_self_heal_preserves_internal_visibility(db):
    """A crash-before-create repair must not resurrect a visible webhook row."""
    sid = "missing-webhook-row"
    db.record_gateway_session_peer(
        sid,
        source="webhook",
        user_id="webhook:alerts",
        session_key="webhook:webhook:alerts:delivery-1",
        chat_id="webhook:alerts:delivery-1",
        chat_type="webhook",
        display_name="webhook/alerts",
        hidden=True,
    )
    row = db.get_session(sid)
    assert row is not None
    assert row["hidden"] == 1
