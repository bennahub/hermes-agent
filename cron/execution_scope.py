"""Persist native cron assignment scope before its originating dispatch ends."""
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

# Runtime counters, provider routes and delivery telemetry do not change intent.
_ASSIGNMENT_FIELDS = (
    "prompt", "script", "no_agent", "monitor_script", "monitor_url", "skills",
    "context_from", "workdir", "deliver", "origin", "schedule", "repeat",
)


def assignment(job: dict[str, Any]) -> dict[str, Any]:
    """The stored assignment, without injected runtime history or script output."""
    result = {key: job.get(key) for key in _ASSIGNMENT_FIELDS}
    repeat = result.get("repeat")
    if isinstance(repeat, dict):
        result["repeat"] = {"times": repeat.get("times")}
    return result


def derive_job_scope(job: dict[str, Any]) -> None:
    """Attach inherited authority only from an admitted native tool dispatch."""
    from agent.execution_scope import capture_binding, derive_child

    binding = capture_binding()
    if binding is None:
        from hermes_cli.native_execution_scope import current_admin_ingress
        ingress = current_admin_ingress()
        if ingress is not None and ingress.get("command") == "cron" and ingress.get("action") in {"create", "edit"}:
            _derive_native_admin_scope(job, ingress)
        return
    from cron.jobs import _current_cron_store

    source_key = "cron:" + job["id"] + ":" + uuid.uuid4().hex
    job["execution_scope"] = derive_child(binding, source_key, assignment(job),
        source_facts={"assignment_kind": "cron", "job_id": job["id"],
                      "job_home": str(_current_cron_store().cron_dir.parent.resolve())})


def refresh_job_scope(previous: dict[str, Any], updated: dict[str, Any]) -> dict | None:
    """A new assignment requires current authority; native status writes retain it."""
    old_locator = previous.get("execution_scope")
    if assignment(previous) == assignment(updated):
        return None
    from agent.execution_scope import capture_binding

    from hermes_cli.native_execution_scope import current_admin_ingress
    ingress = current_admin_ingress()
    native_cron = ingress is not None and ingress.get("command") == "cron" and ingress.get("action") in {"create", "edit"}
    if capture_binding() is None and not native_cron:
        if old_locator is not None:
            raise ValueError("Changing a scoped cron assignment requires current execution authority")
        return None
    updated.pop("execution_scope", None)
    derive_job_scope(updated)
    return old_locator


def close_previous_job_scope(locator: dict | None) -> None:
    """Retire the old revision after its replacement is durably saved."""
    if locator is None:
        return
    from hermes_state import SessionDB

    with SessionDB(Path(locator["db_path"])) as db:
        db.close_scope(locator["scope_id"])


def bind_job_scope(agent: Any, job: dict[str, Any]) -> None:
    """Derive one runtime scope per exact native fire from the stored assignment."""
    from agent.execution_scope import _inherited_binding, derive_child

    locator = job.get("execution_scope")
    if locator is None:
        return
    execution_id = job.get("execution_id")
    if not isinstance(execution_id, str) or not execution_id:
        raise ValueError("Cron agent requires a native execution identity")
    binding = _inherited_binding(
        locator, runtime=None, source_key=locator.get("assignment_id"),
        assigned_goal=assignment(job),
    )
    try:
        parent = binding.db.get_scope(binding.scope_id)
        validate_job_assignment(parent, binding.db)
        source = parent["source"]
        claim_kind = "fire_claim" if job.get("fire_claim") else "run_claim"
        claim_owner = (job.get(claim_kind) or {}).get("by")
        source_key = "cron_run:" + job["id"] + ":" + execution_id
        child_locator = derive_child(
            binding, source_key, assignment(job),
            source_facts={
                "assignment_kind": "cron_run", "job_id": job["id"],
                "job_scope_id": locator["scope_id"], "job_home": source["job_home"],
                "execution_id": execution_id, "claim_kind": claim_kind,
                "claim_owner": claim_owner,
            },
        )
        validate_job_assignment(binding.db.get_scope(child_locator["scope_id"]), binding.db)
    finally:
        binding.db.close()
    agent._pending_inherited_execution_scope = child_locator
    agent._pending_inherited_execution_scope_source_key = source_key
    agent._pending_inherited_execution_scope_goal = assignment(job)


def validate_job_assignment(record: dict[str, Any], db: Any) -> None:
    """Reject retired assignments or runtime fires against native durable state."""
    from cron.executions import get_execution
    from cron.jobs import get_job, use_cron_store

    source = record.get("source", {})
    kind = source.get("assignment_kind")
    if kind not in {"cron", "cron_run"}:
        return
    job_id = source.get("job_id")
    job_home = source.get("job_home")
    if not isinstance(job_id, str) or not isinstance(job_home, str):
        raise ValueError("Cron assignment lacks native store identity")
    with use_cron_store(job_home):
        job = get_job(job_id)
    base_id = source.get("job_scope_id") if kind == "cron_run" else record["scope_id"]
    locator = (job or {}).get("execution_scope") or {}
    if (not job or locator.get("scope_id") != base_id
            or Path(locator.get("db_path", "")).resolve() != Path(db.db_path).resolve()
            or not job.get("enabled", True)
            or job.get("state") in {"paused", "completed", "error"}
            or job.get("paused_at")
            or assignment(job) != source.get("instruction")):
        raise ValueError("Cron assignment is no longer active")
    if kind == "cron":
        return
    base = db.get_scope(base_id)
    if not base or base.get("state") != "active":
        raise ValueError("Cron parent assignment is closed")
    owner = source.get("claim_owner")
    if owner and (job.get(source.get("claim_kind")) or {}).get("by") != owner:
        raise ValueError("Cron fire claim was replaced")
    execution_id = source.get("execution_id")
    if not isinstance(execution_id, str) or not execution_id:
        raise ValueError("Cron runtime has no execution identity")
    execution = get_execution(execution_id, store_home=Path(job_home))
    if (not execution or execution["job_id"] != job_id
            or execution["status"] not in {"claimed", "running"}):
        raise ValueError("Cron execution is no longer active")


