"""Native source-bound execution policy and exact tool admission.

Conversation reconstruction never calls a mint. The separate policy model sees
only a frozen scope and the final invocation, not the executor's transcript.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from agent.execution_scope_observations import compilation_context
from agent.execution_scope_policy import PolicyUnavailable, derive_scope, judge_action


def _current_owner_ingress(turn_id: str | None) -> bool:
    """True only when this exact turn was minted at authenticated owner ingress."""
    if not turn_id:
        return False
    try:
        from tools.owner_task_authority import turn_authority, turn_execution_source
        return bool(turn_authority(turn_id) or turn_execution_source(turn_id))
    except Exception:
        return False


@dataclass
class Binding:
    db: Any
    scope_id: str
    runtime: dict | None = None
    work_id: str | None = None
    work_home: str | None = None
    generation: Any = None
    revoked: bool = False
    failures: int = 0
    owns_db: bool = False
    amendment_pending: bool = False
    amendment_record: dict | None = None
    amendment_ready: Any = field(default_factory=threading.Event)
    lock: Any = field(default_factory=threading.RLock)


@dataclass
class _Receipt:
    binding: Binding
    name: str
    digest: str
    invocation_id: str
    registry_entered: bool = False
    turn_id: str | None = None


_CURRENT = contextvars.ContextVar("execution_scope_receipt", default=None)
_TURNS: dict[str, Binding | None] = {}
_LOCK = threading.RLock()


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def capture_binding() -> Binding | None:
    receipt = _CURRENT.get()
    return receipt.binding if receipt else None


def current_invocation_id() -> str | None:
    receipt = _CURRENT.get()
    return receipt.invocation_id if receipt else None


def get_binding(turn_id: str | None) -> Binding | None:
    with _LOCK:
        return _TURNS.get(turn_id or "")


def _record(binding: Binding) -> dict:
    if binding.revoked:
        raise ValueError("Execution turn authority has ended")
    record = binding.db.get_scope(binding.scope_id)
    if not record or record["state"] != "active":
        raise ValueError("Execution scope is not active")
    if binding.amendment_pending:
        raise ValueError("A fresh owner amendment is being admitted")
    source = record["source"]
    if source.get("assignment_kind") == "terminal":
        from tools.terminal_execution_scope import validate_terminal_assignment
        terminal = record
        if source.get("runtime_parent_scope_id"):
            terminal = binding.db.get_scope(source["runtime_parent_scope_id"])
            if not terminal or terminal["state"] != "active":
                raise ValueError("Terminal assignment has ended")
        validate_terminal_assignment(terminal, binding.db)
    elif source.get("assignment_kind") == "autonomy_peer":
        from agent.autonomy.peer_execution_scope import validate_peer_assignment
        validate_peer_assignment(record, binding.db)
    elif source.get("assignment_kind") in {"cron", "cron_run"}:
        from cron.execution_scope import validate_job_assignment
        validate_job_assignment(record, binding.db)
    if binding.work_id:
        from agent.autonomy import store
        work = store.get_work(binding.work_id, hermes_home=binding.work_home)
        if (not work or work["state"] not in {"working", "waiting", "investigating", "actionable"}
                or work["refs"].get("owner_stop")
                or work["refs"].get("execution_amendment")
                or work["refs"].get("execution_scope", {}).get("scope_id") != binding.scope_id
                or work["refs"].get("resume_generation") != binding.generation):
            raise ValueError("Structured continuation authority is no longer current")
    return record


def locator(binding: Binding) -> dict:
    return {"db_path": str(binding.db.db_path), "scope_id": binding.scope_id}


def derive_child(binding: Binding, source_key: str, goal: Any, *, source_facts: dict | None = None) -> dict:
    """Assign a native child exactly the parent's frozen permission ceiling."""
    parent = _record(binding)
    source = {"kind": "derived", "parent_scope_id": binding.scope_id,
              "assignment_id": source_key, "instruction": goal}
    from agent.execution_scope_observations import child_work_reference
    if work_ref := child_work_reference(binding):
        source["native_work_ref"] = work_ref
    if source_facts:
        if set(source_facts) & set(source):
            raise ValueError("Native source facts cannot replace assignment identity")
        source.update(source_facts)
    narrowed = {**parent["scope"], "assignments": [*parent["scope"].get("assignments", []), goal]}
    record = binding.db.create_or_get_scope("derived:" + source_key, source, narrowed)
    if record["state"] != "active":
        raise ValueError("Completed child assignment cannot be reopened")
    return {"db_path": str(binding.db.db_path), "scope_id": record["scope_id"],
            "assignment_id": source_key}



