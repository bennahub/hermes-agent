"""Global informational receipts with an explicit profile opt-out; never a source of authority."""
from pathlib import Path

import yaml


def _enabled(db) -> bool:
    from hermes_cli.config import cfg_get, read_user_config_raw
    # load_config uses ambient profile state; an unreadable opt-in stays disabled.
    try:
        config = read_user_config_raw(Path(db.db_path).parent / "config.yaml")
    except (OSError, ValueError, yaml.YAMLError):
        return False
    return cfg_get(config, "security", "execution_scope_observations", default=True) is not False


def active_work_referents(db, session_id: str | None) -> list[dict]:
    import sqlite3
    try:
        return _active_work_referents(db, session_id)
    except (sqlite3.Error, OSError, ValueError):
        return []  # Optional context must not strand an otherwise valid request.


def _active_work_referents(db, session_id: str | None) -> list[dict]:
    """Bounded structured obligations for read-only target clarification only."""
    if not session_id:
        return []
    from agent.autonomy import store
    from agent.autonomy.owner_continuity import _source_row
    import sqlite3
    path = Path(db.db_path).parent / "autonomy" / "work.db"
    if not path.is_file():
        return []
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM work WHERE state = ?", ("needs_owner",),
        ).fetchall()
    referents = []
    for row in rows:
        work = store._row_to_work(row)
        refs = work.get("refs", {})
        owner = refs.get("owner_request") or {}
        if not owner or (db.resolve_resume_session_id(owner.get("session_id"))
                          or owner.get("session_id")) != session_id:
            continue
        if refs.get("owner_stop") or refs.get("pending_owner_result") or refs.get("execution_amendment"):
            continue
        obligation = refs.get("owner_obligation") or {}
        if obligation.get("status") != "unresolved":
            continue
        try:
            original = _source_row(db, owner["session_id"], owner["message_id"])
        except (KeyError, TypeError, ValueError):
            return []
        instruction = original.get("content")
        if not isinstance(instruction, str) or len(instruction.encode("utf-8")) > 4000:
            return []  # Never hide a candidate's restrictions or imply uniqueness.
        referents.append({
            "work_id": work["id"],
            "owner_request": {"session_id": owner.get("session_id"), "message_id": owner.get("message_id")},
            "owner_obligation": {key: obligation.get(key) for key in ("id", "delivery_id", "kind", "status")},
            "original_owner_instruction": instruction,
        })
        if len(referents) > 16:
            return []
    return referents


def compilation_context(db, session_id: str | None = None) -> dict:
    if not _enabled(db):
        return {}
    context = {"resolve_targets": True}
    referents = active_work_referents(db, session_id)
    if referents:
        context["active_work_referents"] = referents
    return context


def policy_context(binding) -> dict:
    if not _enabled(binding.db):
        return {}
    context = {"observations": binding.db.get_scope_observations(binding.scope_id)}
    work = _current_work(binding)
    if work:
        context["native_work"] = {**work[0], "verification_recorded": bool(work[1]["refs"].get("verification"))}
    source = (binding.db.get_scope(binding.scope_id) or {}).get("source", {})
    owner = (work[1]["refs"].get("owner_request") if work else None) or source.get("owner_request") or {}
    session_id = owner.get("session_id") or source.get("session_id")
    referents = active_work_referents(binding.db, session_id)
    # Always provide the refreshed list, including empty, so stale frozen
    # informational referents cannot be reintroduced by judge defaults.
    context["active_work_referents"] = referents
    return context


def approval_context() -> dict:
    """Same opt-in receipts for the existing consequence guardian; no bypass."""
    from agent.execution_scope import capture_binding, _record
    binding = capture_binding()
    if binding is None:
        return {}
    context = policy_context(binding)
    if not context:
        return {}
    record = _record(binding)
    authority = dict(record["scope"])
    # Informational referents are refreshed above; do not replay the frozen snapshot
    # through the independent consequence guardian.
    authority.pop("active_work_referents", None)
    return {"frozen_authority": authority, **context}


def _current_work(binding):
    """Resolve only host-recorded work identity, never an ID from command text."""
    from agent.autonomy import store
    if binding.work_id:
        ref = {"work_id": binding.work_id, "scope_id": binding.scope_id, "generation": binding.generation}
    else:
        source = (binding.db.get_scope(binding.scope_id) or {}).get("source", {})
        ref = source.get("native_work_ref")
    if not isinstance(ref, dict) or set(ref) != {"work_id", "scope_id", "generation"}:
        return None
    work = store.get_work(ref["work_id"], hermes_home=Path(binding.db.db_path).parent)
    if not work:
        return None
    refs = work["refs"]
    if (work["state"] not in {"working", "waiting", "investigating", "actionable"}
            or refs.get("owner_stop") or refs.get("execution_amendment")
            or refs.get("resume_generation") != ref["generation"]
            or refs.get("execution_scope") != {"scope_id": ref["scope_id"], "db_path": str(binding.db.db_path)}):
        return None
    return dict(ref), work


def child_work_reference(binding):
    if _enabled(binding.db) and (current := _current_work(binding)):
        return current[0]
    return None
