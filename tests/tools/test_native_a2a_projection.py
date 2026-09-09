"""Native transport provenance is confined to its private file and recipient."""
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from agent.message_projection import native_metadata, projection, record_peer_final, stamp_final
from agent.turn_context import _stage_turn_user_message
from gateway.a2a_threads import read_thread, record_send, reply_send_id, send_event_id, thread_id
from hermes_cli.main import _read_query_file
from hermes_state import SessionDB
from tools import bot_mode_dm as dm
from tools import bot_mode_probe
from tui_gateway import server


@pytest.fixture
def native(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    home = root / "profiles" / "recipient"
    home.mkdir(parents=True)
    (home / "profile.yaml").write_text("ui_meta:\n  hermes-bots:\n    shape: cloud\n")
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(dm, "_dm_dir", lambda: private)
    monkeypatch.setattr(dm, "cleanup_bot_dm_cache", lambda: 0)
    metadata = native_metadata("peer", "peer", "collaboration", sender="sender", recipient="recipient",
        send_id="native-send", event_id=send_event_id("sender", "recipient", "native-send"), source_session_id="source")
    return root, home, private, metadata


def test_real_cli_query_file_stage_and_durable_recipient_reply(native):
    root, home, private, metadata = native
    body = "Message from 🤖 sender (@sender): Please check QA"
    path = dm._write_dm_file(body, display_metadata=metadata, projection_root=str(root))
    events = record_send(root, sender="sender", recipients=["recipient"], body="Please check QA", send_id="native-send")
    args = SimpleNamespace(query_file=path, query=None)
    _read_query_file(args)
    agent = SimpleNamespace(_pending_cli_user_message=None, _persist_user_message_idx=0)
    row, _ = _stage_turn_user_message(agent, args.query, None, None, None, None, None)
    assert row["content"] == body and row["role"] == "user"
    assert row["display_kind"] == "a2a_message"
    assert projection(row["display_metadata"])["event_id"] == events[0].event_id
    messages = [row, {"role": "assistant", "content": "QA reply"}]
    stamp_final(agent, messages)
    reply = projection(messages[-1]["display_metadata"])
    assert reply["event_id"] == send_event_id(reply["sender"], reply["recipient"], reply["send_id"])
    assert reply["event_id"] != events[0].event_id
    agent._session_db = SimpleNamespace(db_path=home / "state.db")
    recorded = record_peer_final(agent, messages)
    assert recorded[0].event_id == reply["event_id"]
    assert [event.body for event in read_thread(root, thread=thread_id("sender", "recipient"))] == [
        "Please check QA", "QA reply"
    ]
    assert record_peer_final(agent, messages)[0].event_id == reply["event_id"]
    with SessionDB(home / "state.db") as db:
        db.create_session("recipient", source="cli")
        db.append_messages_batch("recipient", messages)
        stored = db.get_messages("recipient")
        from tui_gateway.server import _canonical_owner_read_state
        assert _canonical_owner_read_state(db, "recipient")["latest_reply_at"] is None
        assert [projection(r["display_metadata"])["send_id"] for r in stored] == [
            "native-send", reply_send_id("sender", "recipient", "native-send")
        ]
    # Provenance is one turn only, never an ambient later Owner attribution.
    next_row, _ = _stage_turn_user_message(agent, body, None, None, None, None, None)
    assert "display_metadata" not in next_row
    dm._unlink_dm_file(path)
    assert not list(private.iterdir())


def test_peer_final_records_finalized_attachments_and_silent_is_not_success(native):
    root, home, _, metadata = native
    record_send(root, sender="sender", recipients=["recipient"], body="Make a report", send_id="native-send")
    agent = SimpleNamespace(_persist_user_message_idx=0,
                            _session_db=SimpleNamespace(db_path=home / "state.db"))
    messages = [
        {"role": "user", "content": "Make a report", "display_metadata": metadata},
        {"role": "assistant", "content": "Done"},
    ]
    agent._turn_display_projection = projection(metadata)
    stamp_final(agent, messages)
    messages[-1]["display_metadata"]["attachments"] = [
        {"artifact_id": "a" * 32, "filename": "report.pdf", "mime_type": "application/pdf"}
    ]
    reply = record_peer_final(agent, messages)
    assert reply[0].attachments[0]["artifact_id"] == "a" * 32
    assert len(read_thread(root, thread=reply[0].thread_id)) == 2

    silent_metadata = native_metadata(
        "peer", "peer", "collaboration", sender="sender", recipient="recipient",
        send_id="silent-request", event_id=send_event_id("sender", "recipient", "silent-request"),
    )
    silent = [{"role": "user", "content": "quiet", "display_metadata": silent_metadata},
              {"role": "assistant", "content": "[SILENT]"}]
    agent._turn_display_projection = projection(silent_metadata)
    stamp_final(agent, silent)
    assert record_peer_final(agent, silent) == []


def test_fast_live_reply_sees_request_in_thread_before_parent_wake(native):
    root, home, _, _ = native

    class ParentDB:
        db_path = str(root / "state.db")

        @staticmethod
        def get_session_title(_sid):
            return "Bot Chat"

    parent = SimpleNamespace(
        _session_db=ParentDB(), session_id="parent-session", _session_title_hint=None,
        _bot_mode_protocol=True, tools=[], valid_tool_names=set(),
    )
    observed = []

    def dispatch(profile, prompt, *, display_metadata=None):
        assert profile == "recipient"
        child = SimpleNamespace(
            _persist_user_message_idx=0,
            _session_db=SimpleNamespace(db_path=home / "state.db"),
            _turn_display_projection=projection(display_metadata),
        )
        messages = [{"role": "user", "content": prompt, "display_metadata": display_metadata},
                    {"role": "assistant", "content": "fast child result"}]
        stamp_final(child, messages)
        record_peer_final(child, messages)
        # The thread is the run-scoped one the request opened, not "these two
        # agents" — and the reply is in it, which is what proves the reply
        # inherited the request's run rather than starting an episode of one.
        run = projection(display_metadata)["run_id"]
        assert run
        observed.extend(event.body for event in read_thread(
            root, thread=thread_id("default", "recipient", run)
        ))
        assert read_thread(root, thread=thread_id("default", "recipient")) == []
        return True

    parent._message_agent_dispatcher = dispatch
    bot_mode_probe._reset_cache_for_tests()
    result = json.loads(dm.message_agent_tool(target="recipient", message="do work", agent=parent))
    assert result["transport"] == "live_session"
    assert observed == ["do work", "fast child result"]


@pytest.mark.parametrize("tamper", ["content", "recipient", "root", "event", "symlink", "world_readable", "ordinary_file"])
def test_private_ingress_refuses_foreign_or_ambiguous_provenance(native, tamper):
    root, home, private, metadata = native
    body = "Message from sender: QA"
    path = dm._write_dm_file(body, display_metadata=metadata, projection_root=str(root))
    sidecar = Path(path + ".projection.json")
    if tamper == "content":
        body += " changed"
    elif tamper == "symlink":
        other = sidecar.with_suffix(".other")
        sidecar.rename(other)
        sidecar.symlink_to(other)
    elif tamper == "world_readable":
        sidecar.chmod(0o644)
    elif tamper == "ordinary_file":
        path = str(root / "arbitrary.txt")
        Path(path).write_text(body)
        Path(path + ".projection.json").write_bytes(sidecar.read_bytes())
    else:
        data = json.loads(sidecar.read_text())
        if tamper == "root":
            data["root"] = str(root / "foreign")
        else:
            data["metadata"]["projection"]["recipient" if tamper == "recipient" else "event_id"] = "foreign"
        sidecar.write_text(json.dumps(data))
    assert dm.read_dm_projection(path, body) is None


def test_native_send_identity_replay_and_mismatch(native):
    root, _, _, _ = native
    first = record_send(root, sender="sender", recipients=["recipient"], body="same", send_id="one")
    replay = record_send(root, sender="sender", recipients=["recipient"], body="same", send_id="one")
    second = record_send(root, sender="sender", recipients=["recipient"], body="same", send_id="two")
    assert first[0].event_id == replay[0].event_id != second[0].event_id
    assert not record_send(root, sender="sender", recipients=["recipient"], body="different", send_id="one")
    assert not record_send(root, sender="sender", recipients=["foreign"], body="same", send_id="one")


def test_cli_staged_provenance_cannot_cross_native_profile_between_read_and_turn(native, monkeypatch):
    root, _, _, metadata = native
    path = dm._write_dm_file('body',display_metadata=metadata,projection_root=str(root))
    _read_query_file(SimpleNamespace(query_file=path,query=None))
    monkeypatch.setenv('HERMES_HOME',str(root/'profiles'/'foreign'))
    row, _ = _stage_turn_user_message(SimpleNamespace(), 'body', None,None,None,None,None)
    assert 'display_metadata' not in row


def test_completion_attribution_requires_exact_native_ack_source_and_unique_identity(native):
    from agent.message_projection import process_collaboration_identity
    root, home, _, metadata = native
    with SessionDB(home/'state.db') as db:
        db.create_session('source', source='desktop')
        db.create_session('foreign', source='desktop')
        ack = {'status':'sent', 'process_id':'proc_native', 'display_metadata':metadata}
        db.append_message('source','tool',json.dumps(ack),tool_name='message_agent',tool_call_id='send1')
        assert process_collaboration_identity(db,'source','proc_native')['send_id'] == 'native-send'
        assert process_collaboration_identity(db,'foreign','proc_native') == {}
        assert process_collaboration_identity(db,'source','proc_other') == {}
        spoof = {**ack,'process_id':'proc_spoof'}
        db.append_message('source','user',json.dumps(spoof))
        db.append_message('source','tool',json.dumps(spoof),tool_name='terminal',tool_call_id='shell')
        assert process_collaboration_identity(db,'source','proc_spoof') == {}
        foreign = native_metadata('peer','peer','collaboration', sender='sender',recipient='recipient',source_session_id='source',send_id='other',event_id='different')
        db.append_message('source','tool',json.dumps({**ack,'display_metadata':foreign}),tool_name='message_agent',tool_call_id='send2')
        assert process_collaboration_identity(db,'source','proc_native') == {}


def test_native_live_queue_does_not_merge_peer_with_owner(native, monkeypatch):
    _, _, _, metadata = native
    session = {"running": True, "history_lock": threading.RLock(), "session_key": "recipient", "transport": None}
    monkeypatch.setitem(server._sessions, "projection-qa", session)
    assert server._submit_internal_prompt("projection-qa", "same", display_metadata=metadata)
    server._enqueue_prompt(session, "same", None, owner_task_nonce="existing-owner-nonce", client_message_id="owner-uuid")
    peer = session["queued_prompt"]
    owner = session["queued_prompts"][0]
    assert peer["display_kind"] == "a2a_message"
    assert projection(peer["display_metadata"])["origin"] == "peer"
    assert owner["display_metadata"]["client_message_ids"] == ["owner-uuid"]
    assert projection(owner["display_metadata"])["origin"] == "owner"
    assert server._sanitize_queued_entry_vs_inflight_user(peer, "same") == peer
