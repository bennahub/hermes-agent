import json

from hermes_state import SessionDB
from agent.autonomy import store
from agent.autonomy.owner_continuity import verification_candidates, verify
from hermes_cli import autonomy_cmd


def _work(home):
    db = SessionDB(home / "state.db")
    db.create_session("owner-session", "cli")
    source_id = db.append_message("owner-session", "user", "Create and verify the bounded artifact")
    work = store.start_work(
        why="owner task", outcome="verified artifact", done_contract="receipt",
        idempotency_key="native-verification", hermes_home=home,
        refs={"owner_request": {"session_id": "owner-session", "message_id": str(source_id)}},
    )["work"]
    return db, work, source_id


def _tool(db, session, payload, work_id, *, effect="completed"):
    return db.append_message(
        session, "tool", json.dumps(payload), tool_name="terminal",
        effect_disposition=effect, display_metadata={"owner_work_id": work_id, "tool": "terminal"},
    )


def test_work_get_discovers_receipt_and_verify_accepts_exact_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db, work, _ = _work(tmp_path)
    try:
        message_id = _tool(db, "owner-session", {"status": "completed", "output": "artifact deleted and verified"}, work["id"])
        captured = []
        monkeypatch.setattr(autonomy_cmd, "_print_json", lambda payload, exit_code=0: captured.append(payload) or exit_code)
        assert autonomy_cmd._cmd_work_get(type("Args", (), {"work_id": work["id"]})()) == 0
        candidates = captured[0]["work"]["verification_candidates"]
        assert candidates == [{
            "message_id": message_id, "session_id": "owner-session", "tool": "terminal",
            "status": "completed", "output_preview": "artifact deleted and verified",
        }]
        updated = verify(work["id"], tool_message_id=message_id, hermes_home=tmp_path)
        assert updated["refs"]["verification"]["message_id"] == str(message_id)
    finally:
        db.close()


def test_candidates_exclude_old_other_work_failed_and_dispatch_receipts(tmp_path):
    db, work, source_id = _work(tmp_path)
    db.create_session("other-session", "cli")
    try:
        failed_id = _tool(db, "owner-session", {"status": "failed", "error": "SOURCE_DOWNLOAD"}, work["id"])
        pending_id = _tool(db, "owner-session", {"status": "pending", "process_id": "proc-1"}, work["id"])
        foreign_session_id = _tool(db, "other-session", {"status": "completed", "output": "foreign session"}, work["id"])
        foreign_work_id = _tool(db, "owner-session", {"status": "completed", "output": "other work"}, "aw_other")
        # A receipt from the current source is the only candidate.
        good_id = _tool(db, "owner-session", {"status": "completed", "output": "current outcome"}, work["id"])
        candidates = verification_candidates(work, hermes_home=tmp_path)
        assert [item["message_id"] for item in candidates] == [good_id]
        assert candidates[0]["output_preview"] == "current outcome"
        import pytest
        with pytest.raises(ValueError, match="failed tool observation"):
            verify(work["id"], tool_message_id=failed_id, hermes_home=tmp_path)
        for rejected_id in (foreign_session_id, foreign_work_id):
            with pytest.raises(ValueError, match="persisted tool observation"):
                verify(work["id"], tool_message_id=rejected_id, hermes_home=tmp_path)
    finally:
        db.close()
