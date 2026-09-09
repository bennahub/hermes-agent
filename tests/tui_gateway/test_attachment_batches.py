"""Native artifact bytes, durable admissions and registered RPC queue isolation."""
import base64
import contextlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image
from tui_gateway import attachment_batches as batches


def item(data=b"document", name="notes.txt", kind="file", mime="text/plain"):
    return dict(item_id=str(uuid4()), filename=name, kind=kind, mime_type=mime,
                data_base64=base64.b64encode(data).decode())


def mixed():
    stream = BytesIO()
    Image.new("RGB", (2, 2), "red").save(stream, format="PNG")
    return [item(), item(stream.getvalue(), "photo.png", "image", "image/png"), item(b"last", "last.txt")]


def test_native_mixed_batch_unknown_ack_retry_preserves_bytes_order_and_safe_ids(tmp_path):
    mid = str(uuid4()); items = mixed(); calls = []
    first = batches.stage(tmp_path, "alpha", "chat", mid, mid, items)
    second = batches.stage(tmp_path, "alpha", "chat", mid, mid, items)
    assert first == second
    def dispatch(records, paths):
        calls.append(records)
        assert [Path(p).read_bytes() for p in paths] == [base64.b64decode(i["data_base64"]) for i in items]
        return {"result": {"status": "streaming", "client_message_id_supported": True}}
    result = batches.submit(tmp_path, "chat", mid, mid, {"text": "same"}, dispatch)
    retry = batches.submit(tmp_path, "chat", mid, mid, {"text": "same"}, dispatch)
    assert len(calls) == 1 and retry["replayed"] and retry["status"] == result["status"]
    assert [r["filename"] for r in result["attachments"]] == [i["filename"] for i in items]
    assert all("path" not in key for row in result["attachments"] for key in row)
    from gateway.published_artifacts import resolve_blob
    for row in result["attachments"]:
        blob, native = resolve_blob(tmp_path, row["artifact_id"])
        assert native.sha256 == row["sha256"] and blob.is_file()
    batches.complete(tmp_path, mid, "complete")
    assert batches.submit(tmp_path, "chat", mid, mid, {"text": "same"}, dispatch)["delivery_state"] == "completed"


def test_invalid_last_item_has_no_filesystem_effect(tmp_path):
    home = tmp_path / "untouched"
    mid = str(uuid4()); items = mixed(); items[-1]["data_base64"] = "invalid!"
    with pytest.raises(batches.BatchError):
        batches.stage(home, "alpha", "chat", mid, mid, items)
    assert not home.exists()


def test_direct_native_context_reads_upload_outside_project_workspace(tmp_path):
    from agent.context_references import preprocess_context_references
    from tui_gateway import server
    home, project = tmp_path / "profile", tmp_path / "unrelated-project"
    project.mkdir()
    mid = str(uuid4())
    staged = batches.stage(home, "alpha", "chat", mid, mid, [item(b"REAL_UPLOADED_TEXT")])
    def dispatch(records, paths):
        assert Path(paths[0]).is_relative_to(home / "attachments")
        assert records == staged["attachments"]
        prompt = "Review @file:" + paths[0]
        allowed = server._session_attachment_context_paths({"profile_home": str(home)}, prompt, str(project))
        result = preprocess_context_references(prompt, cwd=project, allowed_root=project,
                                               allowed_file_paths=allowed, context_length=32768)
        assert "REAL_UPLOADED_TEXT" in result.message
        assert not result.warnings
        return {"result": {"status": "streaming"}}
    batches.submit(home, "chat", mid, mid, {}, dispatch)


@pytest.mark.parametrize("redirect", ["artifacts", "artifacts/published", "artifacts/published/index.db",
                                     "artifacts/published/index.db-wal", "artifacts/published/index.db-shm",
                                     "artifacts/published/index.db-journal"])
