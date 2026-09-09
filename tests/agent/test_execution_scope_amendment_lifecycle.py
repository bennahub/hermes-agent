"""Accepted amendments survive outages without reviving an older instruction."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agent import execution_scope as scopes
from agent import execution_scope_preflight as preflight
from agent.autonomy import owner_continuity, store
from hermes_state import SessionDB


def _work_binding(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(tmp_path / "state.db")
    db.create_session("owner-session", "cli")
    mid = db.append_message("owner-session", "user", "perform original task")
    record = db.create_or_get_scope("original", {"kind": "owner", "instruction": "perform original task"},
                                    {"objective": "original task"})
    binding = scopes.Binding(db, record["scope_id"])
    work = store.start_work(
        why="original task", outcome="original task", done_contract="verified",
        idempotency_key="native-work", hermes_home=tmp_path,
        refs={"owner_request": {"session_id": "owner-session", "message_id": str(mid)},
              "execution_scope": scopes.locator(binding), "resume_generation": 1},
    )["work"]
    binding.work_id, binding.work_home, binding.generation = work["id"], str(tmp_path), 1
    agent = SimpleNamespace(_current_turn_id="owner-turn", _session_db=db, session_id="owner-session",
                            _owner_continuity_work_id=work["id"], _current_main_runtime=lambda: None)
    monkeypatch.setitem(scopes._TURNS, "owner-turn", binding)
    monkeypatch.setattr(scopes, "judge_action", lambda *args, **kwargs: (True, "offline"))
    return agent, binding


def test_close_during_compilation_cannot_publish_closed_replacement(tmp_path, monkeypatch):
    agent, binding = _work_binding(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def compile_policy(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {"objective": "new correction"}

    monkeypatch.setattr(scopes, "derive_scope", compile_policy)
    token = scopes.begin_amendment(agent, "correction-one", text="inspect only")
    try:
        with ThreadPoolExecutor() as pool:
            future = pool.submit(scopes.finish_amendment, agent, "inspect only", "correction-one", token)
            assert entered.wait(5)
            scopes.close_turn(agent)
            release.set()
            with pytest.raises(scopes.PolicyUnavailable, match="turn changed"):
                future.result(timeout=5)
        work = store.get_work(binding.work_id, tmp_path)
        assert work["refs"]["execution_scope"] == scopes.locator(binding)
        assert work["refs"]["pending_owner_result"]["state"] == "needs_owner"
        candidate = binding.db.get_scope_for_source("correction-one")
        assert candidate["state"] == "closed"
        assert work["refs"]["execution_scope"]["scope_id"] != candidate["scope_id"]
    finally:
        release.set()
        binding.db.close()


def test_compiler_failure_preserves_facts_and_requests_exact_owner_recovery(tmp_path, monkeypatch):
    agent, binding = _work_binding(tmp_path, monkeypatch)
    binding.db.claim_scope_action(binding.scope_id, "earlier-action", "terminal", {}, attempt_id="owner-turn")
    token = scopes.begin_amendment(agent, "correction-outage", text="inspect only")
    monkeypatch.setattr(scopes, "derive_scope", lambda *args, **kwargs: (_ for _ in ()).throw(
        scopes.PolicyUnavailable("offline policy")))
    try:
        with pytest.raises(scopes.PolicyUnavailable):
            scopes.finish_amendment(agent, "inspect only", "correction-outage", token)
        work = store.get_work(binding.work_id, tmp_path)
        assert work["refs"]["execution_amendment"]["instruction"] == "inspect only"
        pending = work["refs"]["pending_owner_result"]
        assert pending["state"] == "needs_owner"
        assert "uncertain" not in pending["text"].lower()
        assert "no action" not in pending["text"].lower()
        assert binding.db.get_scope_action(binding.scope_id, "earlier-action")["state"] == "admitted"
        denied = scopes.execute_scoped("terminal", {}, lambda args: pytest.fail("old task executed"),
                                       turn_id="owner-turn", invocation_id="after-outage")
        assert json.loads(denied)["effect_disposition"] == "not_started"
        from agent.execution_scope_amendment import settle_pending_amendment
        settle_pending_amendment(work, tmp_path)
        assert store.get_work(binding.work_id, tmp_path)["refs"]["pending_owner_result"]["id"] == pending["id"]
    finally:
        binding.db.close()


def test_restart_pending_correction_blocks_old_policy_and_uses_existing_outbox(tmp_path, monkeypatch):
    agent, binding = _work_binding(tmp_path, monkeypatch)
    scopes.begin_amendment(agent, "correction-before-crash", text="inspect only")
    binding.db.close()
    binding.db = SessionDB(tmp_path / "state.db")
    agent._session_db = binding.db
    monkeypatch.setattr(owner_continuity, "continuation_display_metadata", lambda actor: {"work_id": binding.work_id} if actor is agent else None)
    monkeypatch.setitem(scopes._TURNS, "owner-turn", None)
    try:
        with pytest.raises(scopes.PolicyUnavailable, match="Accepted owner correction"):
            preflight._prepare_scope(agent, {"role": "user", "content": "old task"}, None)
        recovered = store.get_work(binding.work_id, tmp_path)
        assert recovered["refs"]["pending_owner_result"]["state"] == "needs_owner"
        fresh_binding = scopes.Binding(binding.db, binding.scope_id, work_id=binding.work_id,
                                       work_home=str(tmp_path), generation=1)
        with pytest.raises(ValueError, match="no longer current"):
            scopes._record(fresh_binding)
        assert owner_continuity.deliver_pending(recovered, tmp_path)
        from tools import owner_task_authority as authority
        mid = binding.db.append_message("owner-session", "user", "continue")
        restarted = SimpleNamespace(_current_turn_id="fresh-owner-turn", _session_db=binding.db,
                                    session_id="owner-session", _current_main_runtime=lambda: None)
        row = {"role": "user", "content": "continue", "_row_id": mid}
        observed = []
        monkeypatch.setattr(scopes, "derive_scope", lambda original, **kwargs: observed.append(original) or {"objective": "inspect only"})
        nonce = authority.mint_pending("owner-session", profile_home=str(tmp_path), instruction="continue")
        authority.bind_turn("owner-session", "fresh-owner-turn", nonce=nonce)
        preflight._prepare_scope(restarted, row, None)
        assert observed[0]["active_structured_work"]["accepted_owner_correction"] == "inspect only"
        owner_continuity.bind_turn(restarted, row)
        scopes.bind_agent_turn(restarted, "continue", row)
        replacement = scopes.get_binding("fresh-owner-turn")
        assert scopes._record(replacement)["state"] == "active"
        assert store.get_work(binding.work_id, tmp_path)["refs"]["execution_amendment"] is None
        scopes.close_turn(restarted)
    finally:
        binding.db.close()


def test_success_publishes_replacement_and_clears_exact_pending_marker(tmp_path, monkeypatch):
    agent, binding = _work_binding(tmp_path, monkeypatch)
    monkeypatch.setattr(scopes, "derive_scope", lambda *args, **kwargs: {"objective": "inspect only"})
    token = scopes.begin_amendment(agent, "correction-success", text="inspect only")
    try:
        assert store.get_work(binding.work_id, tmp_path)["refs"]["execution_amendment"]["id"] == "correction-success"
        scopes.finish_amendment(agent, "inspect only", "correction-success", token)
        current = scopes.get_binding("owner-turn")
        work = store.get_work(binding.work_id, tmp_path)
        assert current is not binding
        assert work["refs"]["execution_scope"] == scopes.locator(current)
        assert work["refs"]["execution_amendment"] is None
        assert scopes._record(current)["state"] == "active"
        scopes.close_turn(agent)
        assert binding.db.get_scope(current.scope_id)["state"] == "active"
    finally:
        binding.db.close()


@pytest.mark.parametrize("source", [{"kind": "derived"}, {"kind": "scheduled", "assignment_kind": "cron"}])
def test_native_assignment_cannot_drop_lifetime_fence_through_amendment(tmp_path, monkeypatch, source):
    db = SessionDB(tmp_path / "state.db")
    record = db.create_or_get_scope("assignment", source, {"objective": "bounded"})
    binding = scopes.Binding(db, record["scope_id"])
    monkeypatch.setitem(scopes._TURNS, "native-turn", binding)
    # Keep the native-source classification assertion independent of cron fixture setup.
    monkeypatch.setattr(scopes, "_record", lambda current: record)
    try:
        with pytest.raises(scopes.PolicyUnavailable, match="native assignment"):
            scopes.begin_amendment(SimpleNamespace(_current_turn_id="native-turn"), "correction", text="expand")
        assert not binding.amendment_pending
        assert db.get_scope(record["scope_id"])["state"] == "active"
    finally:
        db.close()


def test_admitted_old_action_survives_amendment_timeout_and_late_settlement(tmp_path, monkeypatch):
    agent, binding = _work_binding(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(scopes, "derive_scope", lambda *args, **kwargs: {"objective": "inspect only"})

    def admitted_effect(args):
        entered.set()
        assert release.wait(5)
        return "known late outcome"

    try:
        with ThreadPoolExecutor() as pool:
            old = pool.submit(scopes.execute_scoped, "terminal", {"command": "original"},
                              admitted_effect, turn_id="owner-turn", invocation_id="same-call")
            assert entered.wait(5)
            token = scopes.begin_amendment(agent, "during-effect", text="inspect only")
            scopes.finish_amendment(agent, "inspect only", "during-effect", token)
            replacement = scopes.get_binding("owner-turn")
            assert scopes.mark_attempt_uncertain("owner-turn") == 1
            action = binding.db.get_scope_action(binding.scope_id, "turn:owner-turn:call:same-call")
            assert action["uncertain"] == 1 and action["state"] == "admitted"
            next_attempt = scopes.Binding(binding.db, replacement.scope_id, work_id=binding.work_id,
                                          work_home=str(tmp_path), generation=1)
            monkeypatch.setitem(scopes._TURNS, "reconnect", next_attempt)
            denied = scopes.execute_scoped("terminal", {}, lambda args: pytest.fail("uncertain replay"),
                                           turn_id="reconnect", invocation_id="new-call")
            assert denied.uncertain
            release.set()
            assert old.result(timeout=5) == "known late outcome"
        assert binding.db.get_scope_uncertainty(replacement.scope_id, "reconnect") is None
        assert scopes.execute_scoped("terminal", {}, lambda args: "next action",
                                     turn_id="reconnect", invocation_id="new-call") == "next action"
        monkeypatch.setitem(scopes._TURNS, "owner-turn", next_attempt)
        assert scopes.execute_scoped("terminal", {"command": "original"},
                                     lambda args: pytest.fail("duplicate invocation after amendment"),
                                     turn_id="owner-turn", invocation_id="same-call") == "known late outcome"
    finally:
        release.set()
        binding.db.close()


def test_amendment_ancestry_uncertainty_rechecked_inside_native_claim(tmp_path, monkeypatch):
    agent, binding = _work_binding(tmp_path, monkeypatch)
    monkeypatch.setattr(scopes, "derive_scope", lambda *args, **kwargs: {"objective": "inspect only"})
    binding.db.claim_scope_action(binding.scope_id, "earlier", "terminal", {}, attempt_id="owner-turn")
    token = scopes.begin_amendment(agent, "race-correction", text="inspect only")
    try:
        scopes.finish_amendment(agent, "inspect only", "race-correction", token)

        def policy(*args, **kwargs):
            binding.db.mark_scope_action_uncertain(binding.scope_id, "earlier")
            return True, "offline"

        monkeypatch.setattr(scopes, "judge_action", policy)
        denied = scopes.execute_scoped("terminal", {}, lambda args: pytest.fail("claim ignored old uncertainty"),
                                       turn_id="owner-turn", invocation_id="after-correction")
        assert denied.uncertain
        current = scopes.get_binding("owner-turn")
        assert binding.db.get_scope_action(current.scope_id, "turn:owner-turn:call:after-correction") is None
        # A genuinely fresh source does not globally inherit this older obligation.
        fresh = binding.db.create_or_get_scope("fresh-owner", {"kind": "owner", "instruction": "inspect failure"},
                                               {"objective": "inspect failure"})
        assert binding.db.claim_scope_action(fresh["scope_id"], "fresh-call", "terminal", {},
                                              attempt_id="fresh-turn")["status"] == "claimed"
    finally:
        binding.db.close()
