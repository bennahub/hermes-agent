"""Per-invocation native terminal scope and registered process lifetime."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
from pathlib import Path
import time
import uuid

_CHILD_SCOPE = ContextVar("terminal_child_execution_scope", default=None)


@contextmanager
def terminal_execution_scope(command, backend, *, background=False):
    from agent.execution_scope import capture_binding, current_invocation_id, derive_child
    binding = capture_binding()
    if binding is None:
        yield
        return
    if backend != "local":
        raise ValueError("Execution scope inheritance requires the local terminal backend; remote scope transport is unavailable")
    invocation = current_invocation_id()
    if not invocation:
        raise ValueError("Terminal execution is missing its admitted invocation identity")
    from tools.process_registry import CHECKPOINT_PATH
    process_id = "proc_" + uuid.uuid4().hex[:12]
    locator = derive_child(binding, "terminal:" + invocation, command, source_facts={
        "assignment_kind": "terminal", "process_id": process_id,
        "parent_invocation_id": invocation, "process_checkpoint": str(CHECKPOINT_PATH),
    })
    state = {"locator": locator, "process_id": process_id, "command": command,
             "background": background, "process": None, "registered": False}
    token = _CHILD_SCOPE.set(state)
    try:
        yield
    finally:
        _CHILD_SCOPE.reset(token)
        if not background or not state["registered"]:
            binding.db.close_scope(locator["scope_id"])
            process = state["process"]
            if process is not None:
                from tools.process_registry import process_registry
                code = process.process.poll()
                process_registry._finish_exited(process, code if code is not None else -1)


def apply_child_execution_scope(env):
    state = _CHILD_SCOPE.get()
    if state is None:
        return env
    return {**env, "HERMES_EXECUTION_SCOPE": json.dumps(state["locator"], sort_keys=True, separators=(",", ":"))}


def native_process_fields():
    """Reserved native identity for the background registry before Popen runs."""
    state = _CHILD_SCOPE.get()
    if not state or not state["background"]:
        return {}
    return {"id": state["process_id"], "execution_scope_locator": state["locator"]}


def native_process_started(process):
    state = _CHILD_SCOPE.get()
    if state and process.id == state["process_id"]:
        state["process"] = process
        state["registered"] = True


def register_foreground_process(proc, cwd):
    """Track lifetime metadata only; BaseEnvironment still owns stdout and wait."""
    state = _CHILD_SCOPE.get()
    if not state or state["background"]:
        return
    from tools.process_registry import ProcessSession, process_registry
    process = ProcessSession(id=state["process_id"], command=state["command"],
        process=proc, pid=proc.pid, cwd=cwd, started_at=time.time(),
        host_start_time=process_registry._safe_host_start_time(proc.pid),
        execution_scope_locator=state["locator"])
    with process_registry._lock:
        process_registry._prune_if_needed()
        process_registry._running[process.id] = process
    state["process"] = process
    state["registered"] = True
    process_registry._write_checkpoint()


def close_process_execution_scope(process):
    locator = getattr(process, "execution_scope_locator", None)
    if not locator:
        return
    from hermes_state import SessionDB
    db = SessionDB(Path(locator["db_path"]))
    try:
        db.close_scope(locator["scope_id"])
    finally:
        db.close()


def validate_terminal_assignment(record, db):
    """Validate the exact registered native process, including checkpoint recovery."""
    source = record["source"]
    if source.get("assignment_kind") != "terminal":
        return
    action = db.get_scope_action(source["parent_scope_id"], source["parent_invocation_id"])
    if not action or action["state"] not in {"admitted", "completed"}:
        raise ValueError("Terminal assignment has no admitted parent invocation")
    validate_registered_assignment(record, db)


def validate_registered_assignment(record, db):
    """Shared exact native ProcessSession lifetime check for scoped transports."""
    source = record["source"]
    from tools.process_registry import process_registry
    # A child can enter before its spawning thread has checkpointed Popen. This
    # bounded wait is registration coordination, never a freshness authority test.
    deadline = time.monotonic() + 2
    while True:
        process = process_registry.get(source["process_id"])
        if process is not None:
            valid = (not process.exited and process.host_start_time is not None
                     and process_registry._host_pid_is_ours(process.pid, process.host_start_time)
                     and (process.execution_scope_locator or {}).get("scope_id") == record["scope_id"])
            if valid:
                return
            break
        try:
            entries = json.loads(Path(source["process_checkpoint"]).read_text())
        except (OSError, ValueError):
            entries = []
        entries = entries if isinstance(entries, list) else []
        entry = next((item for item in entries if isinstance(item, dict) and item.get("session_id") == source["process_id"]), None)
        if entry:
            if (entry.get("host_start_time") is not None
                    and (entry.get("execution_scope_locator") or {}).get("scope_id") == record["scope_id"]
                    and process_registry._host_pid_is_ours(entry.get("pid"), entry["host_start_time"])):
                return
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.02)
    db.close_scope(record["scope_id"])
    raise ValueError("Terminal assignment's registered native process has completed or is unavailable")


def terminal_process_was_started():
    state = _CHILD_SCOPE.get()
    return bool(state and state["registered"])
