import json
import sqlite3

from agent import execution_scope as scopes
from agent import execution_scope_policy as policy
from hermes_state import SessionDB


def _binding(tmp_path, key="owner:observed"):
    db = SessionDB(tmp_path / "state.db")
    frozen = {"version": 1, "policy": {"objective": "inspect then repair", "permitted": ["inspect", "repair"], "excluded": ["wipe"]}, "original_instruction": "inspect then repair"}
    record = db.create_or_get_scope(key, {"kind": "owner", "instruction": "inspect then repair"}, frozen)
    return db, scopes.Binding(db, record["scope_id"])


def test_observations_use_real_policy_payload_and_remain_scope_bound(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("security:\n  execution_scope_observations: true\n", encoding="utf-8")
    db, binding = _binding(tmp_path)
    payloads = []
    calls = {"inspect": 0, "repair": 0}

    def decision(system, payload, runtime):
        payloads.append((system, payload))
        return {"allowed": True, "reason": "test"}

    monkeypatch.setattr(policy, "_decision", decision)
    monkeypatch.setitem(scopes._TURNS, "observed-turn", binding)
    try:
        def inspect(_):
            calls["inspect"] += 1
            return json.dumps({"version": "1"})

        assert scopes.execute_scoped("terminal", {"command": "inspect"}, inspect, turn_id="observed-turn", invocation_id="inspect")
        assert scopes.execute_scoped("terminal", {"command": "repair"}, lambda _: calls.__setitem__("repair", calls["repair"] + 1) or "repaired", turn_id="observed-turn", invocation_id="repair") == "repaired"
        assert calls == {"inspect": 1, "repair": 1}
        assert payloads == []
        assert scopes.execute_scoped("terminal", {"command": "repair"}, lambda _: calls.__setitem__("repair", calls["repair"] + 1), turn_id="observed-turn", invocation_id="repair") == "repaired"
        assert calls["repair"] == 1

        db.claim_scope_action(binding.scope_id, "legacy", "terminal", {"command": "legacy"})
        db.complete_scope_action(binding.scope_id, "legacy", "legacy-result")
        db._execute_write(lambda conn: conn.execute("UPDATE execution_scope_actions SET arguments_json=NULL WHERE invocation_id=?", ("legacy",)))
        assert all(item["invocation_id"] != "legacy" for item in db.get_scope_observations(binding.scope_id))
        db.claim_scope_action(binding.scope_id, "uncertain", "terminal", {"command": "uncertain"})
        db.mark_scope_action_uncertain(binding.scope_id, "uncertain")
        assert all(item["invocation_id"] != "uncertain" for item in db.get_scope_observations(binding.scope_id))
        other = db.create_or_get_scope("owner:other", {"kind": "owner", "instruction": "other"}, {"version": 1, "policy": {"objective": "other", "permitted": [], "excluded": []}, "original_instruction": "other"})
        assert db.get_scope_observations(other["scope_id"]) == []
        db.close_scope(binding.scope_id)
        assert db.get_scope_observations(binding.scope_id) == []
    finally:
        scopes._TURNS.pop("observed-turn", None)
        db.close()


def test_explicit_opt_out_has_no_observation_payload_and_legacy_upgrade_preserves_row(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("security:\n  execution_scope_observations: false\n")
    db, binding = _binding(tmp_path, "owner:default-off")
    db.claim_scope_action(binding.scope_id, "legacy", "terminal", {"command": "old"})
    db.complete_scope_action(binding.scope_id, "legacy", "old-result")
    db._execute_write(lambda conn: conn.execute("UPDATE execution_scope_actions SET arguments_json=NULL WHERE invocation_id='legacy'"))
    db.close()
    connection = sqlite3.connect(tmp_path / "state.db")
    before = connection.execute("SELECT arguments_digest, result_json FROM execution_scope_actions WHERE invocation_id='legacy'").fetchone()
    connection.execute("ALTER TABLE execution_scope_actions DROP COLUMN arguments_json")
    connection.commit()
    connection.close()
    db = SessionDB(tmp_path / "state.db")
    try:
        connection = sqlite3.connect(tmp_path / "state.db")
        columns = {row[1]: row[3] for row in connection.execute("PRAGMA table_info(execution_scope_actions)")}
        after = connection.execute("SELECT arguments_digest, result_json FROM execution_scope_actions WHERE invocation_id='legacy'").fetchone()
        connection.close()
        assert columns["arguments_json"] == 0 and after == before
        assert db.get_scope_observations(binding.scope_id) == []
        captured = []
        monkeypatch.setattr(policy, "_decision", lambda system, payload, runtime: captured.append(payload) or {"allowed": True, "reason": "test"})
        monkeypatch.setitem(scopes._TURNS, "default-off", scopes.Binding(db, binding.scope_id))
        assert scopes.execute_scoped("terminal", {"command": "inspect"}, lambda _: "ok", turn_id="default-off", invocation_id="fresh") == "ok"
        assert captured == []
    finally:
        scopes._TURNS.pop("default-off", None)
        db.close()


def test_large_receipts_do_not_strand_scope_or_shrink_invocation_budget(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("security:\n  execution_scope_observations: true\n")
    db, binding = _binding(tmp_path, "owner:large")
    payloads = []
    def decision(system, payload, runtime):
        assert len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()) <= policy.MAX_INPUT_BYTES
        payloads.append(payload)
        return {"allowed": True, "reason": "test"}
    monkeypatch.setattr(policy, "_decision", decision)
    monkeypatch.setitem(scopes._TURNS, "large-turn", binding)
    try:
        db.claim_scope_action(binding.scope_id, "large", "read_file", {"path": "large"})
        db.complete_scope_action(binding.scope_id, "large", "x" * 700000)
        db.claim_scope_action(binding.scope_id, "small", "read_file", {"path": "small"})
        db.complete_scope_action(binding.scope_id, "small", "x" * 60000)
        assert [r["invocation_id"] for r in db.get_scope_observations(binding.scope_id)] == ["small"]
        dispatched = []
        assert scopes.execute_scoped("write_file", {"content": "x" * 220000},
            lambda args: dispatched.append(args) or "ok", turn_id="large-turn", invocation_id="write") == "ok"
        assert len(dispatched) == 1
        assert payloads == []
        assert scopes.execute_scoped("read_file", {"path": "verify"}, lambda _: "verified",
            turn_id="large-turn", invocation_id="verify") == "verified"
        assert binding.failures == 0
        (tmp_path / "config.yaml").write_text("security: [broken")
        assert scopes.execute_scoped("read_file", {"path": "verify"}, lambda _: "ok",
            turn_id="large-turn", invocation_id="config-fallback") == "ok"
    finally:
        scopes._TURNS.pop("large-turn", None)
        db.close()


def test_same_native_receipts_reach_consequence_guardian_without_bypassing_it(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from agent import auxiliary_client
    from agent.execution_scope_observations import compilation_context
    from tools.approval_smart import _smart_approve
    (tmp_path / "config.yaml").write_text("security:\n  execution_scope_observations: true\n")
    db, binding = _binding(tmp_path, "owner:guardian")
    db.claim_scope_action(binding.scope_id, "inspect", "read_file", {"path": "helper.py"})
    db.complete_scope_action(binding.scope_id, "inspect", "safe helper contents")
    seen = []
    def guardian(**kwargs):
        seen.append(kwargs["messages"])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="DENY"))])
    monkeypatch.setattr(auxiliary_client, "call_llm", guardian)
    monkeypatch.setattr(policy, "_decision", lambda *a: {"allowed": True, "reason": "test"})
    monkeypatch.setitem(scopes._TURNS, "guardian-turn", binding)
    try:
        assert compilation_context(db) == {"resolve_targets": True}
        assert scopes.execute_scoped("terminal", {"command": "helper"},
            lambda _: _smart_approve("helper", "script"), turn_id="guardian-turn", invocation_id="guard") == "approve"
        assert seen == []
        (tmp_path / "config.yaml").write_text("security:\n  execution_scope_observations: false\n")
        assert compilation_context(db) == {}
        assert scopes.execute_scoped("terminal", {"command": "helper2"},
            lambda _: _smart_approve("helper2", "script"), turn_id="guardian-turn", invocation_id="guard2") == "approve"
    finally:
        scopes._TURNS.pop("guardian-turn", None)
        db.close()


