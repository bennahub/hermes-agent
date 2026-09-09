"""Cron scope is a durable native assignment, never a caller-supplied grant."""
from types import SimpleNamespace

import pytest

from agent import execution_scope as scope
from cron.execution_scope import bind_job_scope
from cron.jobs import create_job, get_job, update_job, use_cron_store
from hermes_state import SessionDB


def test_scoped_job_persists_revisions_and_rejects_scope_injection(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with SessionDB(tmp_path / "state.db") as db, use_cron_store(tmp_path):
        parent = db.create_or_get_scope(
            "owner:cron:1", {"kind": "owner", "instruction": "Review financial definitions periodically"},
            {"allowed": ["financial review"], "prohibited": ["unrelated commands"]},
        )
        binding = scope.Binding(db, parent["scope_id"])
        monkeypatch.setattr(scope, "capture_binding", lambda: binding)
        job = create_job("Review MET definitions", "every 1h")
        locator = get_job(job["id"])["execution_scope"]
        record = db.get_scope(locator["scope_id"])
        assert record["scope"] == {**parent["scope"], "assignments": [record["source"]["instruction"]]}
        assert record["source"]["instruction"]["prompt"] == job["prompt"]
        assert record["source"]["assignment_id"].startswith("cron:" + job["id"] + ":")
        updated = update_job(job["id"], {"prompt": "Review updated MET definitions"})
        assert updated["execution_scope"]["scope_id"] != locator["scope_id"]
        assert db.get_scope(locator["scope_id"])["state"] == "closed"
        assert db.get_scope(updated["execution_scope"]["scope_id"])["scope"]["allowed"] == parent["scope"]["allowed"]
        with pytest.raises(ValueError, match="cannot be updated"):
            update_job(job["id"], {"execution_scope": locator})
        monkeypatch.setattr(scope, "capture_binding", lambda: None)
        with pytest.raises(ValueError, match="requires current execution authority"):
            update_job(job["id"], {"prompt": "Run an unrelated historical command"})
        unchanged = update_job(job["id"], {"last_status": "success"})
        assert unchanged["execution_scope"] == updated["execution_scope"]
        from cron.executions import create_execution
        child = SimpleNamespace()
        unchanged["execution_id"] = create_execution(job["id"], source="test")["id"]
        bind_job_scope(child, unchanged)
        assert child._pending_inherited_execution_scope["scope_id"] != unchanged["execution_scope"]["scope_id"]
        assert child._pending_inherited_execution_scope_goal["prompt"] == updated["prompt"]


def test_unscoped_legacy_job_is_not_minted_from_its_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(scope, "capture_binding", lambda: None)
    with use_cron_store(tmp_path):
        job = create_job("Historical scheduler prompt", "every 1h")
        assert not get_job(job["id"]).get("execution_scope")
        child = SimpleNamespace()
        bind_job_scope(child, job)
        assert not hasattr(child, "_pending_inherited_execution_scope")


def test_native_action_rechecks_current_assignment_and_binds_fire(tmp_path, monkeypatch):
    from cron.execution_scope import execute_job_action

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with SessionDB(tmp_path / "state.db") as db, use_cron_store(tmp_path):
        parent = db.create_or_get_scope("owner:fire", {"instruction": "Read MET"},
                                        {"allowed": ["read MET"]})
        monkeypatch.setattr(scope, "capture_binding", lambda: scope.Binding(db, parent["scope_id"]))
        job = create_job("Read MET", "every 1h")
        job["execution_id"] = "native-fire-1"
        seen = []

        def admitted(locator, source_key, goal, name, arguments, execute, *, invocation_id):
            seen.append((locator, source_key, goal, name, arguments, invocation_id))
            return execute(arguments)

        monkeypatch.setattr(scope, "execute_native_scoped", admitted)
        assert execute_job_action(job, "monitor", "web_fetch", {"url": "https://example.test"},
                                  lambda args: (True, args["url"])) == (True, "https://example.test")
        assert seen[0][1] == job["execution_scope"]["assignment_id"]
        assert seen[0][2]["prompt"] == "Read MET"
        assert seen[0][-1] == "cron:" + job["id"] + ":native-fire-1:monitor"

        def changed_during_admission(locator, source_key, goal, name, arguments, execute,
                                     *, invocation_id):
            update_job(job["id"], {"enabled": False, "state": "paused"})
            return execute(arguments)

        monkeypatch.setattr(scope, "execute_native_scoped", changed_during_admission)
        effects = []
        ok, _ = execute_job_action(job, "script", "cron_script", {"program": "echo MET"},
                                   lambda args: effects.append(args))
        assert not ok
        assert effects == []


def test_native_missing_scope_never_executes(tmp_path, monkeypatch):
    from cron.execution_scope import execute_job_action

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(scope, "capture_binding", lambda: None)
    with use_cron_store(tmp_path):
        job = create_job("Legacy assignment", "every 1h")
        effects = []
        ok, error = execute_job_action(job, "script", "cron_script", {},
                                       lambda args: effects.append(args))
        assert not ok
        assert "migration" in error
        assert effects == []


def test_native_continuation_uses_exact_work_identity_never_script(monkeypatch):
    from agent.autonomy import owner_continuity, store
    from cron.execution_scope import run_job_script_scoped
    from cron import scheduler_script

    marker = {"work_id": "work-1", "generation": 7, "hermes_home": "/native/home"}
    job = {"id": "job-1", "native_continuation": marker}
    work = {"refs": {"resume_generation": 7,
                     "resume_job": {"id": "job-1", "generation": 7}}}
    monkeypatch.setattr(store, "get_work", lambda work_id, home: work)
    monkeypatch.setattr(scheduler_script, "_resolve_script_path",
                        lambda path: pytest.fail("Native transport must not inspect a script"))
    calls = []
    monkeypatch.setattr(owner_continuity, "run_resume",
                        lambda work_id, generation, **kwargs: calls.append((work_id, generation, kwargs)) or 0)
    assert run_job_script_scoped(job, "/arbitrary/untrusted.py") == (True, "[SILENT]")
    assert calls == [("work-1", 7, {"hermes_home": "/native/home"})]
    work["refs"]["resume_job"]["id"] = "different-job"
    assert not run_job_script_scoped(job, "/arbitrary/untrusted.py")[0]
    assert len(calls) == 1


def test_script_executes_admitted_bytes_despite_original_replacement(tmp_path, monkeypatch):
    from cron import execution_scope as cron_scope, scheduler_script

    original = tmp_path / "collect.py"
    original.write_text("print('current assignment')")
    monkeypatch.setattr(scheduler_script, "_resolve_script_path", lambda value: (original, None))
    captured = []

    def admit(job, step, name, arguments, execute):
        assert arguments["program"] == "print('current assignment')"
        original.write_text("print('historical unrelated command')")
        return execute(arguments)

    def run(snapshot, **kwargs):
        snapshot = type(original)(snapshot)
        captured.append(snapshot)
        assert snapshot.read_text() == "print('current assignment')"
        return True, "current assignment"

    monkeypatch.setattr(cron_scope, "execute_job_action", admit)
    monkeypatch.setattr(scheduler_script, "_run_job_script", run)
    assert cron_scope.run_job_script_scoped({"id": "job"}, str(original)) == (True, "current assignment")
    assert len(captured) == 1
    assert not captured[0].exists()


def test_cron_run_scope_ends_without_closing_recurring_assignment(tmp_path, monkeypatch):
    from cron.execution_scope import validate_job_assignment, job_admission_fence
    from cron.executions import create_execution, finish_execution
    from cron.jobs import claim_job_for_fire

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with SessionDB(tmp_path / "state.db") as db, use_cron_store(tmp_path):
        parent = db.create_or_get_scope("owner:runs", {"instruction": "Read MET"}, {"allowed": ["MET"]})
        monkeypatch.setattr(scope, "capture_binding", lambda: scope.Binding(db, parent["scope_id"]))
        job = create_job("Read MET", "every 1h")
        base_id = job["execution_scope"]["scope_id"]
        first_fire = create_execution(job["id"], source="test")["id"]
        job["execution_id"] = first_fire
        first = SimpleNamespace()
        bind_job_scope(first, job)
        first_id = first._pending_inherited_execution_scope["scope_id"]
        first_record = db.get_scope(first_id)
        with job_admission_fence(first_record, db):
            validate_job_assignment(first_record, db)
        finish_execution(first_fire, success=True)
        with pytest.raises(ValueError, match="execution is no longer active"):
            validate_job_assignment(first_record, db)
        db.close_scope(first_id)
        assert db.get_scope(base_id)["state"] == "active"

        job = claim_job_for_fire(job["id"], return_job=True)
        job["execution_id"] = create_execution(job["id"], source="test")["id"]
        second = SimpleNamespace()
        bind_job_scope(second, job)
        second_id = second._pending_inherited_execution_scope["scope_id"]
        assert second_id != first_id
        validate_job_assignment(db.get_scope(second_id), db)
        update_job(job["id"], {"fire_claim": {"by": "replacement-native-claim"}})
        with pytest.raises(ValueError, match="claim was replaced"):
            validate_job_assignment(db.get_scope(second_id), db)
        update_job(job["id"], {"fire_claim": job["fire_claim"]})
        update_job(job["id"], {"enabled": False, "state": "paused"})
        with pytest.raises(ValueError, match="no longer active"):
            validate_job_assignment(db.get_scope(second_id), db)


@pytest.mark.parametrize("lock_result", [False, None, OSError("lock unavailable")])
def test_required_cron_admission_lock_fails_closed(tmp_path, monkeypatch, lock_result):
    from cron import jobs
    from cron.execution_scope import job_admission_fence

    def acquire(*args):
        if isinstance(lock_result, Exception):
            raise lock_result
        return lock_result

    monkeypatch.setattr(jobs, "_acquire_flock", acquire)
    record = {"source": {"assignment_kind": "cron", "job_home": str(tmp_path)}}
    admitted = []
    with pytest.raises(RuntimeError, match="cross-process jobs lock"):
        with job_admission_fence(record, None):
            admitted.append(True)
    assert admitted == []