@contextmanager
def job_admission_fence(record: dict[str, Any], db: Any):
    """Fence native assignment validation through the durable action claim only."""
    from cron.jobs import _jobs_lock, use_cron_store

    source = record.get("source", {})
    if source.get("assignment_kind") not in {"cron", "cron_run"}:
        yield
        return
    with use_cron_store(source["job_home"]), _jobs_lock(required=True):
        validate_job_assignment(record, db)
        yield


def execute_job_action(
    job: dict[str, Any],
    step: str,
    tool_name: str,
    arguments: dict[str, Any],
    execute: Callable[[dict[str, Any]], Any],
) -> tuple[bool, Any]:
    """Authorize one native fire step against its durable current assignment."""
    from agent.execution_scope import execute_native_scoped
    from cron.jobs import get_job

    locator = job.get("execution_scope")
    if not locator:
        return False, "Cron execution scope is missing; this job requires explicit authority migration."

    def current_assignment():
        current = get_job(job["id"])
        if (not current or current.get("execution_scope") != locator
                or not current.get("enabled", True)
                or current.get("state") in {"paused", "completed", "error"}
                or assignment(current) != assignment(job)):
            raise ValueError("Cron assignment is no longer current")
        return current

    try:
        current_assignment()
        claim = job.get("fire_claim") or job.get("run_claim") or {}
        fire_id = job.get("execution_id") or claim.get("by")
        if not isinstance(fire_id, str) or not fire_id:
            raise ValueError("Cron execution requires a native fire identity")

        def run(final_args):
            current_assignment()
            return execute(final_args)

        result = execute_native_scoped(
            locator, locator.get("assignment_id"), assignment(job), tool_name,
            arguments, run, invocation_id="cron:" + job["id"] + ":" + fire_id + ":" + step,
        )
        if isinstance(result, (tuple, list)) and len(result) == 2:
            return bool(result[0]), result[1]
        return False, str(result)
    except Exception as exc:
        return False, "Cron action was not admitted: " + type(exc).__name__


def run_job_script_scoped(
    job: dict[str, Any],
    script_path: str,
    *,
    workdir: str | None = None,
    cancel_event: Any = None,
    step: str = "script",
) -> tuple[bool, Any]:
    """Judge and execute the same script bytes, never a mutable path-only grant."""
    if job.get("native_continuation") is not None:
        return run_native_continuation(job)
    import hashlib
    import os
    import tempfile
    from cron.scheduler_script import _resolve_script_path, _run_job_script

    path, error = _resolve_script_path(script_path)
    if path is None:
        return False, error
    program = path.read_bytes()
    # Same directory preserves relative imports and the original cwd contract.
    descriptor, snapshot = tempfile.mkstemp(prefix=".scope-", suffix=path.suffix, dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(program)
        arguments = {
            "path": str(path), "program": program.decode("utf-8"),
            "sha256": hashlib.sha256(program).hexdigest(), "cwd": workdir or str(path.parent),
        }
        return execute_job_action(
            job, step, "cron_script", arguments,
            lambda _args: _run_job_script(snapshot, workdir=workdir or str(path.parent), cancel_event=cancel_event),
        )
    finally:
        Path(snapshot).unlink(missing_ok=True)


def run_native_continuation(job: dict[str, Any]) -> tuple[bool, str]:
    """Dispatch only the work identity that owns this exact native transport job."""
    from agent.autonomy import owner_continuity, store

    marker = job.get("native_continuation")
    if not isinstance(marker, dict):
        return False, "Invalid native continuation identity"
    work_id = marker.get("work_id")
    generation = marker.get("generation")
    home = marker.get("hermes_home")
    if not isinstance(work_id, str) or not isinstance(generation, int) or not isinstance(home, str):
        return False, "Invalid native continuation identity"
    work = store.get_work(work_id, home)
    refs = (work or {}).get("refs", {})
    native_job = refs.get("resume_job") or {}
    if (native_job.get("id") != job.get("id")
            or native_job.get("generation") != generation
            or refs.get("resume_generation") != generation):
        return False, "Native continuation no longer owns this job"
    code = owner_continuity.run_resume(work_id, generation, hermes_home=home)
    return code == 0, "[SILENT]" if code == 0 else "Native continuation failed"


def _derive_native_admin_scope(job, ingress):
    """Fresh parsed human admin action; generic programmatic callers never mint."""
    from cron.jobs import _current_cron_store
    from hermes_state import SessionDB
    from agent.execution_scope_policy import derive_scope
    home = _current_cron_store().cron_dir.parent.resolve()
    source_key = "cron-admin:" + job["id"] + ":" + uuid.uuid4().hex
    source = {"kind": "scheduled", "assignment_kind": "cron", "assignment_id": source_key,
              "instruction": assignment(job), "job_id": job["id"], "job_home": str(home),
              "owner_ingress_id": ingress["id"], "owner_ingress_command": ingress["command"]}
    original = {"command": ingress["command"], "action": ingress["action"], "arguments": ingress["arguments"]}
    scope = derive_scope(original, runtime=None)
    scope = {**scope, "assignments": [assignment(job)]}
    source["owner_original_instruction"] = original
    with SessionDB(home / "state.db") as db:
        record = db.create_or_get_scope(source_key, source, scope)
    job["execution_scope"] = {"db_path": str(home / "state.db"), "scope_id": record["scope_id"],
                              "assignment_id": source_key}