def test_native_preflight_compiles_once_with_profile_selector_policy(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from agent.execution_scope_preflight import prepare_scope
    from tools.owner_task_authority import mint_execution_source
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("security:\n  execution_scope_observations: true\n")
    seen = []
    def decision(system, payload, runtime):
        seen.append((system, payload))
        return {"objective": "verify current work", "permitted": ["verify current work"], "excluded": []}
    monkeypatch.setattr(policy, "_decision", decision)
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session("preflight", "cli")
        instruction = "Verify this task and complete its current native work"
        row = {"role": "user", "content": instruction,
               "_row_id": db.append_message("preflight", "user", instruction)}
        agent = SimpleNamespace(session_id="preflight", _session_db=db,
            _current_turn_id="selector-turn", _current_main_runtime=lambda: None)
        assert mint_execution_source(agent, instruction, source_id="selector-owner")
        try:
            prepare_scope(agent, instruction, [row], 0)
            scopes.bind_agent_turn(agent, instruction, row)
            assert seen == []
            assert scopes.get_binding(agent._current_turn_id) is not None
        finally:
            scopes._TURNS.pop("selector-turn", None)


def test_native_child_receives_only_current_structured_work_reference(tmp_path, monkeypatch):
    from agent.autonomy import store
    from agent.execution_scope_observations import policy_context
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("security:\n  execution_scope_observations: true\n")
    db, parent = _binding(tmp_path, "owner:child-work")
    work = store.start_work(why="test", outcome="test", done_contract="test", idempotency_key="child-work",
        hermes_home=tmp_path, refs={"resume_generation": 1,
        "execution_scope": {"scope_id": parent.scope_id, "db_path": str(db.db_path)}})["work"]
    parent.work_id, parent.work_home, parent.generation = work["id"], str(tmp_path), 1
    try:
        child = scopes.derive_child(parent, "native-child", "record this work")
        binding = scopes.Binding(db, child["scope_id"])
        assert policy_context(binding)["native_work"]["work_id"] == work["id"]
        assert policy_context(binding)["native_work"]["verification_recorded"] is False
        store.update_work(work["id"], hermes_home=tmp_path, refs={"verification": {"kind": "tool", "message_id": "evidence"}})
        assert policy_context(binding)["native_work"]["verification_recorded"] is True
        grandchild = scopes.derive_child(binding, "native-grandchild", "record this work")
        assert policy_context(scopes.Binding(db, grandchild["scope_id"]))["native_work"]["work_id"] == work["id"]
        store.update_work(work["id"], hermes_home=tmp_path, refs={"resume_generation": 2})
        assert "native_work" not in policy_context(binding)
        parent.generation = 2
        next_child = scopes.derive_child(parent, "native-next", "record this work")
        next_binding = scopes.Binding(db, next_child["scope_id"])
        assert policy_context(next_binding)["native_work"]["generation"] == 2
        store.update_work(work["id"], hermes_home=tmp_path, refs={"owner_stop": {"reason": "done"}})
        assert "native_work" not in policy_context(next_binding)
    finally:
        db.close()


def test_preflight_referents_are_current_session_data_only(tmp_path, monkeypatch):
    from agent.autonomy import store
    from agent.execution_scope_observations import active_work_referents, compilation_context
    from agent.execution_scope_preflight import prepare_scope
    from agent.autonomy.owner_continuity import _bind_owner_decision
    from tools.owner_task_authority import mint_execution_source
    from types import SimpleNamespace
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("security:\n  execution_scope_observations: true\n")
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session("followup-session", "cli")
        db.create_session("other-session", "cli")
        db.append_message("followup-session", "user", "MacBook task")
        db.append_message("followup-session", "user", "Old task")
        mac = store.start_work(
            why="mac update", outcome="verify reachability", done_contract="read-only preflight",
            idempotency_key="mac", hermes_home=tmp_path,
            refs={"owner_request": {"session_id": "followup-session", "message_id": "1"},
                  "owner_obligation": {"id": "ob-mac", "delivery_id": "del-mac", "kind": "uncertain_execution", "status": "unresolved"}},
        )["work"]
        store.update_work(mac["id"], state="needs_owner", hermes_home=tmp_path, expected_state="working")
        old = store.start_work(
            why="old", outcome="old", done_contract="old", idempotency_key="old", hermes_home=tmp_path,
            refs={"owner_request": {"session_id": "followup-session", "message_id": "2"},
                  "owner_obligation": {"id": "ob-old", "kind": "owner_input", "status": "adjudicated"}},
        )["work"]
        store.update_work(old["id"], state="needs_owner", hermes_home=tmp_path, expected_state="working", refs={"owner_obligation": {"id": "ob-old", "kind": "owner_input", "status": "unresolved"}, "owner_stop": {"reason": "adjudicated"}})
        other = store.start_work(
            why="other", outcome="other", done_contract="other", idempotency_key="other", hermes_home=tmp_path,
            refs={"owner_request": {"session_id": "other-session", "message_id": "1"},
                  "owner_obligation": {"id": "ob-other", "kind": "owner_input", "status": "unresolved"}},
        )["work"]
        store.update_work(other["id"], state="needs_owner", hermes_home=tmp_path, expected_state="working")
        referents = active_work_referents(db, "followup-session")
        assert len(referents) == 1
        assert referents[0]["owner_obligation"]["status"] == "unresolved"
        before = store.get_work(mac["id"], hermes_home=tmp_path)
        instruction = "شيك الان يمديك تحدث"
        current = {"role": "user", "content": instruction,
                   "_row_id": db.append_message("followup-session", "user", instruction)}
        agent = SimpleNamespace(session_id="followup-session", _session_db=db,
            _current_turn_id="followup-test", _current_main_runtime=lambda: None)
        captured = []
        monkeypatch.setattr(policy, "_decision", lambda system, payload, runtime:
            captured.append((system, payload)) or {"objective": "status", "permitted": ["read-only status"], "excluded": ["updates"]})
        assert mint_execution_source(agent, instruction, source_id="followup-source")
        try:
            prepare_scope(agent, instruction, [current], 0)
            assert _bind_owner_decision(db, agent.session_id, current["_row_id"],
                                       prepared=agent._prepared_owner_decision) is None
            scopes.bind_agent_turn(agent, instruction, current)
            assert captured == []
            scope = db.get_scope(scopes.get_binding(agent._current_turn_id).scope_id)
            assert scope["source"].get("owner_decision_target") is None
            assert scope["scope"]["active_work_referents"] == referents
            assert store.get_work(mac["id"], hermes_home=tmp_path) == before
            with monkeypatch.context() as unavailable:
                def fail_read(*args, **kwargs):
                    raise sqlite3.OperationalError("optional work context unavailable")
                unavailable.setattr(sqlite3, "connect", fail_read)
                assert compilation_context(db, agent.session_id) == {"resolve_targets": True}
            (tmp_path / "config.yaml").write_text("security:\n  execution_scope_observations: false\n")
            assert compilation_context(db, agent.session_id) == {}
        finally:
            scopes.close_turn(agent)


def test_runtime_referents_drop_resolved_work_and_follow_new_obligation(tmp_path, monkeypatch):
    from agent.autonomy import store
    from agent.execution_scope_observations import policy_context

    (tmp_path / "config.yaml").write_text("security:\n  execution_scope_observations: true\n")
    db = SessionDB(tmp_path / "state.db")
    db.create_session("owner-session", "cli")
    owner_message = db.append_message("owner-session", "user", "Check the assigned target")
    old = store.start_work(
        why="old", outcome="old", done_contract="old", idempotency_key="old-obligation",
        hermes_home=tmp_path,
        refs={"owner_request": {"session_id": "owner-session", "message_id": str(owner_message)},
              "owner_obligation": {"id": "old-obligation", "delivery_id": "old-delivery",
                                    "kind": "owner_input", "status": "unresolved"}},
    )["work"]
    store.update_work(old["id"], state="needs_owner", hermes_home=tmp_path, expected_state="working")
    frozen = {"version": 1, "policy": {"objective": "inspect", "permitted": ["inspect"], "excluded": []},
              "original_instruction": "inspect", "active_work_referents": [{"work_id": old["id"]}]}
    record = db.create_or_get_scope("owner:referent-refresh", {"kind": "owner", "session_id": "owner-session"}, frozen)
    binding = scopes.Binding(db, record["scope_id"])
    try:
        assert [item["work_id"] for item in policy_context(binding)["active_work_referents"]] == [old["id"]]
        delivery_message = db.append_message("owner-session", "assistant", "Observed failed delivery")
        store.update_work(old["id"], state="failed", completion_result="Observed failed delivery", hermes_home=tmp_path, expected_state="needs_owner", refs={"owner_delivery": {"message_id": str(delivery_message), "source_session_id": "owner-session"}})
        assert policy_context(binding)["active_work_referents"] == []
        judge_payloads = []
        monkeypatch.setattr(policy, "_decision", lambda system, payload, runtime: judge_payloads.append(payload) or {"allowed": False, "reason": "no current referent"})
        monkeypatch.setitem(scopes._TURNS, "referent-refresh", binding)
        assert scopes.execute_scoped("terminal", {"command": "inspect"}, lambda _: True, turn_id="referent-refresh", invocation_id="resolved-old") is True
        assert judge_payloads == []
        new = store.start_work(
            why="new", outcome="new", done_contract="new", idempotency_key="new-obligation",
            hermes_home=tmp_path,
            refs={"owner_request": {"session_id": "owner-session", "message_id": str(owner_message)},
                  "owner_obligation": {"id": "new-obligation", "delivery_id": "new-delivery",
                                        "kind": "owner_input", "status": "unresolved"}},
        )["work"]
        store.update_work(new["id"], state="needs_owner", hermes_home=tmp_path, expected_state="working")
        assert [item["work_id"] for item in policy_context(binding)["active_work_referents"]] == [new["id"]]
    finally:
        db.close()


def test_context_denial_offers_inspection_but_uncertainty_never_retries(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    scope = {"version": 1, "policy": {"objective": "check status", "permitted": ["reads"], "excluded": ["writes"]},
             "original_instruction": "check now", "active_work_referents": [{"work_id": "active-work"}]}
    record = db.create_or_get_scope("owner:recovery", {"kind": "owner"}, scope)
    binding = scopes.Binding(db, record["scope_id"])
    monkeypatch.setitem(scopes._TURNS, "recovery-turn", binding)
    judged = []
    monkeypatch.setattr(scopes, "judge_action", lambda *a, **k: judged.append(a) or (False, "interface evidence missing"))
    effects = []
    try:
        first = scopes.execute_scoped("terminal", {"command": "unknown-helper"},
            lambda _: effects.append(True) or "ran", turn_id="recovery-turn", invocation_id="denied")
        assert first == "ran" and effects == [True] and judged == []
        db.claim_scope_action(binding.scope_id, "uncertain-action", "terminal", {"command": "prior"}, attempt_id="recovery-turn")
        db.mark_scope_action_uncertain(binding.scope_id, "uncertain-action")
        uncertain = json.loads(scopes.execute_scoped("terminal", {"command": "read"},
            lambda _: effects.append(True), turn_id="recovery-turn", invocation_id="next"))
        assert uncertain["effect_disposition"] == "uncertain" and "recovery" not in uncertain
        assert effects == [True] and judged == []
    finally:
        scopes._TURNS.pop("recovery-turn", None)
        db.close()