def test_store_redirect_is_refused_before_foreign_write(tmp_path, redirect):
    home, foreign = tmp_path / "profile", tmp_path / "foreign"
    home.mkdir(); foreign.mkdir()
    target = home / redirect
    target.parent.mkdir(parents=True, exist_ok=True)
    destination = foreign if "index.db" not in redirect else foreign / "private.db"
    if destination != foreign:
        destination.write_bytes(b"FOREIGN_OWNER_BYTES")
    target.symlink_to(destination, target_is_directory=destination == foreign)
    before = {str(p.relative_to(foreign)): p.read_bytes() for p in foreign.rglob("*") if p.is_file()}
    mid = str(uuid4())
    with pytest.raises(batches.BatchError):
        batches.stage(home, "alpha", "chat", mid, mid, [item()])
    assert {str(p.relative_to(foreign)): p.read_bytes() for p in foreign.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("target_kind", ["blob", "execution_root", "fifo"])
def test_redirect_after_staging_refuses_admission_and_preserves_foreign_bytes(tmp_path, target_kind):
    home, foreign = tmp_path / "profile", tmp_path / "foreign"
    foreign.mkdir()
    mid = str(uuid4())
    staged = batches.stage(home, "alpha", "chat", mid, mid, [item()])
    if target_kind in {"blob", "fifo"}:
        target = batches.artifacts._blob_path(home, staged["attachments"][0]["artifact_id"])
        target.unlink()
        destination = foreign / "private.txt"
        destination.write_bytes(b"document")
    else:
        target, destination = home / "attachments", foreign
    if target_kind == "fifo":
        os.mkfifo(target)
    else:
        target.symlink_to(destination, target_is_directory=target_kind == "execution_root")
    with pytest.raises((batches.BatchError, OSError)):
        batches.submit(home, "chat", mid, mid, {}, lambda *args: pytest.fail("unsafe admission"))
    assert sorted(p.name for p in foreign.iterdir()) == (["private.txt"] if target_kind in {"blob", "fifo"} else [])
    with batches._connect(home) as conn:
        assert conn.execute("SELECT state FROM attachment_batches").fetchone()[0] == "staged"


def test_concurrent_sessions_never_claim_each_others_uploads(tmp_path):
    ids = [str(uuid4()), str(uuid4())]
    def stage(index):
        return batches.stage(tmp_path, "alpha", f"chat-{index}", ids[index], ids[index], [item(str(index).encode())])
    with ThreadPoolExecutor(2) as pool:
        staged = list(pool.map(stage, range(2)))
    assert staged[0]["attachments"][0]["artifact_id"] != staged[1]["attachments"][0]["artifact_id"]
    with pytest.raises(batches.BatchError):
        batches.submit(tmp_path, "chat-1", ids[0], ids[0], {}, lambda *a: pytest.fail("cross-session dispatch"))


def test_same_caption_distinct_id_is_two_turns_but_concurrent_retry_is_one(tmp_path):
    ids = [str(uuid4()), str(uuid4())]; calls = []
    for mid in ids:
        batches.stage(tmp_path, "alpha", "chat", mid, mid, [item()])
    def run(mid):
        return batches.submit(tmp_path, "chat", mid, mid, {"text": "same"},
            lambda *a: calls.append(mid) or {"result": {"status": "queued"}})
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(run, [ids[0], ids[0], ids[1], ids[1]]))
    assert sorted(calls) == sorted(ids)


def test_dispatch_ambiguity_is_not_replayed_and_changed_payload_refused(tmp_path, monkeypatch):
    mid = str(uuid4()); batches.stage(tmp_path, "alpha", "chat", mid, mid, [item()])
    def lost(*args):
        raise RuntimeError("connection lost after dispatch")
    with pytest.raises(RuntimeError):
        batches.submit(tmp_path, "chat", mid, mid, {"text": "one"}, lost)
    monkeypatch.setattr(batches, "_PROCESS", "restarted")
    assert batches.submit(tmp_path, "chat", mid, mid, {"text": "one"}, lost)["status"] == "outcome_unknown"
    with pytest.raises(batches.BatchError):
        batches.submit(tmp_path, "chat", mid, mid, {"text": "different"}, lost)


def test_untyped_admission_error_remains_uncertain_without_redispatch(tmp_path):
    mid = str(uuid4()); batches.stage(tmp_path, "alpha", "chat", mid, mid, [item()])
    response = batches.submit(tmp_path, "chat", mid, mid, {}, lambda *a: {"error": {"code": 4090}})
    assert response["error"]["code"] == 4090
    assert batches.seal_refusal(tmp_path, "chat", mid, mid) is None
    assert batches.submit(tmp_path, "chat", mid, mid, {}, lambda *a: pytest.fail("redispatched"))["status"] == "outcome_unknown"