def attach_work_scope(agent, work):
    """Bind already frozen current authority to a newly registered native work."""
    binding = get_binding(getattr(agent, "_current_turn_id", None))
    if binding is None:
        return
    from agent.autonomy import store
    from agent.autonomy.owner_continuity import resolve_home
    home = str(resolve_home())
    changed = store.update_work(work["id"], refs={"execution_scope": locator(binding)},
        hermes_home=home, expected_state=work["state"],
        expected_refs={"owner_request": work["refs"]["owner_request"],
                       "execution_scope": work["refs"].get("execution_scope"), "owner_stop": None})
    if changed is None:
        raise PolicyUnavailable("Native work changed before current execution scope could attach")
    binding.work_id, binding.work_home = work["id"], home
    binding.generation = changed["refs"].get("resume_generation")

def _inherited_binding(value: dict, *, runtime: dict | None,
                       source_key: str | None = None, assigned_goal: Any = None) -> Binding:
    from hermes_state import SessionDB
    from pathlib import Path
    if not isinstance(value, dict) or not isinstance(value.get("db_path"), str):
        raise ValueError("Invalid inherited execution authority")
    from hermes_constants import get_hermes_home, get_default_hermes_root
    path = Path(value["db_path"]).resolve()
    if not path.is_absolute() or not path.is_file():
        raise ValueError("Inherited execution authority database is unavailable")
    root = get_default_hermes_root().resolve()
    allowed = {get_hermes_home().resolve() / "state.db", root / "state.db"}
    is_profile = path.name == "state.db" and path.parent.parent == root / "profiles"
    if path not in allowed and not is_profile:
        raise ValueError("Inherited scope must belong to a native Hermes state database")
    db = SessionDB(path)
    binding = Binding(db, value.get("scope_id", ""), runtime, owns_db=True)
    try:
        record = _record(binding)
        source = record["source"]
        expected = source_key or value.get("assignment_id")
        if (source.get("kind") not in {"derived", "scheduled"}
                or not expected or source.get("assignment_id") != expected
                or (assigned_goal is not None and source.get("instruction") != assigned_goal)):
            raise ValueError("Inherited assignment identity does not match")
    except Exception:
        db.close()
        raise
    # Original goal may be enriched for API context; it never replaces the
    # stored assignment or broadens the inherited policy.
    return binding


