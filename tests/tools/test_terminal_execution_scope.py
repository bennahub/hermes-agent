"""Real local spawn paths carry a per-invocation scope without leaking it."""
import json
import os
import time
import pytest
from tools.terminal_execution_scope import terminal_execution_scope, apply_child_execution_scope


@pytest.mark.parametrize("mode", ["foreground", "background", "pty"])
def test_native_subprocess_scope_is_per_invocation(tmp_path, monkeypatch, mode):
    from agent import execution_scope
    from tools.environments.local import LocalEnvironment
    from tools.process_registry import ProcessRegistry
    locator = {"db_path": str(tmp_path / "state.db"), "scope_id": "child-scope", "assignment_id": "terminal:invocation"}
    from types import SimpleNamespace
    from hermes_state import SessionDB
    db = SessionDB(tmp_path / "state.db")
    parent = db.create_or_get_scope("parent", {"kind": "owner"}, {"goal": "test"})
    db.claim_scope_action(parent["scope_id"], "invocation", "terminal", {"command": "test"})
    seen = []
    monkeypatch.setattr(execution_scope, "capture_binding", lambda: SimpleNamespace(db=db))
    monkeypatch.setattr(execution_scope, "current_invocation_id", lambda: "invocation", raising=False)
    def derive(binding, key, goal, *, source_facts):
        seen.append((key, goal))
        record = db.create_or_get_scope(key, {"kind": "derived", "parent_scope_id": parent["scope_id"], **source_facts}, {"goal": "test"})
        locator["scope_id"] = record["scope_id"]
        return locator
    monkeypatch.setattr(execution_scope, "derive_child", derive)
    monkeypatch.delenv("HERMES_EXECUTION_SCOPE", raising=False)
    command = 'printf %s "$HERMES_EXECUTION_SCOPE"'
    backend = LocalEnvironment(cwd=str(tmp_path))
    original_env = dict(backend.env)
    with terminal_execution_scope(command, "local", background=mode != "foreground"):
        if mode == "foreground":
            result = backend.execute(command, timeout=10)
            output = result["output"]
        else:
            registry = ProcessRegistry()
            process = registry.spawn_local(command, cwd=str(tmp_path), env_vars=backend.env, use_pty=mode == "pty")
            deadline = time.monotonic() + 10
            while not process.exited and time.monotonic() < deadline:
                time.sleep(0.02)
            assert process._completion_event.wait(10)
            output = process.output_buffer
    # Interactive shell setup can emit a job-control diagnostic before printf.
    output = output.removeprefix("bash: no job control in this shell\n")
    assert json.loads(output.strip()) == locator
    assert db.get_scope(locator["scope_id"])["state"] == "closed"
    assert seen == [("terminal:invocation", command)]
    assert backend.env == original_env
    assert "HERMES_EXECUTION_SCOPE" not in os.environ
    assert "HERMES_EXECUTION_SCOPE" not in apply_child_execution_scope({})
    db.close()


def test_nonlocal_guarded_spawn_refused_before_derivation(monkeypatch):
    from agent import execution_scope
    monkeypatch.setattr(execution_scope, "capture_binding", lambda: object())
    monkeypatch.setattr(execution_scope, "current_invocation_id", lambda: "invocation", raising=False)
    monkeypatch.setattr(execution_scope, "derive_child", lambda *args: pytest.fail("must not derive remote scope"))
    with pytest.raises(ValueError, match="local terminal backend"):
        with terminal_execution_scope("echo harmless", "ssh"):
            pytest.fail("unsupported spawn cannot run")


def test_terminal_assignment_requires_exact_live_native_process(tmp_path):
    import subprocess
    from hermes_state import SessionDB
    from tools.process_registry import process_registry
    from tools.terminal_execution_scope import validate_terminal_assignment
    db = SessionDB(tmp_path / "state.db")
    parent = db.create_or_get_scope("parent", {"kind": "owner"}, {"goal": "test"})
    db.claim_scope_action(parent["scope_id"], "call", "terminal", {})
    proc = subprocess.Popen(["sleep", "30"])
    checkpoint = tmp_path / "processes.json"
    source = {"kind": "derived", "assignment_kind": "terminal", "process_id": "proc_exact",
              "parent_scope_id": parent["scope_id"], "parent_invocation_id": "call",
              "process_checkpoint": str(checkpoint)}
    record = db.create_or_get_scope("terminal-test", source, {"goal": "test"})
    entry = {"session_id": "proc_exact", "pid": proc.pid,
             "host_start_time": process_registry._safe_host_start_time(proc.pid),
             "execution_scope_locator": {"scope_id": record["scope_id"]}}
    checkpoint.write_text(json.dumps([entry]))
    try:
        validate_terminal_assignment(record, db)
        # Background lifetime is the registered process, independent of parent
        # completion; the exact already-admitted parent action remains historical.
        db.complete_scope_action(parent["scope_id"], "call", {"session_id": "proc_exact"})
        db.close_scope(parent["scope_id"])
        validate_terminal_assignment(record, db)
        proc.terminate()
        proc.wait(timeout=5)
        with pytest.raises(ValueError, match="completed or is unavailable"):
            validate_terminal_assignment(record, db)
        assert db.get_scope(record["scope_id"])["state"] == "closed"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        db.close()