def test_partial_materialization_failure_rolls_back_native_rows_and_owned_bytes(tmp_path, monkeypatch):
    original = Path.open
    created = []
    def opening(path, mode="r", *args, **kwargs):
        if mode == "xb":
            created.append(path)
            if len(created) == 2:
                raise OSError("synthetic disk failure")
        return original(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "open", opening)
    mid = str(uuid4())
    with pytest.raises(OSError):
        batches.stage(tmp_path, "alpha", "chat", mid, mid, mixed())
    assert not any(path.exists() for path in created)
    conn = batches._connect(tmp_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM published_artifacts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM attachment_batches").fetchone()[0] == 0
    finally:
        conn.close()


def test_completed_native_receipt_survives_process_change_without_replay(tmp_path, monkeypatch):
    mid = str(uuid4()); batches.stage(tmp_path, "alpha", "chat", mid, mid, [item()])
    batches.submit(tmp_path, "chat", mid, mid, {}, lambda *a: {"result": {"status": "streaming"}})
    batches.complete(tmp_path, mid, "complete")
    monkeypatch.setattr(batches, "_PROCESS", "new-process")
    replay = batches.submit(tmp_path, "chat", mid, mid, {}, lambda *a: pytest.fail("replayed a completed turn"))
    assert replay["delivery_state"] == "completed" and replay["status"] == "streaming"


@pytest.fixture
def rpc(tmp_path, monkeypatch):
    from tui_gateway import server
    session = dict(session_key="chat", profile_home=str(tmp_path), agent=SimpleNamespace(),
                   history_lock=threading.Lock(), history=[], history_version=0,
                   attached_images=["another-clients-legacy-image"], running=True)
    monkeypatch.setattr(server, "_sess_nowait", lambda *a: (session, None))
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a: False)
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {})
    monkeypatch.setattr(server, "_session_info", lambda *a: {"profile_name": "alpha"})
    @contextlib.contextmanager
    def db(*args):
        yield SimpleNamespace(_session_turn_lease_key=lambda key: key)
    monkeypatch.setattr(server, "_session_db", db)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a: None)
    monkeypatch.setattr(server, "_legacy_group_fence_error", lambda *a: None)
    monkeypatch.setattr(server, "_typed_stop_phrase_response", lambda *a: None)
    monkeypatch.setattr(server, "current_transport", lambda: None)
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "interrupt")
    return server, session


def test_registered_prompt_mixed_busy_queue_claims_only_its_batch(rpc):
    server, session = rpc
    mid = str(uuid4()); entries = mixed()
    params = dict(session_id="ui", text="caption", client_message_id=mid,
                  attachment_batch={"batch_id": mid, "items": entries})
    first = server._methods["prompt.submit"]("one", params)
    second = server._methods["prompt.submit"]("retry", params)
    assert first["result"]["status"] == "queued" and second["result"]["replayed"]
    assert session["attached_images"] == ["another-clients-legacy-image"]
    envelope = session["queued_prompt"]
    assert not session.get("queued_prompts")
    assert envelope["persist_text"] == "caption"
    assert len(envelope["image_paths"]) == 1
    assert [r["filename"] for r in envelope["display_metadata"]["attachments"]] == [i["filename"] for i in entries]
    text = envelope["text"]
    assert text.index("1. notes.txt") < text.index("2. photo.png") < text.index("3. last.txt")
    assert envelope["display_metadata"]["client_message_ids"] == [mid]


def test_voice_batch_persists_explicit_canonical_transcript(rpc):
    server, session = rpc
    mid, item_id = str(uuid4()), str(uuid4())
    transcript = {
        "version": 1, "text": "راجع الأرقام", "audio_item_id": item_id,
        "source": "hermes_stt", "provider": "openai", "model": "whisper-1", "language": "ar",
    }
    audio = item(b"audio bytes", "voice.m4a", "file", "audio/mp4")
    audio["item_id"] = item_id
    document = item(b"document", "notes.txt", "file", "text/plain")
    result = server._methods["prompt.submit"]("voice", {
        "session_id": "ui", "text": "راجع الأرقام", "client_message_id": mid,
        "attachment_batch": {"batch_id": mid, "items": [document, audio], "transcript": transcript},
    })
    assert result["result"]["status"] == "queued"
    metadata = session["queued_prompt"]["display_metadata"]
    assert [item["filename"] for item in metadata["attachments"]] == ["notes.txt", "voice.m4a"]
    assert metadata["transcript"]["audio_item_id"] == item_id
    assert metadata["transcript"]["text"] == transcript["text"]
    assert metadata["transcript"]["audio_artifact_id"] == metadata["attachments"][1]["artifact_id"]