def bind_agent_turn(agent: Any, original_user_message: Any, persisted_row: dict,
                    display_kind: str | None = None) -> None:
    from tools.owner_task_authority import consume_execution_source, turn_authority

    turn_id = str(agent._current_turn_id)
    with _LOCK:
        _TURNS[turn_id] = None  # Missing/malformed authority fails closed.
    grant = turn_authority(turn_id)
    source = (grant or {}).get("execution_source") or consume_execution_source(agent)
    inherited = getattr(agent, "_pending_inherited_execution_scope", None)
    inherited_key = getattr(agent, "_pending_inherited_execution_scope_source_key", None)
    inherited_goal = getattr(agent, "_pending_inherited_execution_scope_goal", None)
    agent._pending_inherited_execution_scope = None
    agent._pending_inherited_execution_scope_source_key = None
    agent._pending_inherited_execution_scope_goal = None
    if inherited is None and os.environ.get("HERMES_EXECUTION_SCOPE"):
        inherited = json.loads(os.environ["HERMES_EXECUTION_SCOPE"])
    work_id = getattr(agent, "_owner_continuity_work_id", None)
    db = getattr(agent, "_session_db", None)
    decision_expected_refs = {}
    # CLI peer/internal transport wrappers are never fresh owner ingress.
    from agent.message_projection import projection
    projected = projection(persisted_row.get("display_metadata") or {})
    if display_kind or (projected and projected.get("origin") != "owner"):
        source = None
    if projected and projected.get("origin") == "peer":
        inherited = (persisted_row.get("display_metadata") or {}).get("execution_scope")
        inherited_key = "a2a:" + str(projected.get("event_id") or "")
        inherited_goal = original_user_message
    if not (source or work_id or inherited):
        return
    runtime = agent._current_main_runtime()
    if source:
        if db is None or type(persisted_row.get("_row_id")) is not int:
            return
        source_key = f"owner:{agent.session_id}:{persisted_row['_row_id']}"
        original = {"kind": "owner", "ingress_id": source["id"],
                    "session_id": agent.session_id, "message_id": persisted_row["_row_id"],
                    "instruction": source["instruction"]}
        record = db.get_scope_for_source(source_key)
        if record is None:
            try:
                record = db.create_or_get_scope(
                    source_key, original, derive_scope(original["instruction"], runtime=runtime, **compilation_context(db, agent.session_id)))
            except PolicyUnavailable:
                return
        from agent.autonomy.owner_decision import validate_bound_owner_decision
        try:
            decision_expected_refs = validate_bound_owner_decision(agent, record, persisted_row) or {}
        except ValueError:
            return
        binding = Binding(db, record["scope_id"], runtime)
    elif work_id:
        from agent.autonomy import store
        from agent.autonomy.owner_continuity import _source_row, resolve_home
        home = str(resolve_home())
        work = store.get_work(work_id, hermes_home=home)
        if not work or work["state"] != "working":
            return
        saved = work["refs"].get("execution_scope")
        if saved:
            if db is None or str(db.db_path) != saved["db_path"]:
                return
            binding = Binding(db, saved["scope_id"], runtime)
        else:
            # Native pre-upgrade active work has an exact durable original
            # source. Lazy additive adoption never uses the resumed prompt.
            owner = work["refs"]["owner_request"]
            row = _source_row(db, owner["session_id"], owner["message_id"])
            key = f"work:{work_id}"
            record = db.get_scope_for_source(key)
            if record is None:
                try:
                    record = db.create_or_get_scope(key, {"kind": "continuation", "work_id": work_id,
                        "owner_request": owner, "instruction": row["content"]},
                        derive_scope(row["content"], runtime=runtime, **compilation_context(db, agent.session_id)))
                except PolicyUnavailable:
                    return
            binding = Binding(db, record["scope_id"], runtime)
    elif inherited:
        binding = _inherited_binding(inherited, runtime=runtime, source_key=inherited_key,
                                     assigned_goal=inherited_goal)
        parent = _record(binding)
        source_kind = parent["source"].get("assignment_kind")
        if source_kind in {"terminal", "cron"} or parent["source"].get("kind") == "scheduled":
            facts = {"runtime_parent_scope_id": binding.scope_id, "assignment_kind": source_kind or "cron"}
            child = derive_child(binding, "runtime:" + turn_id, parent["source"]["instruction"], source_facts=facts)
            binding.scope_id = child["scope_id"]
    else:
        return
    try:
        if work_id:
            from agent.autonomy import store
            from agent.autonomy.owner_continuity import resolve_home
            binding.work_id, binding.work_home = work_id, str(resolve_home())
            work = store.get_work(work_id, hermes_home=binding.work_home)
            binding.generation = work["refs"].get("resume_generation")
            changed = store.update_work(work_id, refs={"execution_scope": locator(binding),
                                    **({"execution_amendment": None} if decision_expected_refs else {})},
                              hermes_home=binding.work_home, expected_state="working" if decision_expected_refs else work["state"],
                              expected_refs={"resume_generation": binding.generation,
                                             "execution_scope": work["refs"].get("execution_scope"),
                                             **decision_expected_refs})
            if changed is None:
                if binding.owns_db:
                    binding.db.close()
                return
        _record(binding)
    except (PolicyUnavailable, ValueError, KeyError, TypeError):
        if locals().get("binding") is not None and getattr(binding, "owns_db", False):
            binding.db.close()
        return
    with _LOCK:
        _TURNS[turn_id] = binding


