"""Native hosted-room events and task payloads retain original ordered media."""
import threading
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from gateway import hosted_rooms, hosted_room_driver as driver
from tui_gateway import attachment_batches as batches
from tui_gateway.hosted_room_service import HostedRoomService
from tui_gateway.hosted_room_server_rpc import HostedRoomServerRPC, HostedRoomSessionError
from tests.tui_gateway.test_attachment_batches import item, mixed


def service_at(home):
    home.mkdir(exist_ok=True)
    server = SimpleNamespace(_methods={}, _sessions={}, _sessions_lock=threading.Lock(), _hermes_home=home)
    service = HostedRoomService(server, db_path=home / "rooms.db")
    service.local_profiles = lambda: ("alpha", "beta")
    return service


def create_room(service, room="room-one"):
    return service.create_room(room_id=room, name="Synthetic room", members=[
        {"member_id": p, "profile": p, "handle": p} for p in ("alpha", "beta")])


def sending(service, mid, items, room="room-one", caption="Audio caption"):
    return service.send(room_id=room, event_id=hosted_rooms.user_event_id(mid),
                        payload={"text": caption, "thread_id": "thread-one"},
                        attachment_batch={"batch_id": mid, "items": items})


def test_native_event_retry_after_restart_is_one_event_and_one_artifact_batch(tmp_path):
    service = service_at(tmp_path); create_room(service)
    entries = mixed() + [item(b"original audio bytes", "note.m4a", mime="audio/mp4")]
    mid = str(uuid4())
    first = sending(service, mid, entries)
    restarted = service_at(tmp_path)
    replay = sending(restarted, mid, entries)
    assert first["seq"] == replay["seq"] and replay["idempotent"]
    events = [e for e in restarted._events("room-one") if e["kind"] == "message.user"]
    assert len(events) == 1
    references = events[0]["payload"]["attachments"]
    assert events[0]["payload"]["text"] == "Audio caption"
    assert [r["filename"] for r in references] == [i["filename"] for i in entries]
    assert all(set(r) == {"artifact_id", "filename", "mime_type", "size_bytes"} for r in references)
    tasks = driver.list_tasks(service.db_path, room_id="room-one", status="queued")
    assert len(tasks) == 1 and tasks[0]["payload"]["attachments"] == references
    assert "original audio bytes" not in tasks[0]["payload"]["prompt"]
    for profile in ("alpha", "beta"):
        target = tmp_path / profile; target.mkdir()
        prompt, context = batches.room_turn_context(tmp_path, target, "room-one", references, "Native room prompt")
        assert context["caption"] == "Native room prompt" and len(context["image_paths"]) == 1
        assert prompt.index("notes.txt") < prompt.index("photo.png") < prompt.index("last.txt") < prompt.index("note.m4a")
        audio = list((target / "attachments").rglob("*.m4a"))
        assert len(audio) == 1 and audio[0].read_bytes() == b"original audio bytes"


def test_same_text_distinct_batches_and_changed_ack_retry_are_not_conflated(tmp_path):
    service = service_at(tmp_path); create_room(service)
    ids = [str(uuid4()), str(uuid4())]; entries = mixed()
    results = [sending(service, mid, entries, caption="same") for mid in ids]
    assert results[0]["seq"] != results[1]["seq"]
    with pytest.raises(hosted_rooms.EventConflictError):
        sending(service, ids[0], entries, caption="changed")
    assert len([e for e in service._events("room-one") if e["kind"] == "message.user"]) == 2


def test_cross_room_artifact_reference_refused_before_event_append(tmp_path):
    service = service_at(tmp_path); create_room(service); create_room(service, "room-two")
    event = sending(service, str(uuid4()), mixed())
    before = service._events("room-two")
    with pytest.raises(batches.BatchError):
        service.send(room_id="room-two", event_id="foreign", payload={"text": "caption",
                     "thread_id": "thread", "attachments": event["payload"]["attachments"]})
    assert service._events("room-two") == before


