"""Native peer transport carries a narrowed current assignment, never owner ingress."""
import uuid
from pathlib import Path


def prepare_peer_scope(target, assignment, hermes_home=None):
    from agent.execution_scope import capture_binding, derive_child, current_invocation_id
    binding = capture_binding()
    key = "autonomy-peer:" + uuid.uuid4().hex
    goal = {"target": target, **assignment}
    from tools.process_registry import CHECKPOINT_PATH
    facts = {"assignment_kind": "autonomy_peer", "process_id": "proc_" + uuid.uuid4().hex[:12],
             "process_checkpoint": str(CHECKPOINT_PATH)}
    if binding is not None:
        return derive_child(binding, key, goal, source_facts={
            **facts, "parent_invocation_id": current_invocation_id()})
    from hermes_cli.native_execution_scope import current_admin_ingress
    ingress = current_admin_ingress()
    if ingress is None or ingress.get("command") != "autonomy" or ingress.get("action") != "delegate":
        raise ValueError("Peer dispatch requires current structured execution authority")
    from agent.autonomy.owner_continuity import resolve_home
    from agent.execution_scope_policy import derive_scope
    from hermes_state import SessionDB
    home = Path(hermes_home) if hermes_home is not None else resolve_home()
    db_path = Path(home).resolve() / "state.db"
    source = {"kind": "derived", **facts, "assignment_id": key,
              "instruction": goal, "owner_ingress_id": ingress["id"],
              "owner_ingress_command": ingress["command"]}
    original = {"command": ingress["command"], "action": ingress["action"], "arguments": ingress["arguments"]}
    policy = derive_scope(original, runtime=None)
    policy = {**policy, "assignments": [goal]}
    source["owner_original_instruction"] = original
    with SessionDB(db_path) as db:
        row = db.create_or_get_scope(key, source, policy)
    return {"db_path": str(db_path), "scope_id": row["scope_id"], "assignment_id": key}


def close_peer_scope(locator):
    if locator:
        from hermes_state import SessionDB
        with SessionDB(Path(locator["db_path"])) as db:
            db.close_scope(locator["scope_id"])


def run_peer_process(argv, env, locator, *, timeout=600):
    """Register actual native execution without transferring stdout ownership."""
    import subprocess
    import time
    from hermes_state import SessionDB
    from tools.process_registry import ProcessSession, process_registry
    process = None
    with SessionDB(Path(locator["db_path"])) as db:
        record = db.get_scope(locator["scope_id"])
    if not record or record["state"] != "active":
        raise ValueError("Peer assignment has ended")
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        process = ProcessSession(id=record["source"]["process_id"], command="Hermes peer assignment",
            process=proc, pid=proc.pid, cwd=str(Path.cwd()), started_at=time.time(),
            host_start_time=process_registry._safe_host_start_time(proc.pid), execution_scope_locator=locator)
        with process_registry._lock:
            process_registry._prune_if_needed()
            process_registry._running[process.id] = process
        process_registry._write_checkpoint()
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except BaseException:
            failed_peer_outcome(locator, "native peer execution did not complete")
            proc.kill()
            proc.communicate()
            raise
        return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
    finally:
        if process is not None:
            process_registry._finish_exited(process, proc.returncode if proc.returncode is not None else -1)


def validate_peer_assignment(record, db):
    source = record["source"]
    if source.get("parent_scope_id"):
        action = db.get_scope_action(source["parent_scope_id"], source.get("parent_invocation_id"))
        if not action or action["state"] not in {"admitted", "completed"}:
            raise ValueError("Peer assignment has no admitted native parent action")
    elif not source.get("owner_ingress_id"):
        raise ValueError("Peer assignment has no native current authority")
    from tools.terminal_execution_scope import validate_registered_assignment
    validate_registered_assignment(record, db)


def failed_peer_outcome(locator, error):
    from hermes_state import SessionDB
    with SessionDB(Path(locator["db_path"])) as db:
        admitted = db.close_scope_tree_and_has_actions(locator["scope_id"])
    return {"sent": False, "error": error, "status": "uncertain" if admitted else "not_started",
            "effect_disposition": "unknown" if admitted else "not_started", "retryable": not admitted}
