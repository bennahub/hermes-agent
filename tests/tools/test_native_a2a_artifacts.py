"""Real published IDs, native profile/lineage and private peer ingress."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
from agent.message_projection import native_metadata
from gateway.a2a_artifacts import authorize, recipient_context
from gateway.a2a_threads import read_thread, record_send, send_event_id, thread_id
from gateway.published_artifacts import publish
from hermes_state import SessionDB
from tools import bot_mode_dm as dm


@pytest.fixture
def pair(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    sender, recipient = [root / "profiles" / name for name in ("sender", "recipient")]
    for home in (sender, recipient):
        home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(sender))
    with SessionDB(sender / "state.db") as source, SessionDB(recipient / "state.db") as dest:
        source.create_session("source", "gui")
        source.create_session("foreign", "gui")
        source.end_session("source", "compression")
        source.create_session("tip", "gui", parent_session_id="source")
        dest.create_session("destination", "gui")
        file = sender / "qa.txt"
        file.write_text("CANONICAL_PEER_FILE_DATA")
        record = publish(sender, source_path=str(file), profile="sender", session_id="source")
        agent = SimpleNamespace(_session_db=source, session_id="tip", _bot_mode_all_sessions=True)
        target = SimpleNamespace(_session_db=dest, session_id="destination")
        yield root, sender, recipient, agent, target, record


def envelope(records):
    return native_metadata("peer", "peer", "collaboration", metadata={"attachments": records},
        sender="sender", recipient="recipient", source_session_id="tip", send_id="native-send",
        event_id=send_event_id("sender", "recipient", "native-send"))


def test_native_ids_lineage_recipient_files_and_replay(pair):
    root, sender, recipient, agent, target, record = pair
    refs = authorize(agent, [record.artifact_id, record.artifact_id])
    assert refs == [record.to_dict()]
    metadata = envelope(refs)
    before = (sender / "state.db").read_bytes()
    context = recipient_context(target, metadata)
    assert record.artifact_id in context and "CANONICAL_PEER_FILE_DATA" not in context
    staged = list((recipient / "attachments" / "peer").rglob("*.txt"))
    assert len(staged) == 1 and staged[0].read_text() == "CANONICAL_PEER_FILE_DATA"
    assert recipient_context(target, metadata) == context
    assert (sender / "state.db").read_bytes() == before
    kwargs = dict(sender="sender", recipients=["recipient"], body="Check file", send_id="native-send", attachments=refs)
    first = record_send(root, **kwargs)
    assert record_send(root, **kwargs)[0].event_id == first[0].event_id
    assert not record_send(root, **dict(kwargs, attachments=[]))
    assert first[0].attachments[0]["artifact_id"] == record.artifact_id


@pytest.mark.parametrize("bad", ["path", "missing", "foreign_session", "foreign_profile", "blob_changed", "blob_symlink", "profile_symlink"])
def test_unauthorized_artifacts_cannot_dispatch(pair, bad, tmp_path):
    root, sender, recipient, agent, target, record = pair
    ids = [record.artifact_id]
    if bad == "path":
        ids = [str(sender / "qa.txt")]
    elif bad == "missing":
        ids = ["0" * 32]
    elif bad == "foreign_session":
        agent.session_id = "foreign"
    elif bad == "foreign_profile":
        agent = target
    elif bad == "profile_symlink":
        real = root / "moved"
        sender.rename(real)
        sender.symlink_to(real)
    else:
        blob = sender / "artifacts" / "published" / record.artifact_id
        blob.unlink()
        if bad == "blob_changed":
            blob.write_bytes(b"CHANGED")
        else:
            blob.symlink_to(sender / "qa.txt")
    with pytest.raises((ValueError, OSError)):
        authorize(agent, ids)
    assert not (recipient / "attachments").exists()


def test_explicit_inline_argument_live_peer_roundtrip(pair, monkeypatch):
    root, sender, recipient, agent, target, record = pair
    monkeypatch.setattr("tools.bot_mode_probe.is_bot_mode_managed", lambda _: True)
    captured = []
    def dispatch(name, body, display_metadata):
        captured.append((name, body, display_metadata, recipient_context(target, display_metadata)))
        return True
    agent._message_agent_dispatcher = dispatch
    execute = INLINE_TOOL_EXECUTORS["message_agent"]
    ack = json.loads(execute(agent, {"target": "recipient", "message": "Check the attached file",
        "artifact_ids": [record.artifact_id]}, InlineToolContext("qa")))
    assert ack["status"] == "sent"
    assert captured[0][2]["attachments"] == [record.to_dict()]
    assert record.artifact_id in captured[0][3]
    ack2 = json.loads(execute(agent, {"target": "recipient", "message": "No files"}, InlineToolContext("qa")))
    assert ack2["status"] == "sent" and "attachments" not in captured[1][2]
    assert captured[1][3] == ""


def test_attachment_uses_cli_sidecar_when_live_dispatcher_lacks_metadata(pair, monkeypatch):
    root, _, _, agent, _, record = pair
    monkeypatch.setattr("tools.bot_mode_probe.is_bot_mode_managed", lambda _: True)
    live_calls, cli_calls = [], []

    def legacy_dispatch(profile, body):
        live_calls.append((profile, body))
        return True

    def native_cli(*_args, **kwargs):
        cli_calls.append(kwargs["display_metadata"])
        return json.dumps({"status": "sent", "process_id": "native-cli"})

    agent._message_agent_dispatcher = legacy_dispatch
    monkeypatch.setattr(dm, "_start_delivery", native_cli)
    ack = json.loads(INLINE_TOOL_EXECUTORS["message_agent"](
        agent,
        {"target": "recipient", "message": "Review it", "artifact_ids": [record.artifact_id]},
        InlineToolContext("qa"),
    ))
    assert ack["status"] == "sent" and ack["process_id"] == "native-cli"
    assert live_calls == []
    assert cli_calls[0]["attachments"][0]["artifact_id"] == record.artifact_id
    run = cli_calls[0]["projection"]["run_id"]
    assert run
    assert len(read_thread(root, thread=thread_id("sender", "recipient", run))) == 1


def test_cli_sidecar_keeps_exact_artifact_records(pair, tmp_path, monkeypatch):
    root, sender, recipient, agent, target, record = pair
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    monkeypatch.setattr(dm, "_dm_dir", lambda: private)
    monkeypatch.setattr(dm, "cleanup_bot_dm_cache", lambda: 0)
    metadata = envelope(authorize(agent, [record.artifact_id]))
    path = dm._write_dm_file("Check this file", display_metadata=metadata, projection_root=str(root))
    monkeypatch.setenv("HERMES_HOME", str(recipient))
    restored = dm.read_dm_projection(path, "Check this file")
    assert restored == metadata
    assert record.artifact_id in recipient_context(target, restored)
    dm._unlink_dm_file(path)


def test_actual_native_peer_turn_model_context_and_durable_caption(pair, monkeypatch):
    """Only model I/O is synthetic; staging/context/persistence run natively."""
    from unittest.mock import MagicMock, patch
    from run_agent import AIAgent
    from tests.run_agent.test_run_agent import _mock_response
    root, sender, recipient, agent, target, record = pair
    metadata = envelope(authorize(agent, [record.artifact_id]))
    record_send(root, sender="sender", recipients=["recipient"], body="Please review this file",
                send_id="native-send", attachments=[record.to_dict()])
    child_file = recipient / "peer-review.txt"
    child_file.write_text("CHILD PRODUCED DATA")
    child_record = publish(recipient, source_path=str(child_file), profile="recipient", session_id="destination")
    from gateway import published_artifacts
    monkeypatch.setenv("HERMES_HOME", str(recipient))
    with patch("model_tools.get_tool_definitions", return_value=[]), \
         patch("model_tools.check_toolset_requirements", return_value={}), \
         patch("agent.process_bootstrap.OpenAI"):
        native = AIAgent(api_key="synthetic", base_url="https://provider.invalid/v1", model="test/model",
            quiet_mode=True, skip_context_files=True, skip_memory=True,
            session_db=target._session_db, session_id="destination")
    native.client = MagicMock()
    def reply_with_artifact(**_kwargs):
        published_artifacts.note_pending(
            "destination", published_artifacts.message_reference([child_record])[0]
        )
        return _mock_response(content="Peer file available")
    native.client.chat.completions.create.side_effect = reply_with_artifact
    native._session_db_created = True
    native._cached_system_prompt = "Synthetic peer test"
    native._skip_mcp_refresh = True
    result = native.run_conversation("Please review this file",
        persist_user_display_kind="a2a_message", persist_user_display_metadata=metadata)
    assert result["final_response"] == "Peer file available"
    wire = native.client.chat.completions.create.call_args.kwargs["messages"]
    serialized = json.dumps(wire)
    assert record.artifact_id in serialized
    staged = next((recipient / "attachments" / "peer").rglob("*.txt"))
    assert str(staged) in serialized and staged.read_text() == "CANONICAL_PEER_FILE_DATA"
    stored = target._session_db.get_messages("destination")
    incoming = next(r for r in stored if r["role"] == "user")
    assert incoming["content"] == "Please review this file"
    assert incoming["display_metadata"] == metadata
    assert stored[-1]["display_metadata"]["projection"]["audience"] == "peer"
    exchange = read_thread(root, thread=thread_id("sender", "recipient"))
    assert [event.body for event in exchange] == ["Please review this file", "Peer file available"]
    assert exchange[-1].event_id == stored[-1]["display_metadata"]["projection"]["event_id"]
    assert exchange[-1].attachments[0]["artifact_id"] == child_record.artifact_id
    assert all("display_metadata" not in message for message in wire)


@pytest.mark.parametrize("change", ["sender", "source", "recipient", "hash", "staging_symlink"])
def test_recipient_rechecks_grant_and_bytes(pair, tmp_path, change):
    root, sender, recipient, agent, target, record = pair
    metadata = envelope(authorize(agent, [record.artifact_id]))
    if change == "source":
        metadata["projection"]["source_session_id"] = "foreign"
    elif change in {"sender", "recipient"}:
        metadata["projection"][change] = "default"
    elif change == "hash":
        metadata["attachments"][0]["sha256"] = hashlib.sha256(b"spoof").hexdigest()
    else:
        (recipient / "attachments").symlink_to(tmp_path)
    with pytest.raises((ValueError, OSError)):
        recipient_context(target, metadata)