def test_registered_foreground_outcome_loss_is_not_replayed(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from hermes_state import SessionDB
    from agent import execution_scope
    from tools.environments.local import LocalEnvironment
    from tools.terminal_tool import _run_foreground
    db = SessionDB(tmp_path / "state.db")
    parent = db.create_or_get_scope("retry-parent", {"kind": "owner"}, {"goal": "write one marker"})
    binding = execution_scope.Binding(db, parent["scope_id"])
    monkeypatch.setattr(execution_scope, "capture_binding", lambda: binding)
    monkeypatch.setattr(execution_scope, "current_invocation_id", lambda: "retry-call")
    command = "printf x >> marker"
    db.claim_scope_action(binding.scope_id, "retry-call", "terminal", {"command": command})
    backend = LocalEnvironment(cwd=str(tmp_path))
    execute = backend.execute
    calls = []
    def lost_outcome(*args, **kwargs):
        calls.append(1)
        execute(*args, **kwargs)
        raise RuntimeError("transport lost after execution")
    monkeypatch.setattr(backend, "execute", lost_outcome)
    plan = SimpleNamespace(env_type="local", effective_task_id="retry", effective_timeout=10, cwd=str(tmp_path))
    with terminal_execution_scope(command, "local"):
        result = _run_foreground(command, backend, plan, task_id="retry", session_id=None,
            session_key="retry", workdir=str(tmp_path), approval_note=None, clear_interrupt=False)
    assert json.loads(result)["status"] == "uncertain"
    assert len(calls) == 1
    assert (tmp_path / "marker").read_text() == "x"
    db.close()


def test_reused_terminal_snapshot_never_replays_prior_invocation_authority(tmp_path, monkeypatch):
    from agent import execution_scope as scopes
    from hermes_state import SessionDB
    from tools.environments.local import LocalEnvironment
    from tools.terminal_tool import _run_foreground
    from types import SimpleNamespace
    monkeypatch.delenv("HERMES_EXECUTION_SCOPE", raising=False)
    db = SessionDB(tmp_path / "state.db")
    root = db.create_or_get_scope("snapshot-parent", {"kind": "owner"}, {"goal": "inspect current identity"})
    binding = scopes.Binding(db, root["scope_id"])
    monkeypatch.setattr(scopes, "capture_binding", lambda: binding)
    backend = LocalEnvironment(cwd=str(tmp_path))
    plan = SimpleNamespace(env_type="local", effective_task_id="snapshot", effective_timeout=10, cwd=str(tmp_path))
    command = 'printf %s "$HERMES_EXECUTION_SCOPE"'
    prior = None
    try:
        for index in range(2):
            call = "snapshot-call-" + str(index)
            db.claim_scope_action(binding.scope_id, call, "terminal", {"command": command})
            monkeypatch.setattr(scopes, "current_invocation_id", lambda: call)
            with terminal_execution_scope(command, "local"):
                expected = json.loads(apply_child_execution_scope({})["HERMES_EXECUTION_SCOPE"])
                result = json.loads(_run_foreground(command, backend, plan, task_id="snapshot", session_id=None,
                    session_key="snapshot", workdir=str(tmp_path), approval_note=None, clear_interrupt=False))
                actual = json.loads(result["output"].strip())
                assert actual == expected
                assert db.get_scope(actual["scope_id"])["state"] == "active"
                assert actual != prior
            assert db.get_scope(actual["scope_id"])["state"] == "closed"
            prior = actual
        # Upgrade a preexisting poisoned snapshot without granting an absent scope.
        with open(backend._snapshot_path, "a") as snapshot:
            snapshot.write('\nexport HERMES_EXECUTION_SCOPE=stale-owner-authority\n')
        assert backend.execute(command, timeout=10)["output"].strip() == ""
        assert "HERMES_EXECUTION_SCOPE" not in open(backend._snapshot_path).read()
    finally:
        db.close()