def test_invalid_mixed_batch_has_no_artifacts_or_events(tmp_path):
    service = service_at(tmp_path); create_room(service)
    entries = mixed(); entries[-1]["data_base64"] = "bad!"
    with pytest.raises(batches.BatchError):
        sending(service, str(uuid4()), entries)
    conn = batches._connect(tmp_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM published_artifacts").fetchone()[0] == 0
        assert conn.execute("SELECT state FROM attachment_batches").fetchone()[0] == "refused"
    finally:
        conn.close()
    assert not service._events("room-one")


def test_room_upload_refusal_names_the_item_and_seals_that_reason(tmp_path):
    """A room upload gets the same named sentence a chat upload does."""
    import json
    service = service_at(tmp_path); create_room(service)
    entries = mixed(); entries[-1]["data_base64"] = "bad!"
    mid = str(uuid4())
    with pytest.raises(batches.RefusedBatch) as caught:
        sending(service, mid, entries)
    proof = caught.value
    refusal = proof.data["attachment_batch"]["refusal"]
    assert refusal["filename"] == entries[-1]["filename"] == "last.txt"
    assert refusal["item_id"] == entries[-1]["item_id"]
    assert refusal["code"] == "data_corrupt" and str(proof) == refusal["reason"]
    assert str(proof) != batches.GENERIC_REFUSAL
    conn = batches._connect(tmp_path)
    try:
        row = conn.execute("SELECT receipt FROM attachment_batches WHERE batch_id=?", (mid,)).fetchone()
    finally:
        conn.close()
    assert json.loads(row["receipt"])["refusal"] == refusal, "the tombstone kept the reason"
    assert not service._events("room-one")


def test_room_resend_of_a_sealed_identity_names_no_stale_file(tmp_path):
    service = service_at(tmp_path); create_room(service)
    entries = mixed(); entries[-1]["data_base64"] = "bad!"
    mid = str(uuid4())
    with pytest.raises(batches.BatchError):
        sending(service, mid, entries)
    with pytest.raises(batches.BatchError) as caught:
        sending(service, mid, entries[:-1])
    assert "last.txt" not in str(caught.value)
    assert "new message" in str(caught.value)
    assert not service._events("room-one")


def test_registered_groups_send_accepts_original_audio_with_empty_caption(tmp_path, monkeypatch):
    from tui_gateway import server
    service = service_at(tmp_path); create_room(service)
    monkeypatch.setattr(server, "get_hosted_room_service", lambda: service)
    mid = str(uuid4())
    result = server._methods["groups.send"]("r", {"room_id": "room-one", "event_id": mid.upper(),
        "payload": {"text": "", "thread_id": "thread"},
        "attachment_batch": {"batch_id": mid, "items": [item(b"audio", "note.m4a", mime="audio/mp4")]}})
    assert result["result"]["accepted"], result
    event = result["result"]["event"]
    assert event["payload"]["text"] == "" and len(event["payload"]["attachments"]) == 1


def test_native_room_rpc_passes_owned_context_and_rejects_symlink_execution_root(tmp_path):
    service = service_at(tmp_path); create_room(service)
    event = sending(service, str(uuid4()), mixed())
    target = tmp_path / "alpha"; target.mkdir()
    session = {"session_key": "canonical-alpha", "profile_home": str(target)}
    service.server._sessions["handle"] = session
    captured = []
    service.server._submit_prompt = lambda rid, params, context: captured.append((params, context)) or {"result": {"status": "streaming"}}
    rpc = HostedRoomServerRPC(service.server)
    identity = driver.TaskIdentity("room-one", "task", "thread", "turn")
    args = dict(profile="alpha", session_id="handle", prompt="Policy prompt", source="bot_room",
                task=identity, execution_generation=1, on_terminal=lambda r: None,
                attachments=event["payload"]["attachments"])
    assert rpc.submit(**args)["status"] == "streaming"
    assert captured[0][1]["caption"] == "Policy prompt"
    assert len(captured[0][1]["image_paths"]) == 1
    assert "@file:" in captured[0][0]["text"] and "@file:" not in captured[0][1]["caption"]
    other = tmp_path / "beta"; other.mkdir(); (other / "attachments").symlink_to(target / "attachments")
    session["profile_home"] = str(other)
    with pytest.raises(HostedRoomSessionError) as caught:
        rpc.submit(**args)
    assert caught.value.not_admitted is True and len(captured) == 1


def test_real_room_runtime_dispatches_all_original_inputs_to_each_local_member(tmp_path):
    from tests.tui_gateway.test_hosted_room_service import _FakeRPC, _wait_for
    service = service_at(tmp_path); create_room(service)
    calls = []
    class RPC(_FakeRPC):
        def submit(self, *, profile, session_id, prompt, source, task,
                   execution_generation, on_terminal, attachments=None):
            target = tmp_path / profile
            target.mkdir(exist_ok=True)
            model_input, context = batches.room_turn_context(tmp_path, target, task.room_id, attachments, prompt)
            calls.append((profile, list(attachments), model_input, context))
            on_terminal({"status": "settled", "text": "PASS"})
            return {"accepted": True}
    service.rpc = RPC(); service.runtime.rpc = service.rpc
    entries = mixed() + [item(b"voice original", "voice.m4a", mime="audio/mp4")]
    service.start()
    try:
        event = sending(service, str(uuid4()), entries)
        _wait_for(lambda: len(calls) == 2, timeout=5)
        assert {c[0] for c in calls} == {"alpha", "beta"}
        assert all(c[1] == event["payload"]["attachments"] for c in calls)
        assert all(len(c[3]["image_paths"]) == 1 and "voice.m4a" in c[2] for c in calls)
        assert all("@file:" not in c[3]["caption"] for c in calls)
    finally:
        assert service.stop(timeout=2)


def test_registered_batch_capability_negotiates_the_actual_limits():
    from tui_gateway import server
    result = server._methods["attachments.capabilities"]("r", {})["result"]
    assert result["request_bound_batches"] == 1 and result["groups_local"]
    assert result["max_items"] == batches.MAX_ITEMS and not result["groups_peer"]
