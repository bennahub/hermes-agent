"""Compile missing scope before native continuation execution admission."""
from agent.execution_scope_policy import PolicyUnavailable
from agent.execution_scope_observations import compilation_context


class ScopePreflightBlocked(PolicyUnavailable):
    def __init__(self, message, messages):
        super().__init__(message)
        self.messages = list(messages)


def prepare_scope(agent, original_user_message, messages, current_user_idx, display_kind=None):
    # Cached agents must not settle an earlier turn's work on this early exit.
    agent._owner_continuity_work_id = None
    agent._owner_continuity_source = None
    agent._owner_continuity_persistence_required = False
    agent._owner_continuity_deferred = False
    agent._prepared_owner_decision = None
    row = messages[current_user_idx]
    try:
        _prepare_scope(agent, row, display_kind)
    except ScopePreflightBlocked:
        raise
    except ValueError as exc:
        if "ambiguous" in str(exc).lower():
            raise ScopePreflightBlocked(
                "Which one should I continue?", messages,
            ) from exc
        return
    except PolicyUnavailable:
        # Missing or uncompilable scope is not an owner-facing stop. The
        # current owner turn still proceeds; stale unscoped turns stay blocked
        # at exact invocation admission.
        return


def _prepare_scope(agent, row, display_kind):
    from tools.owner_task_authority import peek_execution_source, turn_authority
    from agent.message_projection import projection
    from agent.autonomy.owner_continuity import continuation_display_metadata, _source_row
    from agent.autonomy import store
    from agent.execution_scope import derive_scope
    db = getattr(agent, "_session_db", None)
    grant = turn_authority(getattr(agent, "_current_turn_id", None))
    source = (grant or {}).get("execution_source") or peek_execution_source(agent)
    projected = projection(row.get("display_metadata") or {})
    if display_kind or row.get("display_kind") or (projected and projected.get("origin") != "owner"):
        source = None
    if source:
        if db is None or type(row.get("_row_id")) is not int:
            raise PolicyUnavailable("Original instruction identity was not persisted")
        from agent.autonomy.owner_decision import (
            owner_proposal_candidate, decision_scope_subject, decision_target, select_owner_decision,
        )
        selected = select_owner_decision(db, agent.session_id, row["_row_id"], strict=True)
        target = decision_target(selected)
        prepared = {"session_id": agent.session_id, "message_id": str(row["_row_id"]), "target": target}
        key = f"owner:{agent.session_id}:{row['_row_id']}"
        original = {"kind": "owner", "ingress_id": source["id"], "session_id": agent.session_id,
                    "message_id": row["_row_id"], "instruction": source["instruction"]}
        subject = source["instruction"]
        proposal = owner_proposal_candidate(db, agent.session_id, row["_row_id"])
        if proposal:
            subject = {
                "current_owner_instruction": source["instruction"],
                "owner_visible_proposal_candidate": proposal,
            }
        if target is not None:
            original["owner_decision_target"] = target
            subject = decision_scope_subject(db, target, subject)
        record = db.get_scope_for_source(key)
        if record is None:
            record = db.create_or_get_scope(key, original, derive_scope(subject, runtime=agent._current_main_runtime(), **compilation_context(db, agent.session_id)))
        elif record["source"] != original:
            raise PolicyUnavailable("Prepared owner source no longer matches its native decision target")
        if decision_target(select_owner_decision(db, agent.session_id, row["_row_id"], strict=True)) != target:
            raise PolicyUnavailable("Owner decision changed during scope preparation")
        agent._prepared_owner_decision = prepared
        return
    continuation = continuation_display_metadata(agent)
    if continuation is None:
        return  # Inherited assignments already have frozen policy; never recompile context.
    work = store.get_work(continuation["work_id"])
    from agent.execution_scope_amendment import pending_amendment, settle_pending_amendment
    if pending_amendment(work):
        settle_pending_amendment(work, db.db_path.parent)
        raise PolicyUnavailable("Accepted owner correction requires a new valid execution scope")
    if work["refs"].get("execution_scope") is not None:
        from agent.execution_scope import Binding, _record
        saved = work["refs"]["execution_scope"]
        if not isinstance(saved, dict) or db is None or str(db.db_path) != saved.get("db_path"):
            raise PolicyUnavailable("Continuation scope database does not match source")
        _record(Binding(db, saved.get("scope_id", "")))
        return
    key = "work:" + work["id"]
    if db.get_scope_for_source(key) is None:
        owner = work["refs"]["owner_request"]
        original = _source_row(db, owner["session_id"], owner["message_id"])
        db.create_or_get_scope(key, {"kind": "continuation", "work_id": work["id"],
            "owner_request": owner, "instruction": original["content"]},
            derive_scope(original["content"], runtime=agent._current_main_runtime(), **compilation_context(db, agent.session_id)))


def blocked_result(agent, exc):
    from agent.agent_runtime_helpers import note_turn_persisted
    note_turn_persisted(agent)
    return {"final_response": str(exc) if "Which one" in str(exc) else "Which one should I continue?",
            "messages": exc.messages, "api_calls": 0, "completed": False, "failed": True,
            "error": str(exc), "error_type": "execution_scope_preflight_blocked",
            "effect_disposition": "not_started", "turn_exit_reason": "execution_scope_preflight_blocked"}
