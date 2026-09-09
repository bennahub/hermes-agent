from types import SimpleNamespace

from hermes_state import SessionDB
from agent import execution_scope_policy as policy
from agent.execution_scope_preflight import prepare_scope


def _run(tmp_path, owner_text, monkeypatch, *, canonical=True, purpose="message", display_kind=None):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("owner", "cli")
    from agent.message_projection import native_metadata
    assistant_id = db.append_message("owner", "assistant", "Use the supported Action1 package path, then verify the result.",
        display_metadata=native_metadata("agent", "owner", purpose) if canonical else None, display_kind=display_kind)
    owner_id = db.append_message("owner", "user", owner_text)
    row = {"_row_id": owner_id, "content": owner_text, "role": "user"}
    seen = []
    monkeypatch.setattr(policy, "_decision", lambda system, payload, runtime: seen.append(payload) or {
        "objective": "execute task", "permitted": ["inspect", "repair"], "excluded": []
    })
    import tools.owner_task_authority as authority
    monkeypatch.setattr(authority, "turn_authority", lambda _turn: {
        "execution_source": {"id": "ingress-1", "instruction": owner_text}
    })
    monkeypatch.setattr(authority, "peek_execution_source", lambda _agent: None)
    agent = SimpleNamespace(session_id="owner", _session_db=db, _current_turn_id="turn-1",
                            _current_main_runtime=lambda: None)
    try:
        prepare_scope(agent, owner_text, [row], 0)
        return seen
    finally:
        db.close()


def test_explicit_owner_adoption_binds_only_immediate_proposal(tmp_path, monkeypatch):
    seen = _run(tmp_path, "نفذ اقتراحك بالكامل", monkeypatch)
    adopted = seen[0]["original_instruction"]["owner_visible_proposal_candidate"]
    assert adopted["session_id"] == "owner"
    assert adopted["message_id"] > 0
    assert "Action1 package path" in adopted["content"]
    assert len(adopted["sha256"]) == 64


def test_noncanonical_or_control_reply_is_not_a_proposal_candidate(tmp_path, monkeypatch):
    seen = _run(tmp_path, "نفذ ما طلبته", monkeypatch, canonical=False)
    assert seen[0]["original_instruction"] == "نفذ ما طلبته"


def test_control_projection_is_not_a_proposal_candidate(tmp_path, monkeypatch):
    seen = _run(tmp_path, "نفذ ما طلبته", monkeypatch, purpose="control")
    assert seen[0]["original_instruction"] == "نفذ ما طلبته"


def test_scope_denial_requires_current_attempt_without_admitted_action(tmp_path):
    from agent import execution_scope
    from agent.autonomy import owner_continuity

    db = SessionDB(tmp_path / "state.db")
    scope = db.create_or_get_scope("owner:scope-denial", {"kind": "owner"}, {
        "version": 1, "policy": {"objective": "inspect", "permitted": ["inspect"], "excluded": []},
        "original_instruction": "inspect",
    })
    binding = execution_scope.Binding(db, scope["scope_id"])
    execution_scope._TURNS["scope-denial-turn"] = binding
    agent = SimpleNamespace(_current_turn_id="scope-denial-turn")
    try:
        assert owner_continuity._not_started_scope_failure(agent, {"error_type": "execution_scope_denied"})
        db.claim_scope_action(scope["scope_id"], "turn:scope-denial-turn:call", "terminal", {"command": "inspect"}, attempt_id="scope-denial-turn")
        assert not owner_continuity._not_started_scope_failure(agent, {"error_type": "execution_scope_denied"})
    finally:
        execution_scope._TURNS.pop("scope-denial-turn", None)
        db.close()


def test_finish_turn_routes_current_scope_denial_to_failed_without_badge(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from agent import execution_scope
    from agent.autonomy import owner_continuity, store
    from tui_gateway.activity import work_state

    db = SessionDB(tmp_path / "state.db")
    db.create_session("owner", "cli")
    source_id = db.append_message("owner", "user", "owner task")
    work = store.start_work(why="owner", outcome="done", done_contract="receipt",
        idempotency_key="finish-turn", hermes_home=tmp_path,
        refs={"owner_request": {"session_id": "owner", "message_id": str(source_id)}})["work"]
    scope = db.create_or_get_scope("work:" + work["id"], {"kind": "continuation"}, {
        "version": 1, "policy": {"objective": "owner task", "permitted": [], "excluded": []},
        "original_instruction": "owner task",
    })
    locator = {"scope_id": scope["scope_id"], "db_path": str(db.db_path)}
    work = store.update_work(work["id"], refs={"execution_scope": locator}, hermes_home=tmp_path, expected_state="working")
    binding = execution_scope.Binding(db, scope["scope_id"])
    execution_scope._TURNS["finish-turn"] = binding
    agent = SimpleNamespace(_owner_continuity_work_id=work["id"], _current_turn_id="finish-turn",
                            _active_session_turn_lease_holder=None)
    try:
        owner_continuity.finish_turn(agent, {"failed": True, "error_type": "execution_scope_denied"})
        parked = store.get_work(work["id"], hermes_home=tmp_path)
        assert parked["state"] == "working" and parked["refs"]["pending_owner_result"] is not None
        assert parked["refs"]["outcome_kind"] == "authorization_not_started"
        assert owner_continuity.deliver_pending(parked, tmp_path)
        assert store.get_work(work["id"], hermes_home=tmp_path)["state"] == "failed"
        assert work_state(tmp_path) is None
    finally:
        execution_scope._TURNS.pop("finish-turn", None)
        db.close()


def test_summary_marker_cannot_authorize_even_with_copied_native_projection(tmp_path, monkeypatch):
    seen = _run(tmp_path, "نفذ اقتراحك بالكامل", monkeypatch, display_kind="context_summary")
    assert seen[0]["original_instruction"] == "نفذ اقتراحك بالكامل"
