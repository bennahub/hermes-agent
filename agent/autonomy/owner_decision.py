"""Native owner-decision selection and exact prepared-target identity."""
from copy import deepcopy
from pathlib import Path
from typing import Any
import hashlib
from agent.autonomy import store

_TARGET_REFS = (
    "owner_request", "owner_delivery", "owner_obligation", "pending_owner_result",
    "resume_generation", "owner_stop", "execution_scope", "execution_amendment",
)


def owner_proposal_candidate(db: Any, session_id: str, message_id: int | str) -> dict | None:
    """Return the canonical preceding owner-visible agent reply as a derive candidate.

    Host code supplies this bounded candidate; the isolated derive judgment decides
    whether the current owner instruction explicitly adopts or refers to it. No
    text heuristic here can grant authority, and non-canonical/synthetic rows are
    excluded at the source.
    """
    from agent.message_projection import projection

    current_id = int(message_id)
    rows = db.get_messages(session_id)
    current = next((row for row in rows if int(row.get("id", -1)) == current_id), None)
    if not current or current.get("role") != "user" or current.get("display_kind"):
        return None
    prior = next(
        (row for row in reversed(rows)
         if int(row.get("id", -1)) < current_id and row.get("active", True)),
        None,
    )
    if not prior or prior.get("role") != "assistant" or prior.get("display_kind"):
        return None
    native = projection(prior.get("display_metadata") or {})
    if (not native or native.get("origin") != "agent"
            or native.get("audience") != "owner"
            or native.get("purpose") not in {"message", "result"}):
        return None
    content = str(prior.get("content") or "")
    if not content.strip() or len(content.encode("utf-8")) > 12000:
        return None
    return {
        "session_id": session_id,
        "message_id": int(prior["id"]),
        "content": content,
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
    }

def decision_target(work: dict | None) -> dict | None:
    if work is None:
        return None
    return {"work_id": work["id"],
            "refs": {key: deepcopy(work["refs"].get(key)) for key in _TARGET_REFS}}


def _needs_owner_rows(db: Any) -> list[dict]:
    """Read native candidates without initializing or migrating business state."""
    import sqlite3

    path = Path(db.db_path).parent / "autonomy" / "work.db"
    if not path.is_file():
        return []
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM work WHERE state = ? ORDER BY updated_at DESC", ("needs_owner",),
        ).fetchall()
    return [store._row_to_work(row) for row in rows]


def select_owner_decision(db: Any, session_id: str, message_id: int | str, *, strict: bool = False) -> dict | None:
    """Only trusted ingress calls this; ambiguous replies leave all gates closed."""
    import re
    from agent.autonomy.owner_continuity import _source_row, adjudicated

    row = _source_row(db, session_id, message_id)
    text = str(row.get("content") or "").strip()
    from agent.conversation_referents import is_contextual_followup

    affirmative = is_contextual_followup(text) or bool(
        re.fullmatch(
            r"(?:approved|done|ready|تابع|موافق|ابدأ|ابدا|تم|جاهز)[.!، ]*",
            text,
            re.IGNORECASE,
        )
    )
    candidates = []
    for work in _needs_owner_rows(db):
        if work["refs"].get("pending_owner_result"):
            continue
        source = work["refs"].get("owner_request") or {}
        original = source.get("session_id")
        if (
            not original
            or (db.resolve_resume_session_id(original) or original) != session_id
        ):
            continue
        delivery = work["refs"].get("owner_delivery") or {}
        if not delivery.get("message_id") or int(message_id) <= int(
            delivery["message_id"]
        ):
            continue
        if re.fullmatch(
            r"(?:continue|proceed|approved|كمل|تابع|موافق|ابدأ|ابدا)\s+"
            + re.escape(work["id"])
            + r"[.!، ]*",
            text,
            re.IGNORECASE,
        ):
            candidates = [work]
            break
        if affirmative:
            candidates.append(work)
    if len(candidates) > 1:
        # A row marked `continuation_unstarted` is deliberately invisible to the
        # badge, so the Owner cannot know it is there — and with two candidates
        # an affirmative reply used to bind neither, leaving them typing "كمل"
        # at a jam whose cause they cannot see. A visible request outranks an
        # invisible one; only a genuine ambiguity between two visible requests
        # still needs the explicit `كمل <work id>` form.
        visible = [w for w in candidates if not adjudicated(w["refs"])]
        if len(visible) == 1:
            candidates = visible
    if len(candidates) != 1:
        if strict and len(candidates) > 1:
            raise ValueError("Owner decision is ambiguous between current obligations")
        return None
    return candidates[0]


def decision_scope_subject(db: Any, target: dict, current_instruction: Any) -> dict:
    """Use only the selected structured work's original owner row and frozen policy."""
    from agent.autonomy.owner_continuity import _source_row

    owner = target["refs"]["owner_request"]
    row = _source_row(db, owner["session_id"], owner["message_id"])
    work_source = {
        "work_id": target["work_id"], "owner_request": owner,
        "original_owner_instruction": row["content"],
        "owner_obligation": target["refs"].get("owner_obligation"),
    }
    if target["refs"].get("execution_amendment"):
        work_source["accepted_owner_correction"] = target["refs"]["execution_amendment"]["instruction"]
    saved = target["refs"].get("execution_scope")
    if saved:
        if (not isinstance(saved, dict) or not isinstance(saved.get("db_path"), str)
                or not isinstance(saved.get("scope_id"), str)):
            raise ValueError("Owner decision prior scope locator is invalid")
        if Path(saved["db_path"]).resolve() != Path(db.db_path).resolve():
            raise ValueError("Owner decision scope database does not match")
        record = db.get_scope(saved["scope_id"])
        if record is None:
            raise ValueError("Owner decision prior scope is unavailable")
        # A needs-owner turn normally closes its execution capability. Its
        # immutable policy still bounds the newly authorized native continuation.
        work_source["prior_frozen_scope"] = record["scope"]
    return {"current_original_instruction": current_instruction,
            "active_structured_work": work_source}


def validate_bound_owner_decision(agent: Any, record: dict, persisted_row: dict) -> dict | None:
    """The consumed owner decision must be exactly the target compiled beforehand."""
    target = record["source"].get("owner_decision_target")
    if target is None:
        return
    session_id = agent.session_id
    message_id = persisted_row.get("_row_id")
    if record["source"].get("session_id") != session_id or record["source"].get("message_id") != message_id:
        raise ValueError("Prepared owner decision row does not match")
    work_id = getattr(agent, "_owner_continuity_work_id", None)
    if work_id != target["work_id"]:
        raise ValueError("Prepared owner decision did not bind its selected work")
    work = store.get_work(work_id, hermes_home=Path(agent._session_db.db_path).parent)
    refs = (work or {}).get("refs", {})
    decision = refs.get("owner_decision") or {}
    if (not work or work["state"] != "working"
            or refs.get("owner_request") != target["refs"]["owner_request"]
            or refs.get("resume_generation") != int(target["refs"].get("resume_generation") or 0) + 1
            or refs.get("execution_scope") != target["refs"].get("execution_scope")
            or decision.get("session_id") != session_id
            or decision.get("message_id") != str(message_id)
            or refs.get("owner_stop")):
        raise ValueError("Prepared owner decision target changed before execution binding")
    return {key: deepcopy(refs.get(key)) for key in (*_TARGET_REFS, "owner_decision")}