def close_turn(agent: Any) -> None:
    turn_id = str(getattr(agent, "_current_turn_id", "") or "")
    while True:
        binding = get_binding(turn_id)
        if binding is None:
            return
        # Admission and amendment publication use Binding -> _LOCK -> work.
        with binding.lock:
            with _LOCK:
                if _TURNS.get(turn_id) is not binding:
                    continue
                _TURNS.pop(turn_id)
                binding.revoked = True
                if binding.work_id:
                    from agent.autonomy import store
                    work = store.get_work(binding.work_id, hermes_home=binding.work_home)
                    if work and work["state"] in {"working", "waiting", "investigating", "actionable"}:
                        return
                try:
                    binding.db.close_scope(binding.scope_id)
                finally:
                    if binding.owns_db:
                        binding.db.close()
                return


def _validate_amendable_source(record: dict) -> None:
    source = record["source"]
    if source.get("assignment_kind") or source.get("kind") == "derived":
        raise PolicyUnavailable("A native assignment cannot acquire expanded owner authority through steering")


def amend_agent_turn(agent: Any, text: Any, source_id: str) -> None:
    """Authenticated ingress calls this after accepting a fresh owner correction."""
    previous = begin_amendment(agent, source_id, text=text)
    if previous is None:
        raise PolicyUnavailable("No active execution scope to amend")
    finish_amendment(agent, text, source_id, previous)


def _amend_from_record(agent: Any, text: Any, source_id: str, previous: Binding, record: dict) -> None:
    turn_id = str(getattr(agent, "_current_turn_id", "") or "")
    _validate_amendable_source(record)
    source = {"kind": "owner_amendment", "ingress_id": source_id,
              "prior_scope_id": previous.scope_id, "instruction": text}
    scope = derive_scope({"prior_authority": record["scope"], "current_owner_instruction": text},
                         runtime=previous.runtime, **compilation_context(previous.db, agent.session_id))
    amended = previous.db.create_or_get_scope(source_id, source, scope)
    replacement = Binding(previous.db, amended["scope_id"], previous.runtime,
                          previous.work_id, previous.work_home, previous.generation)
    replacement.owns_db = previous.owns_db
    published = False
    try:
        with previous.lock:
            with _LOCK:
                if (previous.revoked or _TURNS.get(turn_id) is not previous
                        or previous.db.get_scope(previous.scope_id)["state"] != "active"
                        or amended["state"] != "active"):
                    raise PolicyUnavailable("Execution turn changed before the owner amendment could bind")
                if previous.work_id:
                    from agent.autonomy import store
                    work = store.get_work(previous.work_id, hermes_home=previous.work_home)
                    if not work or work["state"] not in {"working", "waiting", "investigating", "actionable"}:
                        raise PolicyUnavailable("Work ended before the owner amendment could bind")
                    changed = store.update_work(
                        previous.work_id, refs={"execution_scope": locator(replacement), "execution_amendment": None},
                        hermes_home=previous.work_home, expected_state=work["state"],
                        expected_refs={"execution_scope": locator(previous),
                                       "resume_generation": previous.generation, "owner_stop": None,
                                       "pending_owner_result": None,
                                       "execution_amendment": previous.amendment_record},
                    )
                    if changed is None:
                        raise PolicyUnavailable("Work changed before the owner amendment could bind")
                previous.revoked = True
                _TURNS[turn_id] = replacement
                published = True
                previous.db.close_scope(previous.scope_id)
    finally:
        if not published:
            previous.db.close_scope(replacement.scope_id)


def begin_amendment(agent: Any, source_id: str, *, text: Any = None) -> Binding | None:
    turn_id = str(getattr(agent, "_current_turn_id", "") or "")
    binding = get_binding(turn_id)
    if binding is None:
        return None
    with binding.lock:
        with _LOCK:
            if _TURNS.get(turn_id) is not binding:
                raise PolicyUnavailable("Execution turn changed before owner steering")
            record = _record(binding)
            _validate_amendable_source(record)
            if binding.work_id:
                if text is None:
                    raise PolicyUnavailable("Original owner amendment input is required")
                from copy import deepcopy
                from agent.autonomy import store
                marker = {"id": source_id, "state": "pending", "instruction": deepcopy(text),
                          "prior_scope": locator(binding), "generation": binding.generation}
                work = store.get_work(binding.work_id, hermes_home=binding.work_home)
                changed = store.update_work(
                    binding.work_id, refs={"execution_amendment": marker},
                    hermes_home=binding.work_home, expected_state=work["state"],
                    expected_refs={"execution_scope": locator(binding), "resume_generation": binding.generation,
                                   "execution_amendment": None, "pending_owner_result": None, "owner_stop": None},
                )
                if changed is None:
                    raise PolicyUnavailable("Work changed before owner amendment acceptance")
                binding.amendment_record = marker
            binding.amendment_pending = True
            binding.amendment_ready.clear()
    return binding