def test_voice_batch_rejects_unbound_or_different_transcript(rpc):
    server, session = rpc
    for text, use_real_item, artifact_id in (
        ("different words", True, None),
        ("راجع الأرقام", False, None),
        ("راجع الأرقام", True, "mismatched-artifact"),
    ):
        mid = str(uuid4())
        audio = item(b"audio bytes", "voice.m4a", "file", "audio/mp4")
        transcript_item_id = audio["item_id"] if use_real_item else str(uuid4())
        params = {
            "session_id": "ui", "text": "راجع الأرقام", "client_message_id": mid,
            "attachment_batch": {"batch_id": mid, "items": [audio], "transcript": {
                "version": 1, "text": text, "audio_item_id": transcript_item_id,
                **({"audio_artifact_id": artifact_id} if artifact_id else {}),
                "source": "hermes_stt",
            }},
        }
        result = server._methods["prompt.submit"]("voice-bad", params)
        assert result["error"]["code"] == 4004
        proof = result["error"]["data"]["attachment_batch"]
        assert proof["admission"] == "refused" and proof["retry_with_new_identity"]
        with batches._connect(Path(session["profile_home"])) as conn:
            row = conn.execute(
                "SELECT state,records FROM attachment_batches WHERE batch_id=?", (mid,)).fetchone()
            assert row["state"] == "refused" and json.loads(row["records"]) == []
            assert conn.execute("SELECT COUNT(*) FROM published_artifacts").fetchone()[0] == 0
        retry = server._methods["prompt.submit"]("voice-bad-retry", params)
        assert retry["error"]["data"]["attachment_batch"] == proof

    mid = str(uuid4())
    audio = item(b"audio bytes", "voice.m4a", "file", "audio/mp4")
    artifact_only = server._methods["prompt.submit"]("voice-artifact-only", {
        "session_id": "ui", "text": "راجع الأرقام", "client_message_id": mid,
        "attachment_batch": {"batch_id": mid, "items": [audio], "transcript": {
            "version": 1, "text": "راجع الأرقام",
            "audio_artifact_id": "client-cannot-author-this",
            "source": "hermes_stt",
        }},
    })
    assert artifact_only["error"]["code"] == 4004
    assert artifact_only["error"]["data"]["attachment_batch"]["admission"] == "refused"


def test_partial_execution_copy_failure_is_transactional_refusal(rpc, monkeypatch):
    server, session = rpc
    mid = str(uuid4())
    entries = [item(b"first", "first.txt"), item(b"second", "second.txt")]
    original = Path.open
    writes = 0

    def opening(path, mode="r", *args, **kwargs):
        nonlocal writes
        if mode == "xb" and "batch-" in str(path):
            writes += 1
            if writes == 2:
                raise OSError("synthetic second-copy failure")
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", opening)
    response = server._methods["prompt.submit"]("partial-copy", {
        "session_id": "ui", "text": "read both", "client_message_id": mid,
        "attachment_batch": {"batch_id": mid, "items": entries},
    })
    proof = response["error"]["data"]["attachment_batch"]
    assert proof["admission"] == "refused"
    assert "queued_prompt" not in session
    execution_root = Path(session["profile_home"]) / "attachments" / f"batch-{mid}"
    assert not execution_root.exists() or not any(execution_root.iterdir())
    with batches._connect(Path(session["profile_home"])) as conn:
        row = conn.execute(
            "SELECT state,records,process FROM attachment_batches WHERE batch_id=?", (mid,)).fetchone()
        assert row["state"] == "refused" and json.loads(row["records"]) == []
        assert row["process"] is None
        assert conn.execute("SELECT COUNT(*) FROM published_artifacts").fetchone()[0] == 0


