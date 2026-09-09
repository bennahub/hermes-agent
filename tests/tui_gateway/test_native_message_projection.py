"""Native durable rows agree across history, preview, attention and push."""
import json
import threading
from types import SimpleNamespace

import pytest

from agent.message_projection import native_metadata, owner_attention, projection, stamp_final
from hermes_state import SessionDB
from tui_gateway.server import _canonical_owner_read_state, _latest_message_preview, _history_to_messages
from hermes_cli.web_routers.sessions import _project_for_display
from gateway.push_registry import is_notifiable


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    database = SessionDB(tmp_path / "state.db")
    database.create_session("owner", source="desktop")
    yield database
    database.close()


@pytest.mark.parametrize("text", ["[System: this is my own quote]", "Message from the bank: please explain"])
def test_native_owner_quote_identity_and_correlations_survive_all_reads(db, text):
    metadata = native_metadata("owner", "owner", "message", metadata={"client_message_ids": ["one", "two"]})
    mid = db.append_message("owner", "user", text, display_metadata=metadata)
    rows = db.get_messages("owner")
    assert _history_to_messages(rows)[0]["text"] == text
    assert _project_for_display(rows)[0].get("display_kind") != "hidden"
    assert _project_for_display(rows)[0]["id"] == mid
    assert _project_for_display(rows)[0]["display_metadata"]["client_message_ids"] == ["one", "two"]
    assert _latest_message_preview(db, "owner")[1] == "user"


@pytest.mark.parametrize("origin,audience,purpose,kind,visible,attention", [
    ("runtime", "internal", "control", "hidden", False, False),
    ("agent", "internal", "progress", "hidden", False, False),
    ("agent", "peer", "collaboration", "a2a_message", True, False),
    ("agent", "owner", "result", None, True, True),
    ("agent", "owner", "decision", "autonomy_owner_notice", True, True),
])
def test_native_row_consumers_agree(db, origin, audience, purpose, kind, visible, attention):
    role = "user" if origin == "runtime" else "assistant"
    metadata = native_metadata(origin, audience, purpose, source_session_id="owner")
    db.append_message("owner", role, "QA content", display_kind=kind, display_metadata=metadata, timestamp=1000)
    rows = db.get_messages("owner")
    assert bool(_history_to_messages(rows)) == visible
    assert (_project_for_display(rows)[0].get("display_kind") != "hidden") == visible
    assert (_canonical_owner_read_state(db, "owner")["latest_reply_at"] is not None) == attention
    assert bool(_latest_message_preview(db, "owner")[0]) == attention
    assert is_notifiable(
        kind="task_complete", role=role, content="QA content",
        display_kind=kind, display_metadata=metadata) == attention
    assert rows[0]["role"] == role and rows[0]["content"] == "QA content"


def test_canonical_read_ignores_newer_peer_and_progress_but_keeps_owner_question(db):
    db.append_message("owner", "assistant", "Answer", timestamp=1000)
    db.set_session_read("owner", through=1000)
    db.append_message("owner", "assistant", "Peer reply", timestamp=1100,
                      display_metadata=native_metadata("agent", "peer", "collaboration"))
    db.append_message("owner", "assistant", "Waiting", timestamp=1200,
                      display_metadata=native_metadata("agent", "internal", "progress"))
    db.append_message("owner", "assistant", "Checking now", timestamp=1250,
                      tool_calls=[{"id":"native-call", "type":"function", "function":{"name":"read_file", "arguments":"{}"}}])
    state = _canonical_owner_read_state(db, "owner")
    assert state["latest_reply_at"] == state["last_read_at"] == 1000
    db.append_message("owner", "assistant", "Your decision?", timestamp=1300,
                      display_kind="autonomy_owner_notice", display_metadata=native_metadata("agent", "owner", "decision"))
    assert _canonical_owner_read_state(db, "owner")["latest_reply_at"] == 1300
    db.set_session_read("owner", through=1300)
    db.set_session_read("owner", through=1000)
    assert _canonical_owner_read_state(db, "owner")["last_read_at"] == 1300


@pytest.mark.parametrize("event_type", ["completion", "watch_match", "async_delegation"])
def test_real_notification_dispatch_types_runtime_before_native_persist(db, monkeypatch, event_type):
    from tui_gateway import server
    from tui_gateway import session_notifications as notifications
    from tools import async_delegation
    monkeypatch.setattr(async_delegation, "claim_event_delivery", lambda *a: "claim")
    monkeypatch.setattr(async_delegation, "complete_event_delivery", lambda *a: None)
    monkeypatch.setattr(server, "_wire_desktop_sinks", lambda: None)
    def submit(*args, **kwargs):
        db.append_message("owner", "user", args[3], display_kind=kwargs.get("display_kind"),
                          display_metadata=kwargs.get("display_metadata"))
        return True
    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    session = {"session_key": "owner", "running": True, "history_lock": threading.RLock()}
    server._notif_dispatch_event_in_home("ui", session,
        {"type": event_type, "session_id": "proc_qa", "session_key": "owner", "delegation_id": "child"},
        "[IMPORTANT: Background process proc_qa completed normally]")
    row = db.get_messages("owner")[-1]
    assert row["role"] == "user" and row["display_kind"] == "hidden"
    assert projection(row["display_metadata"])["origin"] == "runtime"
    assert projection(row["display_metadata"])["process_id"] == "proc_qa"
    assert not _history_to_messages([row])


def test_native_notice_distinguishes_input_and_owner_question(db):
    from agent.autonomy.owner_projection import _insert_message
    with db._lock:
        _insert_message(db._conn, "owner", "user", "[Autonomy notice — not the owner]", 1000)
        _insert_message(db._conn, "owner", "assistant", "Need your decision", 1001)
        db._conn.commit()
    rows = db.get_messages("owner")
    assert [m["text"] for m in _history_to_messages(rows)] == ["Need your decision"]
    assert _canonical_owner_read_state(db, "owner")["latest_reply_at"] == 1001


def test_final_peer_reply_keeps_attachments_and_never_changes_model_content(db):
    from gateway.a2a_threads import reply_send_id, send_event_id
    request_event = send_event_id("parent", "child", "send-one")
    messages = [{"role": "user", "content": "Peer request"},
                {"role": "assistant", "content": "Reply", "display_metadata": {"attachments": [{"artifact_id": "QA"}]}}]
    agent = SimpleNamespace(_persist_user_message_idx=0,
        _turn_display_projection=projection(native_metadata(
            "peer", "peer", "collaboration", sender="parent", recipient="child",
            send_id="send-one", event_id=request_event,
        )))
    stamp_final(agent, messages)
    assert messages[-1]["content"] == "Reply" and messages[-1]["role"] == "assistant"
    assert messages[-1]["display_metadata"]["attachments"] == [{"artifact_id": "QA"}]
    reply = projection(messages[-1]["display_metadata"])
    assert reply["sender"] == "child" and reply["recipient"] == "parent"
    assert reply["send_id"] == reply_send_id("parent", "child", "send-one")
    assert reply["event_id"] == send_event_id("child", "parent", reply["send_id"])
    assert not owner_attention("assistant", "Reply", messages[-1]["display_kind"], messages[-1]["display_metadata"])


def test_malformed_projection_does_not_mint_owner_provenance():
    assert projection({"projection": {"version": True, "origin": "owner", "audience": "owner", "purpose": "message"}}) is None
    assert not owner_attention("assistant", "hidden", "hidden", '{"projection": "owner"}')