def cancel_amendment(binding: Binding | None, *, discard: bool = True) -> None:
    if binding is not None:
        with binding.lock:
            if discard and binding.work_id and binding.amendment_record:
                from agent.autonomy import store
                store.update_work(
                    binding.work_id, refs={"execution_amendment": None}, hermes_home=binding.work_home,
                    expected_refs={"execution_scope": locator(binding), "resume_generation": binding.generation,
                                   "execution_amendment": binding.amendment_record, "pending_owner_result": None},
                )
            binding.amendment_pending = False
            binding.amendment_ready.set()


def finish_amendment(agent: Any, text: Any, source_id: str, binding: Binding | None) -> None:
    if binding is None:
        return
    try:
        record = binding.db.get_scope(binding.scope_id)
        if not record or record["state"] != "active":
            raise PolicyUnavailable("Owner amendment's prior authority has ended")
        _amend_from_record(agent, text, source_id, binding, record)
    except Exception:
        with binding.lock:
            binding.revoked = True
            if binding.work_id:
                from agent.autonomy import store
                from agent.execution_scope_amendment import settle_pending_amendment
                work = store.get_work(binding.work_id, hermes_home=binding.work_home)
                if work:
                    settle_pending_amendment(work, binding.work_home)
            binding.db.close_scope(binding.scope_id)
        raise
    finally:
        cancel_amendment(binding, discard=False)


def execute_native_scoped(value: dict, source_key: str, assigned_goal: Any,
                          tool_name: str, arguments: dict, execute: Callable[[dict], Any], *,
                          invocation_id: str, runtime: dict | None = None) -> Any:
    binding = _inherited_binding(value, runtime=runtime, source_key=source_key,
                                 assigned_goal=assigned_goal)
    native_turn = "native:" + invocation_id
    with _LOCK:
        _TURNS[native_turn] = binding
    try:
        return execute_scoped(tool_name, arguments, execute,
                              turn_id=native_turn, invocation_id=invocation_id)
    finally:
        with _LOCK:
            _TURNS.pop(native_turn, None)
        binding.revoked = True
        binding.db.close()


class ExecutionScopeDenied(str):
    """Native gate outcome; executor need not infer effect state from prose."""

    def __new__(cls, payload: str, *, uncertain: bool = False):
        value = super().__new__(cls, payload)
        value.uncertain = uncertain
        return value


def _blocked(reason: str, *, uncertain: bool = False, recovery: dict | None = None) -> str:
    payload = {
        "error": reason,
        "error_type": "execution_scope_uncertain" if uncertain else "execution_scope_denied",
        "effect_disposition": "uncertain" if uncertain else "not_started",
        "retryable": False,
    }
    if recovery is not None and not uncertain:
        payload["recovery"] = recovery
    return ExecutionScopeDenied(json.dumps(payload), uncertain=uncertain)


def mark_attempt_uncertain(turn_id: str | None) -> int:
    """Revoke late workers; distinguish no admission from unresolved effects."""
    if not turn_id:
        return 0
    while True:
        binding = get_binding(turn_id)
        if binding is None:
            return 0
        with binding.lock:
            with _LOCK:
                if _TURNS.get(turn_id) is not binding:
                    continue
                binding.revoked = True
                return binding.db.mark_scope_attempt_uncertain(binding.scope_id, turn_id)