def test_retry_after_crash_between_execution_copies_removes_old_orphan(rpc, monkeypatch):
    server, session = rpc
    home = Path(session["profile_home"])
    mid = str(uuid4())
    entries = [item(b"first", "first.txt"), item(b"second", "second.txt")]
    staged = batches.stage(home, "alpha", "chat", mid, mid, entries)
    original = Path.open
    writes = 0

    class SimulatedProcessDeath(BaseException):
        pass

    def crash_between_files(path, mode="r", *args, **kwargs):
        nonlocal writes
        if mode == "xb" and "batch-" in str(path):
            writes += 1
            if writes == 2:
                raise SimulatedProcessDeath()
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", crash_between_files)
    with pytest.raises(SimulatedProcessDeath):
        batches.submit(home, "chat", mid, mid, {"text": "read both"}, lambda *_: pytest.fail("dispatched"))

    execution_root = home / "attachments" / f"batch-{mid}"
    old_copy = execution_root / (staged["attachments"][0]["artifact_id"] + ".txt")
    assert old_copy.read_bytes() == b"first"
    with batches._connect(home) as conn:
        crashed = conn.execute(
            "SELECT state,process FROM attachment_batches WHERE batch_id=?", (mid,)).fetchone()
        assert crashed["state"] == "staged" and crashed["process"] is None

    second_artifact = staged["attachments"][1]["artifact_id"]

    def fail_second_copy(path, mode="r", *args, **kwargs):
        if mode == "xb" and path.name.startswith(second_artifact):
            raise OSError("retry cannot create second copy")
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_second_copy)
    response = server._methods["prompt.submit"]("crash-retry", {
        "session_id": "ui", "text": "read both", "client_message_id": mid,
        "attachment_batch": {"batch_id": mid, "items": entries},
    })
    assert response["error"]["data"]["attachment_batch"]["admission"] == "refused"
    assert "queued_prompt" not in session
    assert not execution_root.exists()
    with batches._connect(home) as conn:
        row = conn.execute(
            "SELECT state,records,process FROM attachment_batches WHERE batch_id=?", (mid,)).fetchone()
        assert row["state"] == "refused" and json.loads(row["records"]) == []
        assert row["process"] is None
        assert conn.execute("SELECT COUNT(*) FROM published_artifacts").fetchone()[0] == 0


def test_file_only_busy_batches_do_not_merge_or_steal_legacy_images(rpc, monkeypatch):
    server, session = rpc
    for _ in range(2):
        mid = str(uuid4())
        result = server._methods["prompt.submit"]("r", dict(session_id="ui", text="same",
            client_message_id=mid, attachment_batch={"batch_id": mid, "items": [item()]}))
        assert result["result"]["status"] == "queued"
    assert len(session["queued_prompts"]) == 1
    assert session["queued_prompt"]["image_paths"] == []
    assert session["attached_images"] == ["another-clients-legacy-image"]
    captured = []
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *args, **kwargs: captured.append(kwargs) or True)
    session["running"] = False
    assert server._drain_queued_prompt("r", "ui", session)
    assert captured[0]["image_paths"] == [] and captured[0]["persist_text"] == "same"
    assert len(captured[0]["display_metadata"]["attachments"]) == 1


def test_private_filename_path_is_never_a_display_name(tmp_path):
    mid = str(uuid4())
    result = batches.stage(tmp_path, "alpha", "chat", mid, mid, [item(name="C:\\owner\\secret\\notes.txt")])
    assert result["attachments"][0]["filename"] == "notes.txt"


def test_image_chosen_as_document_still_gets_validated_and_native_image_kind(tmp_path):
    mid = str(uuid4()); image = mixed()[1]; image["kind"] = "file"
    result = batches.stage(tmp_path, "alpha", "chat", mid, mid, [image])
    assert result["attachments"][0]["kind"] == "image"


def _offline_attachment_scope(monkeypatch):
    """Only auxiliary provider I/O is offline; current ingress and DB stay real."""
    from types import SimpleNamespace
    def policy(**kwargs):
        assert kwargs["task"] == "execution_scope"
        payload = json.loads(kwargs["messages"][-1]["content"])
        assert payload["original_instruction"] == "Owner caption"
        assert "invocation" not in payload  # These projection turns do not execute tools.
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
            "objective": "Respond to Owner caption", "permitted": ["Respond without tools"],
            "excluded": ["Unrelated historical commands"],
        })))])
    monkeypatch.setattr("agent.auxiliary_client.call_llm", policy)


