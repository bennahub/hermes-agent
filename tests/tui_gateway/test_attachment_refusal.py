"""Real native index/room/RPC paths, without provider calls or live HOME."""
import concurrent.futures
import json
import multiprocessing
import subprocess
import sys
import threading
from uuid import uuid4

import pytest
from tui_gateway import attachment_batches as batches
from tests.tui_gateway.test_attachment_batches import item, mixed, rpc
from tests.tui_gateway.test_room_attachment_batches import service_at, create_room, sending


def _process_racer(home, mid, entries, barrier, output, seal):
    barrier.wait(timeout=10)
    if seal:
        output.put(("proof", batches.seal_refusal(home, "chat", mid, mid) is not None))
    else:
        try:
            batches.stage(home, "alpha", "chat", mid, mid, entries)
            result = batches.submit(home, "chat", mid, mid, {}, lambda *a: {"result": {"status": "queued"}})
            output.put(("admitted", result["delivery_state"] == "accepted"))
        except batches.RefusedBatch:
            output.put(("admitted", False))


def test_separate_gateway_processes_use_the_native_sqlite_claim(tmp_path):
    mid = str(uuid4()); entries = [item()]
    batches.stage(tmp_path, "alpha", "chat", mid, mid, entries)
    ctx = multiprocessing.get_context("spawn")
    barrier, output = ctx.Barrier(2), ctx.Queue()
    processes = [ctx.Process(target=_process_racer, args=(str(tmp_path), mid, entries, barrier, output, seal))
                 for seal in (False, True)]
    try:
        for process in processes:
            process.start()
        answers = dict(output.get(timeout=20) for _ in processes)
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        assert answers["proof"] != answers["admitted"]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate(); process.join(timeout=5)
        output.close()


def test_terminal_tombstone_is_read_by_fresh_interpreter(tmp_path):
    mid = str(uuid4())
    proof = batches.seal_refusal(tmp_path, "chat", mid, mid)
    script = """
import json, sys
from tui_gateway import attachment_batches as b
try:
    b.submit(sys.argv[1], 'chat', sys.argv[2], sys.argv[2], {}, lambda *a: (_ for _ in ()).throw(AssertionError('dispatch')))
except b.RefusedBatch as exc:
    print(json.dumps(exc.data))
else:
    raise AssertionError('missing terminal refusal')
"""
    child = subprocess.run([sys.executable, "-c", script, str(tmp_path), mid],
                           capture_output=True, text=True, timeout=15, check=True)
    assert json.loads(child.stdout) == proof.data


def test_registered_partial_invalid_seals_then_reconfigure_once(rpc):
    server, session = rpc
    mid = str(uuid4()); entries = mixed(); entries[-1]["data_base64"] = "invalid!"
    params = dict(session_id="ui", text="caption", client_message_id=mid,
                  attachment_batch={"batch_id": mid, "items": entries})
    result = server._methods["prompt.submit"]("r", params)
    proof = result["error"]["data"]["attachment_batch"]
    refusal = proof.get("refusal")
    assert {k: v for k, v in proof.items() if k != "refusal"} == dict(
        schema_version=1, batch_id=mid, client_message_id=mid,
        admission="refused", retry_with_new_identity=True)
    # The refusal names the one item the Owner has to change, not the message.
    assert refusal["item_id"] == entries[-1]["item_id"]
    assert refusal["filename"] == entries[-1]["filename"] == "last.txt"
    assert refusal["code"] == "data_corrupt" and "last.txt" in refusal["reason"]
    assert result["error"]["message"] == refusal["reason"]
    home = session["profile_home"]
    conn = batches._connect(home)
    assert conn.execute("SELECT COUNT(*) FROM published_artifacts").fetchone()[0] == 0
    conn.close()
    # Old identity remains refused even with corrected bytes. New identity works.
    params["attachment_batch"]["items"] = entries[:-1]
    retried = server._methods["prompt.submit"]("retry", params)["error"]["data"]["attachment_batch"]
    assert {k: v for k, v in retried.items() if k != "refusal"} == \
           {k: v for k, v in proof.items() if k != "refusal"}
    # "last.txt" is gone from this submission, so the reply must not name it.
    replayed = retried["refusal"]
    assert replayed["item_id"] is None and replayed["filename"] is None
    assert replayed["code"] == "identity_refused"
    assert "last.txt" not in replayed["reason"]
    assert "new message" in replayed["reason"]
    new = str(uuid4()); calls = []
    batches.stage(home, "alpha", "chat", new, new, entries[:-1])
    for _ in range(2):
        batches.submit(home, "chat", new, new, {}, lambda *a: calls.append(a) or {"result": {"status": "queued"}})
    assert len(calls) == 1