def execute_scoped(tool_name: str, arguments: dict, execute: Callable[[dict], Any], *,
                   turn_id: str | None = None, invocation_id: str | None = None,
                   registry: bool = False) -> Any:
    # Detach from caller/middleware-owned mutable objects before authorization.
    arguments = json.loads(json.dumps(arguments, ensure_ascii=False, allow_nan=False))
    receipt = _CURRENT.get()
    # A nested AIAgent can start a fresh turn while the parent tool's context
    # is still active. Explicit root turns select their own registered scope;
    # only the matching registry leg may consume that turn's existing receipt.
    if turn_id and receipt and (
            not registry or receipt.turn_id != turn_id
            or receipt.binding is not get_binding(turn_id)):
        receipt = None
    digest = _digest(arguments)
    if (registry and receipt and receipt.name == tool_name and receipt.digest == digest
            and not receipt.registry_entered):
        _record(receipt.binding)
        receipt.registry_entered = True
        return execute(arguments)
    binding = receipt.binding if receipt else get_binding(turn_id)
    if binding is None:
        # No compiled scope for this turn: run the tool directly — there is no
        # scope to record claims against. Scheduled deliveries, background
        # reviews, relay lanes, wake-ups and foreground CLI turns all do real
        # work without a live owner turn. Turns that DID compile a scope keep
        # exact admission (claims, replay, uncertainty) on the bound path below.
        return execute(arguments)
    if binding.amendment_pending:
        if not binding.amendment_ready.wait(60):
            return _blocked("Owner amendment admission has not completed; no action was started")
        if not receipt:
            binding = get_binding(turn_id)
            if binding is None:
                return _blocked("Owner amendment did not establish current authority")
    local_id = invocation_id or uuid.uuid4().hex
    call_id = (receipt.invocation_id + ":nested:" + local_id if receipt
               else "turn:" + str(turn_id) + ":call:" + local_id)
    try:
        record = _record(binding)
        prior = binding.db.get_scope_action(binding.scope_id, call_id, include_amendments=True)
        if prior:
            if prior["tool_name"] != tool_name or prior["arguments_digest"] != digest:
                return _blocked("Invocation identity cannot be rebound to different arguments")
            if prior["state"] == "completed":
                return prior["result"]
            return _blocked("This invocation has an admitted outcome requiring reconciliation; do not replay it", uncertain=True)
        attempt_id = turn_id or (receipt.turn_id if receipt else None)
        if not attempt_id:
            return _blocked("Native execution attempt identity is missing")
        if binding.db.get_scope_uncertainty(binding.scope_id, attempt_id):
            return _blocked("A prior admitted action has no resolved outcome; reconcile it before new actions", uncertain=True)
        from agent.execution_scope_admission import claim_action
        # No inference/effect under this lock: only the final revocation check
        # and native durable claim linearize with timeout/owner amendments.
        with binding.lock:
            _record(binding)
            claim = claim_action(binding, call_id, tool_name, arguments, attempt_id=attempt_id)
        if claim["status"] == "completed":
            return claim["result"]
        if claim["status"] == "uncertain":
            return _blocked("This invocation was already admitted without a recorded outcome; do not replay it", uncertain=True)
    except PolicyUnavailable as exc:
        with binding.lock:
            binding.failures += 1
        return _blocked(str(exc))
    except (ValueError, KeyError) as exc:
        return _blocked(str(exc))
    token = _CURRENT.set(_Receipt(binding, tool_name, digest, call_id,
                                  turn_id=turn_id or (receipt.turn_id if receipt else None)))
    try:
        result = execute(arguments)
        # Native tools can return a confirmed UNKNOWN outcome rather than raise.
        # Only explicit structured disposition is used, never error prose.
        try:
            outcome = json.loads(result) if isinstance(result, str) else result
        except (TypeError, ValueError):
            outcome = None
        if isinstance(outcome, dict) and (outcome.get("status") == "uncertain"
                or outcome.get("effect_disposition") in {"unknown", "uncertain"}):
            binding.db.mark_scope_action_uncertain(binding.scope_id, call_id)
            return ExecutionScopeDenied(result if isinstance(result, str) else json.dumps(result), uncertain=True)
        binding.db.complete_scope_action(binding.scope_id, call_id, result)
        return result
    except BaseException:
        binding.db.mark_scope_action_uncertain(binding.scope_id, call_id)
        raise
    finally:
        _CURRENT.reset(token)