def test_real_agent_queue_drain_persists_clean_caption_and_every_artifact(tmp_path, monkeypatch):
    """Only provider I/O is synthetic; native admission/queue/agent/DB flush run."""
    _offline_attachment_scope(monkeypatch)
    from contextlib import nullcontext
    from unittest.mock import MagicMock, patch
    from hermes_state import SessionDB
    from tests.test_tui_gateway_server import _configure_immediate_prompt_run, _session
    from tests.run_agent.test_run_agent import _mock_response
    from tui_gateway import server
    from run_agent import AIAgent
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    drain = server._drain_queued_prompt
    _configure_immediate_prompt_run(monkeypatch, tmp_path, immediate_threads=False)
    db = SessionDB(tmp_path / "state.db")
    sid = "native-batch"; db.create_session(sid, "desktop", model="test/model")
    with patch("model_tools.get_tool_definitions", return_value=[]), \
         patch("model_tools.check_toolset_requirements", return_value={}), \
         patch("agent.process_bootstrap.OpenAI"):
        agent = AIAgent(api_key="synthetic-not-a-real-key", base_url="https://provider.invalid/v1",
                        model="test/model", quiet_mode=True, skip_context_files=True,
                        skip_memory=True, session_db=db, session_id=sid)
    agent.client = MagicMock()
    agent.client.chat.completions.create.return_value = _mock_response(content="Synthetic answer")
    agent._session_db_created = True
    agent._cached_system_prompt = "Synthetic test system"
    agent._skip_mcp_refresh = True
    session = _session(session_key=sid, agent=agent, running=True, profile_home=str(tmp_path))
    session["attached_images"] = ["untouched-legacy-image"]
    monkeypatch.setattr(server, "_session_db", lambda s: nullcontext(db))
    monkeypatch.setattr(server, "_sess_nowait", lambda *a: (session, None))
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a: None)
    monkeypatch.setattr(server, "_legacy_group_fence_error", lambda *a: None)
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a: False)
    monkeypatch.setattr(server, "_sync_agent_compression_with_config", lambda *a: None)
    monkeypatch.setattr(server, "_sync_bot_capabilities", lambda *a: None)
    monkeypatch.setattr(server, "_apply_pending_model_switch", lambda *a: None)
    events = []
    monkeypatch.setattr(server, "_emit", lambda *args, **kw: events.append(args))
    server._sessions["native-batch-handle"] = session
    try:
        mid = str(uuid4()); entries = mixed()
        params = dict(session_id="native-batch-handle", text="Owner caption", client_message_id=mid,
                      attachment_batch={"batch_id": mid, "items": entries})
        receipt = server._methods["prompt.submit"]("rpc", params)
        assert receipt["result"]["status"] == "queued", receipt
        session["running"] = False
        assert drain("rpc", "native-batch-handle", session)
        session["_run_thread"].join(timeout=15)
        assert not session["_run_thread"].is_alive()
        rows = db.get_messages(sid)
        owners = [r for r in rows if r["role"] == "user"]
        assert len(owners) == 1 and owners[0]["content"] == "Owner caption", (rows, events)
        metadata = owners[0]["display_metadata"]
        assert metadata["attachments"] == receipt["result"]["attachments"]
        assert metadata["client_message_ids"] == [mid]
        assert session["attached_images"] == ["untouched-legacy-image"]
        requests = agent.client.chat.completions.create.call_args_list
        assert len(requests) == 1
        prompt = json.dumps(requests[0].kwargs["messages"])
        assert prompt.index("1. notes.txt") < prompt.index("2. photo.png") < prompt.index("3. last.txt")
        assert server._methods["prompt.submit"]("lost-ack-retry", params)["result"]["delivery_state"] == "completed"
        assert agent.client.chat.completions.create.call_count == 1
    finally:
        server._sessions.pop("native-batch-handle", None)
        db.close()