@pytest.mark.parametrize("prestage", [False, True])
def test_concurrent_claim_and_terminal_seal_have_only_one_winner(tmp_path, prestage):
    mid = str(uuid4()); entries = [item()]; calls = []; barrier = threading.Barrier(2)
    if prestage:
        batches.stage(tmp_path, "alpha", "chat", mid, mid, entries)
    def admit():
        barrier.wait(timeout=5)
        try:
            batches.stage(tmp_path, "alpha", "chat", mid, mid, entries)
            batches.submit(tmp_path, "chat", mid, mid, {}, lambda *a: calls.append(a) or {"result": {"status": "queued"}})
        except batches.RefusedBatch:
            pass
    def refuse():
        barrier.wait(timeout=5)
        return batches.seal_refusal(tmp_path, "chat", mid, mid)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        a, b = pool.submit(admit), pool.submit(refuse)
        a.result(timeout=10); proof = b.result(timeout=10)
    assert bool(calls) != bool(proof)
    assert len(calls) <= 1


def test_refusal_survives_real_module_reload_and_cross_session_cannot_seal(tmp_path):
    import importlib
    mid = str(uuid4()); entries = [item()]
    batches.stage(tmp_path, "alpha", "chat", mid, mid, entries)
    assert batches.seal_refusal(tmp_path, "foreign", mid, mid) is None
    proof = batches.seal_refusal(tmp_path, "chat", mid, mid)
    restarted = importlib.reload(batches)
    with pytest.raises(restarted.RefusedBatch) as caught:
        restarted.stage(tmp_path, "alpha", "chat", mid, mid, entries)
    assert caught.value.data == proof.data
    with pytest.raises(restarted.RefusedBatch):
        restarted.submit(tmp_path, "chat", mid, mid, {}, lambda *a: pytest.fail("dispatched refused UUID"))


