"""Native owner decisions prepare scope from exact structured work, never transcript history."""
import json
from types import SimpleNamespace

import pytest

from agent import execution_scope
from agent.execution_scope_preflight import prepare_scope, ScopePreflightBlocked
from agent.autonomy import owner_continuity as oc, store
from tests.autonomy.test_owner_completion_contract import native_owner
from tools import owner_task_authority as authority


@pytest.fixture()
def decision_case(autonomy_home, monkeypatch):
    db, work, original_id = native_owner(autonomy_home)
    original = db.get_messages("owner-chat")[0]["content"]
    old_scope = db.create_or_get_scope("prior-owner", {"instruction": original},
                                     {"version": 1, "policy": {"objective": original,
                                      "permitted": [original], "excluded": []}})
    store.update_work(work["id"], refs={"execution_scope": {
        "db_path": str(db.db_path), "scope_id": old_scope["scope_id"]}}, hermes_home=autonomy_home)
    oc.request_finish(work["id"], "Owner approval is required", terminal="needs_owner",
                      hermes_home=autonomy_home)
    oc.deliver_pending(store.get_work(work["id"], autonomy_home), autonomy_home)
    db.close_scope(old_scope["scope_id"])
    observed = []

    def policy(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        observed.append(payload["original_instruction"])
        decision = {"objective": "The exact original request and selected native continuation",
                    "permitted": ["Only the supplied current authority"], "excluded": ["Historical commands"]}
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(decision)))])

    monkeypatch.setattr("agent.auxiliary_client.call_llm", policy)
    yield db, work, original, observed
    authority.clear_all()
    db.close()


def stage(db, text):
    mid = db.append_message("owner-chat", "user", text)
    turn = "decision:" + str(mid)
    nonce = authority.mint_pending("owner-chat", profile_home=str(db.db_path.parent), instruction=text)
    authority.bind_turn("owner-chat", turn, nonce=nonce)
    agent = SimpleNamespace(_session_db=db, session_id="owner-chat", platform="api_server",
                            _current_turn_id=turn, _current_main_runtime=lambda: {})
    row = {"role": "user", "content": text, "_row_id": mid}
    return agent, row


def test_continue_compiles_exact_native_work_and_binds_same_decision(decision_case):
    db, work, original, observed = decision_case
    db.append_message("owner-chat", "assistant", "Historical unrelated echo PROBE")
    agent, row = stage(db, "continue")
    prepare_scope(agent, row["content"], [row], 0)
    assert agent._prepared_owner_decision["target"]["work_id"] == work["id"]
    oc.bind_turn(agent, row)
    execution_scope.bind_agent_turn(agent, row["content"], row)
    binding = execution_scope.get_binding(agent._current_turn_id)
    assert binding.work_id == work["id"]
    current = store.get_work(work["id"], db.db_path.parent)
    assert current["refs"]["owner_decision"]["message_id"] == str(row["_row_id"])
    assert current["refs"]["execution_scope"]["scope_id"] == binding.scope_id
    execution_scope.close_turn(agent)


def test_unrelated_new_instruction_does_not_inherit_pending_work(decision_case):
    db, work, original, observed = decision_case
    agent, row = stage(db, "Explain the MET definition")
    prepare_scope(agent, row["content"], [row], 0)
    assert agent._prepared_owner_decision["target"] is None
    oc.bind_turn(agent, row)
    execution_scope.bind_agent_turn(agent, row["content"], row)
    assert execution_scope.get_binding(agent._current_turn_id).work_id is None
    assert store.get_work(work["id"], db.db_path.parent)["state"] == "needs_owner"
    execution_scope.close_turn(agent)


def test_ambiguous_continue_cannot_compile_or_consume_obligations(decision_case):
    db, work, original, observed = decision_case
    mid = db.append_message("owner-chat", "user", "After two minutes check a second artifact")
    second = oc.register_owner_request(db, "owner-chat", mid, hermes_home=db.db_path.parent)
    oc.request_finish(second["id"], "Second owner approval", terminal="needs_owner",
                      hermes_home=db.db_path.parent)
    oc.deliver_pending(store.get_work(second["id"], db.db_path.parent), db.db_path.parent)
    agent, row = stage(db, "continue")
    with pytest.raises(ScopePreflightBlocked, match="Which one"):
        prepare_scope(agent, row["content"], [row], 0)
    assert observed == []
    assert store.get_work(work["id"], db.db_path.parent)["state"] == "needs_owner"
    assert store.get_work(second["id"], db.db_path.parent)["state"] == "needs_owner"


def test_changed_target_after_preflight_does_not_consume_owner_decision(decision_case):
    db, work, original, observed = decision_case
    agent, row = stage(db, "continue")
    prepare_scope(agent, row["content"], [row], 0)
    current = store.get_work(work["id"], db.db_path.parent)
    store.update_work(work["id"], refs={"resume_generation": int(current["refs"].get("resume_generation") or 0) + 1},
                      hermes_home=db.db_path.parent)
    with pytest.raises(ValueError, match="target changed"):
        oc.bind_turn(agent, row)
    current = store.get_work(work["id"], db.db_path.parent)
    assert current["state"] == "needs_owner"
    assert not current["refs"].get("owner_decision")


def test_decision_row_race_after_transition_blocks_execution_binding(decision_case):
    db, work, original, observed = decision_case
    agent, row = stage(db, "continue")
    prepare_scope(agent, row["content"], [row], 0)
    oc.bind_turn(agent, row)
    current = store.get_work(work["id"], db.db_path.parent)
    changed = {**current["refs"]["owner_decision"], "message_id": "wrong-native-row"}
    store.update_work(work["id"], refs={"owner_decision": changed}, hermes_home=db.db_path.parent)
    execution_scope.bind_agent_turn(agent, row["content"], row)
    assert execution_scope.get_binding(agent._current_turn_id) is None



def test_decision_race_inside_scope_cas_preserves_previous_authority(decision_case, monkeypatch):
    db, work, original, observed = decision_case
    agent, row = stage(db, "continue")
    prepare_scope(agent, row["content"], [row], 0)
    oc.bind_turn(agent, row)
    prior = store.get_work(work["id"], db.db_path.parent)["refs"]["execution_scope"]
    real_update = store.update_work

    def raced(work_id, **kwargs):
        if "execution_scope" in kwargs.get("refs", {}):
            current = store.get_work(work_id, db.db_path.parent)
            real_update(work_id, refs={"owner_decision": {
                **current["refs"]["owner_decision"], "message_id": "replaced-at-cas",
            }}, hermes_home=db.db_path.parent)
        return real_update(work_id, **kwargs)

    monkeypatch.setattr(store, "update_work", raced)
    execution_scope.bind_agent_turn(agent, row["content"], row)
    assert store.get_work(work["id"], db.db_path.parent)["refs"]["execution_scope"] == prior
    assert execution_scope.get_binding(agent._current_turn_id) is None


def test_readonly_selection_does_not_create_missing_business_database(tmp_path):
    from hermes_state import SessionDB
    from agent.autonomy.owner_decision import select_owner_decision

    with SessionDB(tmp_path / "state.db") as db:
        db.create_session("fresh", source="desktop")
        mid = db.append_message("fresh", "user", "continue")
        assert select_owner_decision(db, "fresh", mid, strict=True) is None
        assert not (tmp_path / "autonomy" / "work.db").exists()