def test_real_agent_immediate_submit_persists_clean_caption_and_private_manifest_as_api_content(
        tmp_path, monkeypatch):
    """The idle-session path must preserve the same display/wire split as queue drain."""
    _offline_attachment_scope(monkeypatch)
    from contextlib import nullcontext
    from unittest.mock import MagicMock, patch
    from hermes_state import SessionDB
    from tests.test_tui_gateway_server import _configure_immediate_prompt_run, _session
    from tests.run_agent.test_run_agent import _mock_response
    from tui_gateway import server
    from run_agent import AIAgent

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _configure_immediate_prompt_run(monkeypatch, tmp_path, immediate_threads=False)
    db = SessionDB(tmp_path / "state.db")
    sid = "native-batch-immediate"
    db.create_session(sid, "desktop", model="test/model")
    with patch("model_tools.get_tool_definitions", return_value=[]), \
         patch("model_tools.check_toolset_requirements", return_value={}), \
         patch("agent.process_bootstrap.OpenAI"):
        agent = AIAgent(api_key="synthetic-not-a-real-key", base_url="https://provider.invalid/v1",
                        model="test/model", quiet_mode=True, skip_context_files=True,
                        skip_memory=True, session_db=db, session_id=sid)
    agent.client = MagicMock()
    agent.client.chat.completions.create.return_value = _mock_response(content="Synthetic answer")
    agent._session_db_created = True
    agent._cached_system_prompt = "Synthetic test system"
    agent._skip_mcp_refresh = True
    ready = threading.Event(); ready.set()
    session = _session(
        session_key=sid, agent=agent, running=False, profile_home=str(tmp_path), agent_ready=ready)
    monkeypatch.setattr(server, "_session_db", lambda s: nullcontext(db))
    monkeypatch.setattr(server, "_sess_nowait", lambda *a: (session, None))
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a: None)
    monkeypatch.setattr(server, "_legacy_group_fence_error", lambda *a: None)
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a: False)
    monkeypatch.setattr(server, "_sync_agent_compression_with_config", lambda *a: None)
    monkeypatch.setattr(server, "_sync_bot_capabilities", lambda *a: None)
    monkeypatch.setattr(server, "_apply_pending_model_switch", lambda *a: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *a: None)
    monkeypatch.setattr(server, "_restart_completed_failed_agent_build", lambda *a: False)
    monkeypatch.setattr(server, "_emit", lambda *args, **kw: None)
    monkeypatch.setattr("agent.image_routing.decide_image_input_mode", lambda *a, **kw: "native")
    server._sessions["native-batch-immediate-handle"] = session
    try:
        mid = str(uuid4())
        params = dict(
            session_id="native-batch-immediate-handle", text="Owner caption", client_message_id=mid,
            attachment_batch={"batch_id": mid, "items": mixed()},
        )
        receipt = server._methods["prompt.submit"]("rpc", params)
        assert receipt["result"]["status"] == "streaming", receipt
        run_thread = session["_run_thread"]
        for _ in range(3):
            # The native startup callback publishes its next worker before start().
            # Synchronize on actual thread start rather than racing join().
            assert run_thread._started.wait(timeout=15)
            run_thread.join(timeout=15)
            assert not run_thread.is_alive()
            latest = session["_run_thread"]
            if latest is run_thread:
                break
            run_thread = latest
        rows = db.get_messages(sid)
        owner = next(row for row in rows if row["role"] == "user")
        assert owner["content"] == "Owner caption"
        assert "Attachments (in supplied order):" in owner["api_content"]
        assert "@file:" in owner["api_content"] and str(tmp_path) in owner["api_content"]
        assert len(owner["display_metadata"]["attachments"]) == 3
        assert owner["display_metadata"]["client_message_ids"] == [mid]
    finally:
        server._sessions.pop("native-batch-immediate-handle", None)
        db.close()


def test_native_lease_identity_keeps_compression_retry_but_excludes_forks(tmp_path):
    from hermes_state import SessionDB
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session("root", "gui")
        db.end_session("root", "compression")
        db.create_session("compressed", "gui", parent_session_id="root")
        db.create_session("delegate", "tool", parent_session_id="root")
        assert db._session_turn_lease_key("compressed") == "root"
        assert db._session_turn_lease_key("delegate") == "delegate"