@pytest.mark.parametrize("state", ["dispatching", "accepted", "completed", "failed", "outcome_unknown"])
def test_no_post_dispatch_state_can_mint_refusal(tmp_path, state):
    mid = str(uuid4()); batches.stage(tmp_path, "alpha", "chat", mid, mid, [item()])
    execution = tmp_path / "attachments" / f"batch-{mid}"
    execution.mkdir(parents=True)
    admitted_copy = execution / "admitted-copy.txt"
    admitted_copy.write_bytes(b"keep admitted execution data")
    conn = batches._connect(tmp_path)
    conn.execute("UPDATE attachment_batches SET state=?", (state,)); conn.commit(); conn.close()
    assert batches.seal_refusal(tmp_path, "chat", mid, mid) is None
    assert admitted_copy.read_bytes() == b"keep admitted execution data"
    with batches._connect(tmp_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM published_artifacts").fetchone()[0] == 1


def test_pre_upgrade_generic_error_staged_row_is_still_uncertain(tmp_path):
    mid = str(uuid4()); batches.stage(tmp_path, "alpha", "chat", mid, mid, [item()])
    conn = batches._connect(tmp_path)
    # Exact old error transition: state staged, cleared fingerprint, process retained.
    conn.execute("UPDATE attachment_batches SET process='previous-gateway', submit_fingerprint=NULL")
    conn.commit(); conn.close()
    assert batches.seal_refusal(tmp_path, "chat", mid, mid) is None
    result = batches.submit(tmp_path, "chat", mid, mid, {}, lambda *a: pytest.fail("legacy ambiguous redispatch"))
    assert result["status"] == "outcome_unknown"


def test_accepted_lost_ack_invalid_retry_has_no_typed_refusal(rpc):
    server, session = rpc
    mid = str(uuid4()); entries = [item()]
    params = dict(session_id="ui", text="caption", client_message_id=mid,
                  attachment_batch={"batch_id": mid, "items": entries})
    assert "result" in server._methods["prompt.submit"]("r", params)
    entries[0]["data_base64"] = "bad!"
    error = server._methods["prompt.submit"]("retry", params)["error"]
    assert not error.get("data")


def test_room_post_append_failure_cannot_refuse_and_retry_returns_native_event(tmp_path, monkeypatch):
    service = service_at(tmp_path); create_room(service)
    mid = str(uuid4()); entries = mixed(); original = service.prepare_room
    monkeypatch.setattr(service, "prepare_room", lambda *a: (_ for _ in ()).throw(batches.BatchError("lost response")))
    with pytest.raises(batches.BatchError) as caught:
        sending(service, mid, entries)
    assert not isinstance(caught.value, batches.RefusedBatch)
    assert batches.seal_refusal(tmp_path, "room:room-one", mid, mid) is None
    monkeypatch.setattr(service, "prepare_room", original)
    assert sending(service, mid, entries)["idempotent"]
    assert len([e for e in service._events("room-one") if e["kind"] == "message.user"]) == 1


def test_registered_room_invalid_batch_has_bound_proof(tmp_path, monkeypatch):
    from tui_gateway import server
    service = service_at(tmp_path); create_room(service)
    monkeypatch.setattr(server, "get_hosted_room_service", lambda: service)
    mid = str(uuid4()); entries = [item()]; entries[0]["data_base64"] = "bad!"
    response = server._methods["groups.send"]("r", dict(room_id="room-one", event_id=mid,
        payload={"text": "caption", "thread_id": "thread"}, attachment_batch={"batch_id": mid, "items": entries}))
    assert response["error"]["data"]["attachment_batch"]["batch_id"] == mid
    assert not service._events("room-one")


def test_legacy_room_append_with_staged_batch_cannot_receive_refusal(tmp_path):
    service = service_at(tmp_path); create_room(service)
    mid = str(uuid4()); entries = mixed()
    # Native pre-upgrade sequence: stage+append without the new claim wrapper.
    from gateway import hosted_rooms
    service._send(room_id="room-one", event_id=hosted_rooms.user_event_id(mid),
                  payload={"text": "caption", "thread_id": "thread-one"},
                  attachment_batch={"batch_id": mid, "items": entries})
    entries[-1]["data_base64"] = "bad!"
    with pytest.raises(batches.BatchError) as caught:
        sending(service, mid, entries, caption="caption")
    assert not isinstance(caught.value, batches.RefusedBatch)
    assert len([e for e in service._events("room-one") if e["kind"] == "message.user"]) == 1


def test_room_invalid_and_valid_concurrent_requests_never_refuse_an_appended_event(tmp_path):
    service = service_at(tmp_path); create_room(service)
    mid = str(uuid4()); entries = mixed(); broken = [dict(i) for i in entries]
    broken[-1]["data_base64"] = "bad!"
    barrier = threading.Barrier(2)
    def run(items):
        barrier.wait(timeout=5)
        try:
            return sending(service, mid, items)
        except batches.BatchError as exc:
            return exc
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        a, b = pool.submit(run, entries), pool.submit(run, broken)
        valid, invalid = a.result(timeout=10), b.result(timeout=10)
    events = [e for e in service._events("room-one") if e["kind"] == "message.user"]
    if isinstance(invalid, batches.RefusedBatch):
        assert not events and isinstance(valid, batches.RefusedBatch)
    else:
        assert isinstance(invalid, batches.BatchError)
        assert len(events) == 1 and isinstance(valid, dict)
