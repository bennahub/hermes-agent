"""Policy outage precedes native work admission and cannot fabricate uncertainty."""
from types import SimpleNamespace
import pytest
from agent import execution_scope
from agent.execution_scope_policy import PolicyUnavailable
from agent.autonomy import owner_continuity as oc, store
from hermes_state import SessionDB
from tools import owner_task_authority as authority


def agent_for(db, sid):
    from run_agent import AIAgent
    return AIAgent(model="gpt-4.1", provider="openai", api_key="test", base_url="http://127.0.0.1:1/v1",
        session_id=sid, session_db=db, enabled_toolsets=[], quiet_mode=True,
        skip_memory=True, skip_context_files=True, skip_background_review=True)


def unavailable(*args, **kwargs):
    raise PolicyUnavailable("synthetic scope compiler outage")


def test_fresh_owner_outage_has_typed_result_and_no_work_obligation(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(execution_scope, "derive_scope", unavailable)
    authority.clear_all()
    with SessionDB(tmp_path / "state.db") as db:
        agent = agent_for(db, "new-owner")
        agent._pending_owner_task_nonce = authority.mint_pending([agent.session_id], profile_home=str(tmp_path), instruction="After two minutes verify the file and report")
        result = agent.run_conversation("After two minutes verify the file and report")
        assert result.get("error_type") != "execution_scope_preflight_blocked"
        works = store.list_work(hermes_home=tmp_path)
        assert all(item.get("state") != "needs_owner" for item in works)
        agent.close()


def test_resume_compiler_outage_uses_bounded_unadmitted_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(execution_scope, "derive_scope", unavailable)
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session("resume-source", source="desktop")
        mid = db.append_message("resume-source", "user", "After two minutes verify the file and report")
        work = oc.register_owner_request(db, "resume-source", mid, hermes_home=tmp_path)
        work = oc.wait(work["id"], until="2020-01-01T00:00:00Z", hermes_home=tmp_path)
        results = []
        def native_turn(*args, **kwargs):
            with monkeypatch.context() as context:
                for key in ("HERMES_OWNER_CONTINUATION_ID", "HERMES_OWNER_CONTINUATION_NONCE", "HERMES_BACKGROUND_DELIVERY"):
                    context.setenv(key, kwargs["env"][key])
                agent = agent_for(db, "resume-source")
                result = agent.run_conversation(kwargs["input"])
                results.append(result)
                agent.close()
                return SimpleNamespace(returncode=1, stdout="", stderr="")
        oc.run_resume(work["id"], 1, hermes_home=tmp_path, runner=native_turn)
        current = store.get_work(work["id"], tmp_path)
        assert results
        assert all(result.get("error_type") != "execution_scope_preflight_blocked" for result in results)
        if current["refs"].get("pending_owner_result"):
            assert current["refs"]["pending_owner_result"]["state"] in {"failed", "needs_owner"}
            assert current["refs"].get("outcome_kind") in {
                "transport_exhausted", "uncertain_execution", "failed", "internal_failure",
            }


def test_expired_consequence_grant_still_registers_exact_queued_work(tmp_path, monkeypatch):
    from agent.turn_context import _bind_owner_task_authority
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    now = [1.0]
    monkeypatch.setattr(authority, "_now", lambda: now[0])
    monkeypatch.setattr(authority, "_claiming_context_is_the_owners_turn", lambda: True)
    authority.clear_all()
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session("queued", source="desktop")
        text = "After two minutes verify the file then report"
        mid = db.append_message("queued", "user", text)
        agent = SimpleNamespace(session_id="queued", platform="desktop", _session_db=db,
                                _current_turn_id="queued-exact-turn")
        agent._pending_owner_task_nonce = authority.mint_pending([agent.session_id], profile_home=str(tmp_path), instruction=text)
        now[0] += authority.PENDING_TTL_SECONDS + 1
        _bind_owner_task_authority(agent, agent._current_turn_id)
        oc.bind_turn(agent, {"role": "user", "content": text, "_row_id": mid})
        assert authority.turn_authority(agent._current_turn_id) is None
        work = store.get_work(agent._owner_continuity_work_id, tmp_path)
        assert work["refs"]["owner_request"] == {"session_id": "queued", "message_id": str(mid)}
    authority.clear_all()


def test_lazy_async_work_retains_already_frozen_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session("lazy", source="desktop")
        mid = db.append_message("lazy", "user", "inspect the report")
        scope = db.create_or_get_scope("lazy-scope", {"kind": "owner", "instruction": "inspect the report"}, {"objective": "inspect report"})
        binding = execution_scope.Binding(db, scope["scope_id"])
        agent = SimpleNamespace(session_id="lazy", _session_db=db, _current_turn_id="lazy-turn",
                                _owner_continuity_source=("lazy", mid), _owner_continuity_work_id=None)
        monkeypatch.setitem(execution_scope._TURNS, "lazy-turn", binding)
        oc.prepare_dispatch(agent, "terminal", {"background": True})
        work = store.get_work(agent._owner_continuity_work_id, tmp_path)
        assert work["refs"]["execution_scope"] == {"scope_id": scope["scope_id"], "db_path": str(db.db_path)}
        assert binding.work_id == work["id"]
        oc.wait(work["id"], until="2030-01-01T00:00:00Z", hermes_home=tmp_path)
        execution_scope.close_turn(agent)
        assert db.get_scope(scope["scope_id"])["state"] == "active"


@pytest.mark.parametrize("invalid", ["closed", "malformed"])
def test_invalid_saved_scope_is_blocked_before_resume_admission(tmp_path, monkeypatch, invalid):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session("invalid-scope", source="desktop")
        mid = db.append_message("invalid-scope", "user", "After two minutes verify the file and report")
        work = oc.register_owner_request(db, "invalid-scope", mid, hermes_home=tmp_path)
        record = db.create_or_get_scope("invalid-saved", {"kind": "owner", "instruction": "inspect report"}, {"objective": "inspect"})
        db.close_scope(record["scope_id"])
        saved = {"db_path": str(db.db_path), "scope_id": record["scope_id"]} if invalid == "closed" else "not-a-locator"
        store.update_work(work["id"], refs={"execution_scope": saved}, hermes_home=tmp_path)
        oc.wait(work["id"], until="2020-01-01T00:00:00Z", hermes_home=tmp_path)
        results = []
        def native_turn(*args, **kwargs):
            with monkeypatch.context() as context:
                for key in ("HERMES_OWNER_CONTINUATION_ID", "HERMES_OWNER_CONTINUATION_NONCE", "HERMES_BACKGROUND_DELIVERY"):
                    context.setenv(key, kwargs["env"][key])
                agent = agent_for(db, "invalid-scope")
                results.append(agent.run_conversation(kwargs["input"]))
                agent.close()
                return SimpleNamespace(returncode=1, stdout="", stderr="")
        oc.run_resume(work["id"], 1, hermes_home=tmp_path, runner=native_turn)
        current = store.get_work(work["id"], tmp_path)
        if results:
            assert results[0].get("error_type") != "execution_scope_preflight_blocked"
        assert not current["refs"].get("owner_obligation")
