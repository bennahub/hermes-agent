"""Source-bound Owner responsibility in the existing autonomy ledger.

This module owns continuation contracts, not a second task/event database.
Background observation and existing human approval gates remain independent.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
import hashlib
import json
import re
import logging
from pathlib import Path
import time

from agent.autonomy import store
from agent.autonomy.lifecycle import identity, adjudicated
from agent.autonomy.paths import resolve_home

logger = logging.getLogger(__name__)

#: Dispatches of an UNADMITTED continuation to re-arm before telling the Owner.
#: An unadmitted dispatch proves no *task* turn ran and no tool executed, so a
#: retry cannot duplicate a logical result or replay an external effect -- it is
#: the transport being retried, not the task. (Turn-start bookkeeping before
#: ``bind_turn`` -- compaction, memory prefetch, the user row -- can have run;
#: none of it is a task action, and all of it is idempotent per turn.)
UNADMITTED_RESUME_ATTEMPTS = 3
# ``resume_attempts`` resets when a dispatch is admitted, because the budget it
# bounds is the transport's. Automatic recovery from a turn that admitted and
# then ended unfinished cannot borrow that counter: every cycle resets it, and
# the loop (settle -> wait 2 minutes -> resume -> admit -> unfinished) would run
# forever without ever asking the Owner. It gets its own, which only an Owner
# decision clears.
UNFINISHED_SETTLEMENTS = 3

#: Marks a needs_owner escalation raised because a continuation never STARTED.
#: The row stays ``needs_owner`` so ``_bind_owner_decision`` can still rebind it
#: from the Owner's next ordinary reply -- the responsibility is not dropped --
#: but ``tui_gateway.activity.work_state`` skips it, because nothing is actually
#: being asked of the Owner. A terminal state would clear the badge too, and
#: that was wrong: ``store.update_work`` freezes a terminal Owner row and
#: ``_bind_owner_decision`` only scans ``needs_owner``, so "كمل" would bind
#: nothing and the obligation would be lost.
UNSTARTED_CONTINUATION = "continuation_unstarted"


def dispatch_never_started(work):
    """True only when a continuation dispatch EXISTS and never reached ``bind_turn``.

    ``bind_turn`` stamps ``admitted`` after the native canonical turn lease
    admits the resumed session and before the first model request, and every
    other exit from it raises. So on a row that has a dispatch, the absence of
    ``admitted`` is proof that no task turn ran for it: nothing was attempted,
    nothing was replayed, no new effect exists for the Owner to inspect.

    Both halves are load-bearing. A row with NO dispatch is not a continuation
    at all -- it is the Owner's own first turn (``bind_turn``'s ordinary path
    writes no dispatch, and ``_bind_owner_decision`` clears it on every rebind).
    That turn really ran and really can have sent mail or deployed, and
    ``_recover_orphan`` reaches it through ``owner_process``. Reading a missing
    dispatch as "not admitted" would tell the Owner their work never happened
    and hide the badge on the one row that genuinely needs them.
    """
    dispatch = (work or {}).get("refs", {}).get("dispatch")
    return bool(isinstance(dispatch, dict) and dispatch.get("nonce")
                and dispatch.get("generation") == (work or {}).get("refs", {}).get("resume_generation")
                and not dispatch.get("admitted"))


def ever_admitted(work):
    """True when ANY dispatch on this row has reached ``bind_turn``.

    ``dispatch_never_started`` speaks only for the dispatch in front of it, and
    ``wait()`` clears ``dispatch`` between cycles. This is the row's own memory,
    and it is what entitles anything to say the task as a whole did nothing.
    """
    refs = (work or {}).get("refs", {})
    return bool(refs.get("ever_admitted") or any(
        attempt.get("admitted") for attempt in
        (refs.get("lifecycle") or {}).get("attempts", {}).values()))


def _interrupted_result(admitted, attempts, ran_before=False):
    """The Owner-facing outcome for a continuation that could not finish.

    Three genuinely different facts, so three different sentences. Claiming
    effects need inspection when ``bind_turn`` never ran is the report of an
    inspection the Owner cannot act on; claiming nothing changed when an earlier
    cycle of the same task deployed is worse, because it is the reassuring
    direction.
    """
    if admitted:
        return (
            "The continuation was interrupted before a verified outcome. Existing effects "
            "need inspection before retry; no action was replayed."
        )
    if ran_before:
        return (
            "I could not restart the continuation for this task after "
            f"{attempts} attempts, so this attempt ran no step and replayed nothing. "
            "Earlier steps of this task did run, so its existing effects still need "
            "checking before it is resumed."
        )
    # Deliberately scoped to the ATTEMPT. Saying the TASK is unchanged is a claim
    # the ledger cannot support: the Owner's own turn can have acted before it
    # parked, and nothing records that.
    return (
        "I could not start the scheduled continuation for this task after "
        f"{attempts} attempts; that attempt ran no step and replayed nothing. "
        "The task is still unfinished. Reply to continue it."
    )


def _unstarted_refs(work, attempts):
    """The historical marker. NOTHING in the runtime writes this any more.

    It exists for `tools/repair_unadmitted_owner_escalations.py`, which settles
    rows a PRE-FIX build escalated on their first transport failure. The current
    build re-arms silently and only escalates once the retry budget is spent,
    which is a real Owner obligation and keeps its badge. A low attempt count
    narrows that population but does NOT prove provenance -- orphan recovery
    escalates at attempt one too -- so the tool decides nothing on its own and
    waits to be told which row a human has read.
    """
    """Refs marking an escalation the transport caused, not the Owner."""
    return {
        UNSTARTED_CONTINUATION: {
            "resume_generation": (work or {}).get("refs", {}).get("resume_generation"),
            "attempts": attempts,
            "at": time.time(),
        }
    }


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("resume requires an ISO timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("resume timestamp requires timezone")
    return parsed.timestamp()


def validate_transition(current, desired):
    refs = desired.get("refs") or {}
    source = refs.get("owner_request")
    prior = (current or {}).get("refs", {}).get("owner_request")
    if prior and source != prior:
        raise ValueError("owner source is immutable")
    if not source:
        return
    if not isinstance(source, dict) or not all(
        isinstance(source.get(k), str) and source[k].strip()
        for k in ("session_id", "message_id")
    ):
        raise ValueError("owner work requires canonical source identity")
    state = desired["state"]
    stopped = (current or {}).get("refs", {}).get("owner_stop")
    if stopped:
        pending = refs.get("pending_owner_result")
        if refs.get("owner_stop") != stopped:
            raise ValueError("Owner Stop is immutable for this source request")
        if state not in {(current or {}).get("state"), "cancelled"}:
            raise ValueError("Stopped Owner task cannot resume or complete")
        if pending is not None and pending.get("state") != "cancelled":
            raise ValueError("Stopped Owner task requires cancellation outcome")
        if pending is None and state != "cancelled":
            raise ValueError("Owner Stop must remain pending until durable delivery")
    if (
        prior
        and (current or {}).get("state") == "needs_owner"
        and state not in {"needs_owner", "failed", "cancelled"}
    ):
        decision = refs.get("owner_decision")
        if (
            not isinstance(decision, dict)
            or not decision.get("message_id")
            or decision == current["refs"].get("owner_decision")
        ):
            raise ValueError("needs_owner requires a new source-bound Owner decision")
    if state == "waiting":
        resume = refs.get("resume")
        if not isinstance(resume, dict) or resume.get("kind") not in {
            "until",
            "event",
            "child",
            "external",
        }:
            raise ValueError("owner wait requires a durable resume condition")
        _timestamp(resume.get("deadline"))
        if resume["kind"] == "until":
            _timestamp(resume.get("at"))
        elif not isinstance(resume.get("id"), str) or not resume["id"].strip():
            raise ValueError("owner wait requires exact event/child/external identity")
    if state in store.TERMINAL_STATES:
        result = desired.get("completion_result")
        if not isinstance(result, str) or not result.strip() or "[SILENT]" in result:
            raise ValueError("owner work requires a visible final outcome")
        delivery = refs.get("owner_delivery")
        if (
            not isinstance(delivery, dict)
            or not delivery.get("message_id")
            or delivery.get("source_session_id", delivery.get("session_id"))
            != source["session_id"]
        ):
            raise ValueError(
                "owner result must be durably delivered before terminal state"
            )
        if state == "completed" and not refs.get("verification"):
            raise ValueError("owner result requires verified outcome")


def requires_continuation(text):
    """Conservative direct-request classifier; async dispatch also registers work.

    Questions/acknowledgements and ordinary one-step asks create no ledger row.
    This supplements the explicit work-start contract, never parses assistant
    promises as the only mechanism for responsibility.
    """
    import re

    text = _request_text(text)
    value = text.strip().lower()
    if re.match(r"^(how|what|why|can you explain|ما هو|ماهي|كيف|ليش|لماذا)\b", value):
        return False
    action = re.search(
        r"\b(check|build|deploy|fix|implement|transfer|download|verify|install|finish|complete)\b|(?:تحقق|نفذ|ابن[يِ]?|انشر|ثبت|افحص|شيك|أصلح|اصلح|جهز|كمل|راجع)",
        value,
    )
    continuation = re.search(
        r"\b(after|later|when|then|return|report|finish|complete|delegate|schedule)\b|(?:بعد|ثم|عندما|إذا وصل|اذا وصل|لما|ارجع|بالنتيجة|بالنتيجه|خلص|أتمم|اتمم|فوض|دقيقتين|دقائق|بالكامل)",
        value,
    )
    substantial = re.search(
        r"\b(build|deploy|fix|implement|install|complete)\b|(?:نفذ|انشر|ثبت|أصلح|اصلح|جهز|أتمم|اتمم)",
        value,
    )
    return bool(substantial or (action and continuation))


def _source_row(db, session_id, message_id):
    # Use the native read path, including archived compaction history. Identity
    # comes from persistence, never a text hash that merges repeated requests.
    row = next(
        (
            r
            for r in db.get_messages(session_id, include_inactive=True)
            if str(r.get("id")) == str(message_id)
        ),
        None,
    )
    if (
        not row
        or row.get("role") != "user"
        or row.get("observed")
        or row.get("display_kind")
    ):
        raise ValueError("owner source is not an ordinary canonical user message")
    return row


def register_owner_request(
    db, session_id, message_id, *, hermes_home=None, force=False
):
    row = _source_row(db, session_id, message_id)
    text = _request_text(row.get("content"))
    if not force and not requires_continuation(text):
        return None
    import os
    from gateway.status import get_process_start_time

    key = f"owner:{session_id}:{message_id}"
    return store.start_work(
        why=text,
        outcome=text,
        done_contract="Verify the requested outcome and deliver a final result to the originating Owner conversation.",
        idempotency_key=key,
        refs={
            "owner_request": {"session_id": session_id, "message_id": str(message_id)},
            "heartbeat_at": time.time(),
            "owner_process": {
                "pid": os.getpid(),
                "started_at": get_process_start_time(os.getpid()),
            },
        },
        hermes_home=hermes_home,
    )["work"]


def continuation_display_metadata(agent):
    """Display-only identity from the existing native continuation nonce fence."""
    import os
    work_id = os.environ.get("HERMES_OWNER_CONTINUATION_ID")
    nonce = os.environ.get("HERMES_OWNER_CONTINUATION_NONCE")
    if not work_id or not nonce:
        return None
    work = store.get_work(work_id)
    dispatch = (work or {}).get("refs", {}).get("dispatch") or {}
    db = getattr(agent, "_session_db", None)
    source = (work or {}).get("refs", {}).get("owner_request") or {}
    if (not work or work["state"] != "working" or dispatch.get("nonce") != nonce
            or dispatch.get("generation") != work["refs"].get("resume_generation")
            or work["refs"].get("pending_owner_result") or db is None
            or getattr(agent, "session_id", None) != (db.resolve_resume_session_id(source.get("session_id")) or source.get("session_id"))):
        return None
    return {"work_id": work_id, "source_session_id": source["session_id"], "source_message_id": source["message_id"]}


def bind_turn(agent, message, *, display_kind=None):
    """Called after the native user row commits, before the first model request."""
    agent._owner_continuity_work_id = None
    agent._owner_continuity_source = None
    agent._owner_continuity_persistence_required = False
    agent._owner_continuity_deferred = False
    import os

    resumed_id = os.environ.get("HERMES_OWNER_CONTINUATION_ID")
    nonce = os.environ.get("HERMES_OWNER_CONTINUATION_NONCE")
    if resumed_id and nonce:
        work = store.get_work(resumed_id)
        dispatch = (work or {}).get("refs", {}).get("dispatch") or {}
        db = getattr(agent, "_session_db", None)
        if (
            work
            and work["state"] == "working"
            and dispatch.get("nonce") == nonce
            and dispatch.get("generation") == work["refs"].get("resume_generation")
            and not work["refs"].get("pending_owner_result")
            and db is not None
        ):
            source = work["refs"]["owner_request"]["session_id"]
            if getattr(agent, "session_id", None) == (
                db.resolve_resume_session_id(source) or source
            ):
                # This runs only after native canonical turn-lease admission.
                # A cron process timing out before here did not execute the turn.
                admitted = store.update_work(
                    work["id"],
                    # ``ever_admitted`` is STICKY. ``wait()`` clears ``dispatch``,
                    # so without it a task whose first cycle really deployed and
                    # then re-armed looks, on its next failed dispatch, exactly
                    # like one that never ran at all -- and would be told
                    # "nothing about it has changed".
                    # ``resume_attempts`` resets here because the budget it
                    # bounds is the TRANSPORT's: this dispatch reached the model,
                    # so the transport works, and a long task's successful cycles
                    # must not spend the retries meant for a broken one.
                    refs={"dispatch": {**dispatch, "admitted": True},
                          "ever_admitted": True, "resume_attempts": 0},
                    expected_state="working",
                    expected_refs={
                        "dispatch": dispatch,
                        "pending_owner_result": None,
                        "resume_generation": dispatch["generation"],
                    },
                )
                if admitted is not None:
                    _bind_work(agent, admitted)
                    return _work_hint(admitted)
        raise ValueError("continuation identity does not match source session")
    if display_kind or not isinstance(message, dict):
        return ""
    from gateway.background_delivery import background_delivery_active

    if background_delivery_active() or str(getattr(agent, "platform", "")).lower() in {
        "cron",
        "subagent",
        "tool",
    }:
        return ""
    from tools.owner_task_authority import turn_authority, turn_execution_source

    if not (turn_authority(
        getattr(agent, "_current_turn_id", None), profile_home=str(resolve_home())
    ) or turn_execution_source(getattr(agent, "_current_turn_id", None))):
        return ""
    agent._owner_continuity_persistence_required = True
    db = getattr(agent, "_session_db", None)
    sid = getattr(agent, "session_id", None)
    mid = message.get("_row_id")
    if db is None or not sid or type(mid) is not int:
        if requires_continuation(message.get("content")):
            raise RuntimeError(
                "Owner task source could not be persisted before execution"
            )
        return ""
    agent._owner_continuity_source = (sid, mid)
    work = _bind_owner_decision(
        db, sid, mid, prepared=getattr(agent, "_prepared_owner_decision", None)
    ) or register_owner_request(db, sid, mid)
    if not work:
        return ""
    _bind_work(agent, work)
    return _work_hint(work)


def _work_hint(work):
    if work["refs"].get("owner_stop"):
        raise ValueError("Owner stopped this source request before the model turn")
    return (
        "\n\nOwner task continuity: this request is durably assigned as "
        + work["id"]
        + ". "
        "Keep responsibility until verified outcome and final Owner delivery. Before promising to return later, "
        "run `hermes autonomy work-wait "
        + work["id"]
        + " --until <ISO timezone timestamp>` "
        "(or --event/--child/--external <exact id> --deadline <ISO timestamp>). "
        "The timer/event resumes this same work. Never use a prose-only waiting state. "
        "For a verified file outcome use `hermes autonomy work-verify "
        + work["id"]
        + " --file <path>`. "
        "After verification run `hermes autonomy work-complete "
        + work["id"]
        + " --result <actual result>`, "
        "then return the visible final result. For other evidence use --tool-message-id on work-verify. "
        "For a background process or message_agent reply, verify its native completion with `hermes autonomy work-verify "
        + work["id"]
        + " --process-id <exact process id>`; a dispatch ACK is not the completed outcome. "
        "To report failure use `hermes autonomy work-update "
        + work["id"]
        + " --state failed --waiting-reason <actual failure>`. For an actual missing Owner decision use "
        "`hermes autonomy work-update "
        + work["id"]
        + " --state needs_owner --waiting-reason <question>`. "
        "Inspect with `hermes autonomy work-get "
        + work["id"]
        + "`; select a successful current-work verification_candidates entry and pass its exact numeric message_id to `hermes autonomy work-verify "
        + work["id"]
        + " --tool-message-id <message_id>`. Continuation never grants new tool authority. "
        "[SILENT] cannot complete this Owner task."
    )



def wait(
    work_id,
    *,
    until=None,
    event=None,
    child=None,
    external=None,
    deadline=None,
    hermes_home=None,
    expected_state=None,
    expected_refs=None,
    extra_refs=None,
):
    choices = [
        (k, v)
        for k, v in [
            ("until", until),
            ("event", event),
            ("child", child),
            ("external", external),
        ]
        if v is not None
    ]
    if len(choices) != 1:
        raise ValueError("choose exactly one resume condition")
    kind, value = choices[0]
    deadline = deadline or (until if kind == "until" else None)
    _timestamp(deadline)
    resume = {
        "kind": kind,
        "deadline": deadline,
        "at" if kind == "until" else "id": value,
    }
    initial = None
    for _ in range(3):
        work = store.get_work(work_id, hermes_home)
        if not work or not work["refs"].get("owner_request"):
            raise ValueError("owner work required")
        if (expected_state is not None and work["state"] != expected_state) or (
            expected_refs
            and any(work["refs"].get(k) != v for k, v in expected_refs.items())
        ):
            return None
        if work["state"] == "needs_owner":
            raise ValueError("Owner decision required before resuming")
        stable_refs = {k: v for k, v in work["refs"].items() if k not in {"native_process_completion", "resume_event"}}
        snapshot = (work["state"], stable_refs)
        if initial is not None and snapshot != initial:
            return None
        initial = snapshot
        if work["refs"].get("owner_stop"):
            raise ValueError("Owner stopped this source request")
        if work["refs"].get("pending_owner_result"):
            return None
        prior_generation = work["refs"].get("resume_generation")
        native_process = work["refs"].get("native_process")
        if kind == "external" and native_process and value == native_process["id"]:
            value = "process:" + value
            resume["id"] = value
        same_process = (
            kind == "external"
            and native_process
            and value == "process:" + native_process["id"]
        )
        generation = int(prior_generation or 0) + 1
        receipt = work["refs"].get("native_process_completion") if same_process else None
        completed_event = None
        if receipt and receipt.get("identity") == native_process:
            completed_event = {"id": "process:" + native_process["id"], "generation": generation,
                               "at": receipt["completed_at"],
                               "payload": {"exit_code": receipt["exit_code"], "reason": receipt["reason"]}}
        updated = store.update_work(
            work_id,
            state="waiting",
            waiting_reason=f"{kind}: {value}",
            refs={
                **(extra_refs or {}),
                "resume": resume,
                "resume_generation": generation,
                "heartbeat_at": time.time(),
                "dispatch": None,
                "resume_event": completed_event,
                "verification": None,
                "native_process": native_process if same_process else None,
                "native_process_completion": work["refs"].get("native_process_completion")
                if same_process
                else None,
            },
            expected_state=expected_state or work["state"],
            expected_refs={**work["refs"], "owner_stop": None, "pending_owner_result": None, **(expected_refs or {})},
            hermes_home=hermes_home,
        )
        if updated is not None:
            return updated
    return None



def _eligible_tool_observations(work, *, hermes_home=None, tool_message_id=None, require_success=False):
    """Read receipts from the current resolved Owner session without mutation.

    The source message lower bound and resolved-session lookup intentionally
    match the pre-existing verifier. ``require_success`` is used only by
    discovery to hide failed or ambiguous receipts; verification retains its
    historical acceptance of opaque tool content while sharing the binding
    and session checks.
    """
    from hermes_state import SessionDB

    source = work["refs"].get("owner_request") or {}
    source_sid = source.get("session_id")
    if not source_sid:
        return []
    db = SessionDB(resolve_home(hermes_home) / "state.db", read_only=True)
    try:
        sid = db.resolve_resume_session_id(source_sid) or source_sid
        rows = db.get_messages(sid)
        records = []
        for row in rows:
            if row.get("role") != "tool" or not row.get("content"):
                continue
            if tool_message_id is not None and str(row.get("id")) != str(tool_message_id):
                continue
            if int(row["id"]) <= int(source.get("message_id", 0)):
                continue
            metadata = row.get("display_metadata") or {}
            if metadata.get("owner_work_id") != work["id"]:
                continue
            if row.get("effect_disposition") in {"none", "unknown"}:
                continue
            try:
                observed = json.loads(row["content"])
                parsed = isinstance(observed, dict)
            except (TypeError, ValueError):
                observed, parsed = {}, False
            failed = (parsed and (
                observed.get("error")
                or observed.get("success") is False
                or observed.get("status") in {"error", "failed", "blocked", "timeout"}
                or observed.get("exit_code") not in (None, 0)
            ))
            dispatch = parsed and (
                observed.get("status") in {"sent", "dispatched", "running", "pending"}
                or observed.get("process_id")
                or (observed.get("session_id") and observed.get("exit_code") is None)
            )
            if require_success and (not parsed or failed or dispatch or row.get("effect_disposition") == "uncertain"):
                continue
            records.append({"row": row, "observed": observed, "parsed": parsed, "failed": bool(failed), "dispatch": bool(dispatch), "session_id": sid})
        return records
    finally:
        db.close()


def verification_candidates(work, *, hermes_home=None):
    """Expose bounded successful current-work receipts for selecting verification evidence."""
    candidates = []
    for record in _eligible_tool_observations(work, hermes_home=hermes_home, require_success=True):
        row, observed = record["row"], record["observed"]
        metadata = row.get("display_metadata") or {}
        preview = observed.get("output", observed.get("result", observed.get("message", "")))
        if not preview:
            preview = row.get("content", "")
        candidates.append({
            "message_id": int(row["id"]),
            "session_id": record["session_id"],
            "tool": metadata.get("tool") or metadata.get("tool_name") or row.get("tool_name") or "tool",
            "status": observed.get("status") or "completed",
            "output_preview": str(preview)[:240],
        })
    return sorted(candidates, key=lambda item: item["message_id"], reverse=True)[:20]

def verify(
    work_id, *, file=None, tool_message_id=None, process_id=None, hermes_home=None
):
    work = store.get_work(work_id, hermes_home)
    if not work or not work["refs"].get("owner_request"):
        raise ValueError("owner work required")
    if sum(bool(v) for v in (file, tool_message_id, process_id)) != 1:
        raise ValueError("choose one concrete verification evidence")
    completion = None
    resume = work["refs"].get("resume") or {}
    process_wait = resume.get("kind") == "external" and str(
        resume.get("id", "")
    ).startswith(("process:", "proc_"))
    if process_id or work["refs"].get("native_process") or process_wait:
        completion = _process_completion_evidence(work, process_id)
    if process_id:
        evidence = completion
    elif file:
        evidence = _file_receipt(file)
    else:
        records = _eligible_tool_observations(
            work, hermes_home=hermes_home, tool_message_id=tool_message_id
        )
        if not records:
            raise ValueError("verification requires this task's persisted tool observation")
        record = records[0]
        if record["failed"]:
            raise ValueError("failed tool observation cannot verify success")
        if record["dispatch"] and completion:
            raise ValueError(
                "dispatch acknowledgment is not external completion; use --process-id"
            )
        row = record["row"]
        evidence = {
            "kind": "tool_observation",
            "session_id": record["session_id"],
            "message_id": str(tool_message_id),
            "sha256": hashlib.sha256(str(row["content"]).encode()).hexdigest(),
        }
    return store.update_work(
        work_id,
        refs={
            "verification": {
                "id": identity(),
                "execution_id": (work["refs"].get("execution") or {}).get("id"),
                **evidence,
                "external_completion": completion,
                "resume_generation": work["refs"].get("resume_generation"),
                "verified_at": time.time(),
            }
        },
        expected_state=work["state"],
        expected_refs={**work["refs"], "owner_stop": None, "pending_owner_result": None},
        hermes_home=hermes_home,
    )



def request_finish(
    work_id,
    result,
    *,
    terminal="completed",
    hermes_home=None,
    expected_state=None,
    expected_refs=None,
    extra_refs=None,
):
    work = store.get_work(work_id, hermes_home)
    if not work or not work["refs"].get("owner_request"):
        raise ValueError("owner work required")
    from agent.autonomy.needs_you import coerce_terminal

    outcome_kind = (extra_refs or {}).get("outcome_kind")
    terminal = coerce_terminal(terminal, result, outcome_kind=outcome_kind)
    if terminal not in {"completed", "failed", "cancelled", "needs_owner"}:
        raise ValueError("invalid owner outcome")
    if not result or not result.strip() or "[SILENT]" in result:
        raise ValueError("visible owner result required")
    if terminal == "completed" and not work["refs"].get("verification"):
        raise ValueError("verify outcome before completion")
    evidence = work["refs"].get("verification") or {}
    resume = work["refs"].get("resume") or {}
    process_wait = resume.get("kind") == "external" and str(resume.get("id", "")).startswith(("process:", "proc_"))
    if terminal == "completed" and (process_wait or work["refs"].get("native_process")):
        completion = _process_completion_evidence(work)
        if evidence.get("external_completion") != completion:
            raise ValueError("verify this generation's native external completion before finishing")
    if terminal == "completed" and evidence.get("kind") == "file":
        current = _file_receipt(evidence["path"])
        if any(current[k] != evidence.get(k) for k in ("path", "sha256", "size")):
            raise ValueError("verified file changed before completion")
    cursor = int(work["refs"]["owner_request"]["message_id"])
    from hermes_state import SessionDB

    path = resolve_home(hermes_home) / "state.db"
    if path.is_file():
        db = SessionDB(path, read_only=True)
        try:
            source = work["refs"]["owner_request"]["session_id"]
            sid = db.resolve_resume_session_id(source) or source
            rows = db.get_messages(sid, limit=1, latest=True)
            if rows:
                cursor = int(rows[-1]["id"])
        finally:
            db.close()
    return store.update_work(
        work_id,
        completion_result=result,
        refs={
            **(extra_refs or {}),
            "outcome_kind": (extra_refs or {}).get("outcome_kind", "owner_input" if terminal == "needs_owner" else terminal),
            "pending_owner_result": {
                "id": identity(),
                "state": terminal,
                "verification": work["refs"].get("verification"),
                "text": result,
                "created_at": time.time(),
                "after_message_id": cursor,
            },
        },
        hermes_home=hermes_home,
        expected_state=expected_state or work["state"],
        expected_refs={**work["refs"], "owner_stop": work["refs"].get("owner_stop"),
                       "pending_owner_result": work["refs"].get("pending_owner_result"),
                       **(expected_refs or {})},
    )


def _delivery_candidate(db, work):
    source = work["refs"]["owner_request"]
    sid = db.resolve_resume_session_id(source["session_id"]) or source["session_id"]
    rows = db.get_messages(sid)
    pending = work["refs"]["pending_owner_result"]
    # Identity is minted before production persists the final assistant row.
    # Neither its text nor its position/time can donate another turn's answer.
    result_id = pending.get("id")
    candidate = next((row for row in rows if result_id
        and row.get("role") == "assistant" and not row.get("tool_calls")
        and (row.get("display_metadata") or {}).get("owner_work_id") == work["id"]
        and (row.get("display_metadata") or {}).get("owner_result_id") == result_id), None)
    return sid, candidate


def _not_started_scope_failure(agent, result):
    """Identify only a structured current-turn scope denial with zero admitted actions."""
    flags = result if isinstance(result, dict) else {}
    markers = {"execution_scope_preflight_blocked", "execution_scope_denied"}
    error_type = str(flags.get("error_type") or "")
    exit_reason = str(flags.get("turn_exit_reason") or "")
    if error_type not in markers and exit_reason not in markers:
        return False
    return _current_turn_has_admitted_action(agent) is False


def finish_turn(agent, result=None):
    work_id = getattr(agent, "_owner_continuity_work_id", None)
    if not isinstance(work_id, str) or not work_id:
        return
    work = store.get_work(work_id)
    if not work or work["state"] in store.TERMINAL_STATES:
        return
    pending = work["refs"].get("pending_owner_result")
    if not pending:
        flags = result if isinstance(result, dict) else {}
        if _not_started_scope_failure(agent, flags):
            request_finish(
                work_id,
                "The turn ended before any task action was admitted; no external effect occurred.",
                terminal="failed", hermes_home=resolve_home(),
                expected_state=work["state"],
                expected_refs={**work["refs"], "pending_owner_result": None, "owner_stop": None},
                extra_refs={"outcome_kind": "authorization_not_started"},
            )
            return
        _settle_unfinished(
            work,
            interrupted=bool(flags.get("interrupted") or flags.get("failed")),
            turn_holder=getattr(agent, "_active_session_turn_lease_holder", None),
        )
        return
    db = getattr(agent, "_session_db", None)
    if db is None:
        return
    sid, row = _delivery_candidate(db, work)
    if row is None:
        return
    source = work["refs"]["owner_request"]
    receipt = {
        "result_id": pending.get("id"),
        "verification": pending.get("verification"),
        "session_id": sid,
        "source_session_id": source["session_id"],
        "message_id": str(row["id"]),
        "content_sha256": hashlib.sha256(str(row["content"]).encode()).hexdigest(),
        "delivered_at": time.time(),
    }
    store.update_work(
        work_id,
        state=pending["state"],
        completion_result=pending["text"],
        refs={"owner_delivery": receipt, "pending_owner_result": None},
        expected_state=work["state"],
        expected_refs={"pending_owner_result": pending},
    )


def signal_event(work_id, event_id, *, payload=None, hermes_home=None):
    work = store.get_work(work_id, hermes_home)
    if not work or work["state"] != "waiting":
        return False
    resume = work["refs"].get("resume", {})
    if resume.get("kind") == "until" or resume.get("id") != event_id:
        return False
    generation = work["refs"].get("resume_generation")
    if work["refs"].get("resume_event"):
        return False
    return (
        store.update_work(
            work_id,
            refs={
                "resume_event": {
                    "id": event_id,
                    "generation": generation,
                    "payload": payload or {},
                    "at": time.time(),
                }
            },
            expected_state="waiting",
            expected_refs={"resume_generation": generation, "resume_event": None},
            hermes_home=hermes_home,
        )
        is not None
    )


def resume_due(work, now=None):
    now = time.time() if now is None else now
    if work["state"] != "waiting" or work["refs"].get("pending_owner_result"):
        return False
    resume = work["refs"].get("resume", {})
    validate_transition(work, work)
    if resume["kind"] == "until":
        return now >= _timestamp(resume["at"])
    event = work["refs"].get("resume_event") or {}
    return (
        event.get("id") == resume.get("id")
        and event.get("generation") == work["refs"].get("resume_generation")
    ) or now >= _timestamp(resume["deadline"])


def _source_has_active_turn(work, home, *, now=None):
    """Read the native canonical lease, including compression and dead-holder rules."""
    from hermes_state import SessionDB, _compression_lock_holder_process_is_dead

    if not (home / "state.db").is_file():
        return False
    db = SessionDB(home / "state.db", read_only=True)
    try:
        with db._read_ctx() as conn:
            key = db._session_turn_lease_key_on_conn(
                conn, work["refs"]["owner_request"]["session_id"]
            )
            row = conn.execute(
                "SELECT holder,expires_at FROM session_turn_leases WHERE conversation_id=?",
                (key,),
            ).fetchone()
            return bool(
                row
                and float(row[1]) > (time.time() if now is None else now)
                and not _compression_lock_holder_process_is_dead(row[0])
            )
    finally:
        db.close()


def _defer_busy_resume(work, home, *, dispatched=None, attempts=None):
    """A skipped one-shot must not consume the fallback for an unfinished owner task.

    Fence the old job with a new generation while retaining its exact wake condition.
    If another turn changed the task, its state wins; never overwrite a Stop/result.

    A deferral *undoes* its dispatch, so ``resume_attempts`` rewinds to the
    pre-dispatch snapshot: being skipped because the source was busy is not a
    failed attempt. A caller re-arming a dispatch that genuinely ran and failed
    passes ``attempts`` to keep that count, because its retries must terminate.
    """
    expected = dispatched or work
    generation = work["refs"]["resume_generation"]
    # A native completion signal can arrive between the first read and this CAS.
    # Refresh it rather than replacing a newly persisted event with an old snapshot.
    for _ in range(3):
        current = store.get_work(work["id"], home)
        if (
            not current
            or current["state"] != expected["state"]
            or current["refs"].get("resume_generation") != generation
            or current["refs"].get("pending_owner_result")
            or current["refs"].get("dispatch") != expected["refs"].get("dispatch")
        ):
            return None
        event = current["refs"].get("resume_event")
        next_event = (
            {**event, "generation": generation + 1}
            if event and event.get("generation") == generation
            else event
        )
        updated = store.update_work(
            work["id"],
            state="waiting",
            refs={
                "resume_generation": generation + 1,
                "resume_event": next_event,
                "resume_job": None,
                "dispatch": work["refs"].get("dispatch"),
                "resume_attempts": (
                    work["refs"].get("resume_attempts") if attempts is None else attempts
                ),
            },
            expected_state=current["state"],
            expected_refs={
                "resume_generation": generation,
                "pending_owner_result": None,
                "resume_event": event,
                "dispatch": current["refs"].get("dispatch"),
            },
            hermes_home=home,
        )
        if updated is not None:
            return updated
    raise RuntimeError("continuation deferral changed concurrently")


def _write_resume_script(work, home):
    """Native cron owns execution/timeout, using its constrained scripts directory."""
    import os, tempfile

    directory = home / "scripts"
    directory.mkdir(parents=True, exist_ok=True)
    generation = int(work["refs"]["resume_generation"])
    path = directory / (
        "owner-continuation-" + work["id"] + "-" + str(generation) + ".py"
    )
    if path.is_symlink():
        raise ValueError("unsafe continuation script")
    repository = Path(__file__).resolve().parents[2]
    generation = int(work["refs"]["resume_generation"])
    content = (
        "import sys\nsys.path.insert(0," + repr(str(repository)) + ")\n"
        "from agent.autonomy.owner_continuity import run_resume\n"
        "raise SystemExit(run_resume("
        + repr(work["id"])
        + ","
        + str(generation)
        + ",hermes_home="
        + repr(str(home))
        + "))\n"
    )
    fd, name = tempfile.mkstemp(prefix=".owner-continuation-", dir=directory)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return path


def _still_holds_lease(db, sid, holder):
    with db._read_ctx() as conn:
        key = db._session_turn_lease_key_on_conn(conn, sid)
        row = conn.execute(
            "SELECT holder,expires_at FROM session_turn_leases WHERE conversation_id=?",
            (key,),
        ).fetchone()
    return bool(row and row[0] == holder and float(row[1]) > time.time())


def _rearm_under_source_lease(work, home, generation, event, next_event, attempts):
    """Re-arm a dead continuation job while holding the source's turn lease."""
    import os, uuid
    from hermes_state import SessionDB

    sid = work["refs"]["owner_request"]["session_id"]
    holder = f"owner-rearm:{os.getpid()}:{uuid.uuid4().hex}"
    db = SessionDB(home / "state.db")
    try:
        if not db.try_acquire_session_turn_lease(sid, holder, ttl_seconds=30, patience_s=0):
            return False
        try:
            # A check before the write only narrows the window: the store lock
            # can be held longer than the lease lives, and a native turn takes
            # the source in that gap. So check, write, then check AGAIN -- and
            # if the lease was lost across the write, put the row back exactly
            # as it was found. An external review reproduced the un-fenced
            # version by stalling inside the store for the full lease TTL.
            if not _still_holds_lease(db, sid, holder):
                return False
            before = {"resume_job": work["refs"].get("resume_job"),
                      "resume_generation": generation,
                      "resume_event": event,
                      "dispatch": work["refs"].get("dispatch"),
                      # A turn that took the lease can verify a completed process
                      # between this check and this write. Fencing it only on the
                      # way OUT fixed one ordering and left the other: a stale
                      # re-arm committed over the verification, and compensation
                      # then correctly refused to touch it.
                      "verification": work["refs"].get("verification"),
                      "pending_owner_result": None}
            updated = store.update_work(
                work["id"],
                refs={"resume_job": None,
                      "resume_generation": generation + 1,
                      "resume_event": next_event,
                      "resume_attempts": attempts + 1},
                expected_state=work["state"],
                expected_refs=before,
                hermes_home=home,
            )
            if updated is None:
                return False
            if _still_holds_lease(db, sid, holder):
                return True
            # Lost it mid-write. Undo everything the re-arm wrote, TOGETHER.
            # Restoring the generation while leaving the event stamped with the
            # migrated one strands an already-signalled wake: `resume_due` stops
            # matching it, `signal_event` refuses to re-signal, and the task
            # waits out its deadline looking merely patient. Fenced on all three,
            # so a concurrent writer's state is left exactly as it found it --
            # and if that fence fails, the row keeps the consistent post-re-arm
            # state it already has, at the cost of one spent retry.
            for _ in range(3):
                current = store.get_work(work["id"], home)
                if (not current
                        or current["refs"].get("resume_generation") != generation + 1
                        or int(current["refs"].get("resume_attempts") or 0) != attempts + 1
                        or current["refs"].get("resume_event") != next_event
                        or current["refs"].get("verification") is not None):
                    break  # somebody else moved it on; their state wins
                if store.update_work(
                    work["id"],
                    refs={"resume_job": before["resume_job"],
                          "resume_generation": generation,
                          "dispatch": before["dispatch"],
                          "resume_event": event,
                          "resume_attempts": attempts},
                    expected_state=current["state"],
                    expected_refs={"resume_generation": generation + 1,
                                   "resume_attempts": attempts + 1,
                                   "resume_event": next_event,
                                   # The turn that took the lease can have
                                   # verified at the NEW generation; rewinding
                                   # under it leaves a row that cannot complete.
                                   "verification": None},
                    hermes_home=home,
                ) is not None:
                    break
            return False
        finally:
            db.release_session_turn_lease(sid, holder)
    finally:
        db.close()


def reconcile(hermes_home=None, *, now=None):
    """Called under the existing native cron tick lock and its drain/ESTOP gates."""
    home = resolve_home(hermes_home)
    if not (home / "autonomy/work.db").is_file():
        return 0
    from cron.jobs import create_job, list_jobs, use_cron_store

    queued = 0
    with use_cron_store(home):
        jobs = list_jobs(include_disabled=True)
        for work in store.list_work(home, states=list(store.OPEN_STATES)):
            try:
                if not work["refs"].get("owner_request"):
                    continue
                if _source_has_active_turn(work, home, now=now):
                    continue
                if work["refs"].get("pending_owner_result"):
                    deliver_pending(work, home)
                    continue
                if work["state"] == "working":
                    _recover_orphan(work, home, now=now)
                    continue
                _poll_native_event(work, home, jobs)
                work = store.get_work(work["id"], home)
                if not resume_due(work, now):
                    continue
                generation = int(work["refs"]["resume_generation"])
                name = f"owner-continuation:{work['id']}:{generation}"
                existing = next((j for j in jobs if j.get("name") == name), None)
                if existing:
                    if (
                        existing.get("state") in {"paused", "completed", "error"}
                        or existing.get("enabled") is False
                    ):
                        # A one-shot goes `enabled=False, state="completed"` after
                        # EVERY run and `state="error"` on the other branch, so
                        # this fires for a job that failed to start as readily as
                        # for one the Owner paused. Treated as a terminal Owner
                        # escalation it was a fourth producer of the same defect,
                        # and a worse one: no retry at all, and no marker, so one
                        # cron-side failure pinned "Needs you" on the first tick.
                        #
                        # Re-arm it like any other transport failure. The
                        # GENERATION is what does that: the job is looked up by
                        # `owner-continuation:<id>:<generation>` a few lines up,
                        # so clearing `resume_job` alone left `existing` truthy
                        # forever and `create_job` unreachable — a bounded loop
                        # that never actually retried and then died silently.
                        # Bumping it changes the name, so the next tick mints a
                        # live job, which is the real recovery from a dead one.
                        attempts = int(work["refs"].get("resume_attempts") or 0)
                        # Retrying is not the same question as de-badging. A
                        # dispatch that never started ran no step whatever the
                        # row did earlier, and the continuation prompt itself
                        # forbids replaying an uncertain action -- so prior
                        # effects are no reason to withhold the retry. Gating
                        # this on recorded effects gave rows with prior work
                        # zero retries and an immediate escalation.
                        if attempts < UNADMITTED_RESUME_ATTEMPTS:
                            # The generation fences the JOB, but an event that
                            # already fired is stamped with the generation it
                            # arrived under. Bumping one without the other
                            # strands a signalled event: `resume_due` stops
                            # matching it and no replacement job is ever minted,
                            # because `signal_event` refuses to re-signal.
                            # `_defer_busy_resume` migrates it; so must this.
                            event = work["refs"].get("resume_event")
                            next_event = (
                                {**event, "generation": generation + 1}
                                if event and event.get("generation") == generation
                                else event
                            )
                            # The loop's busy probe happened before this branch,
                            # and a native turn can take the source lease in
                            # between; charging it a transport retry would spend
                            # the budget of the task it is running right now. A
                            # second probe only narrows that window -- the turn
                            # can start between the probe and the write, and it
                            # changes nothing this CAS watches. So hold the same
                            # lease the turn would need, exactly as the failure
                            # path does, and give up the tick if it is taken.
                            if not _rearm_under_source_lease(
                                work, home, generation, event, next_event, attempts
                            ):
                                continue
                        else:
                            _settle_owner_state(
                                work, home,
                                result=(
                                    "The registered continuation did not resume this task. Its native schedule is paused or no longer runnable; I have not replayed any task action."
                                    if not ever_admitted(work)
                                    else _interrupted_result(False, attempts, True)
                                ),
                                extra_refs={"outcome_kind": "transport_exhausted"},
                            )
                    continue
                script = _write_resume_script(work, home)
                job = create_job(
                    prompt=None,
                    # Already due: let this native tick claim the job immediately.
                    # A future timestamp would defer it an entire ticker interval.
                    schedule=datetime.now(timezone.utc).isoformat(),
                    name=name,
                    repeat=1,
                    deliver="local",
                    script=str(script),
                    no_agent=True,
                    native_continuation={"work_id": work["id"], "generation": generation,
                                         "hermes_home": str(home)},
                )
                store.update_work(
                    work["id"],
                    refs={"resume_job": {"id": job["id"], "generation": generation}},
                    hermes_home=home,
                )
                queued += 1
            except Exception as exc:
                # Keep the durable responsibility and every other native job.
                # Never include task text, tool output, or credentials in diagnostics.
                logger.warning(
                    "Owner task reconciliation deferred for %s (%s)",
                    work["id"],
                    type(exc).__name__,
                )
    return queued


def run_resume(work_id, generation, *, hermes_home=None, runner=None):
    """Native cron -> same canonical CLI session, with a durable generation claim.

    Unknown interrupted execution is never blindly repeated. This entrypoint
    accepts only a previously registered due work identity, not arbitrary text.
    """
    import os, subprocess, sys, uuid
    from hermes_state import SessionDB

    home = resolve_home(hermes_home)
    work = store.get_work(work_id, home)
    if (
        not work
        or not resume_due(work)
        or int(work["refs"].get("resume_generation", -1)) != generation
    ):
        print("[SILENT]")
        return 0
    if _source_has_active_turn(work, home):
        _defer_busy_resume(work, home)
        print("[SILENT]")
        return 0
    nonce = uuid.uuid4().hex
    from gateway.status import get_process_start_time

    dispatched = store.update_work(
        work_id,
        state="working",
        refs={
            "dispatch": {
                "generation": generation,
                "nonce": nonce,
                "pid": os.getpid(),
                "started_at": time.time(),
                "process_started_at": get_process_start_time(os.getpid()),
            },
            "heartbeat_at": time.time(),
            "resume_attempts": int(work["refs"].get("resume_attempts") or 0) + 1,
        },
        expected_state="waiting",
        expected_refs={"resume_generation": generation, "pending_owner_result": None},
        hermes_home=home,
    )
    if dispatched is None:
        print("[SILENT]")
        return 0
    if _source_has_active_turn(work, home):
        _defer_busy_resume(work, home, dispatched=dispatched)
        print("[SILENT]")
        return 0
    db = SessionDB(home / "state.db", read_only=True)
    try:
        sid = (
            db.resolve_resume_session_id(work["refs"]["owner_request"]["session_id"])
            or work["refs"]["owner_request"]["session_id"]
        )
    finally:
        db.close()
    prompt = (
        f"[Owner task continuation, not the user. Work {work_id}; generation {generation}.]\n"
        "Resume the existing responsibility. Read work-get for the exact source, wake condition and prior evidence. "
        "First verify existing effects and pending approvals; never replay an uncertain send, deploy, delete or other consequential action. "
        "A child result requires your verification, not automatic completion. Preserve all approval gates. "
        "Do the next authorized step, register another work-wait if necessary, verify and work-complete then return the final result."
    )
    env = {
        **os.environ,
        "HERMES_HOME": str(home),
        "HERMES_BACKGROUND_DELIVERY": "1",
        "HERMES_OWNER_CONTINUATION_ID": work_id,
        "HERMES_OWNER_CONTINUATION_NONCE": nonce,
    }
    argv = [
        sys.executable,
        "-m",
        "hermes_cli.main",
        "chat",
        "--cli",
        "--resume",
        sid,
        "-Q",
        "--query-file",
        "-",
    ]
    try:
        result = (runner or _native_resume_command)(
            argv,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=_native_resume_timeout(),
            env=env,
            cwd=str(Path(__file__).resolve().parents[2]),
        )
        if result.returncode != 0:
            raise RuntimeError("native continuation did not finish successfully")
    except Exception as exc:
        current = store.get_work(work_id, home)
        dispatch = (current or {}).get("refs", {}).get("dispatch") or {}
        # Another native event/Owner turn may have won after the preflight. Its
        # outcome cannot be replaced by this losing CLI's exit or timeout.
        if (
            not current
            or current["state"] != "working"
            or current["refs"].get("pending_owner_result")
            or current["refs"].get("resume_generation") != generation
            or dispatch.get("nonce") != nonce
        ):
            print("[SILENT]")
            return 0
        admitted = not dispatch_never_started(current)
        attempts = int(current["refs"].get("resume_attempts") or 0)
        # The one place this failure was observable at all. Identity only —
        # never task text, tool output or credentials.
        logger.warning(
            "Owner continuation dispatch failed: work=%s generation=%s attempt=%s "
            "admitted=%s source_session=%s cause=%s",
            work_id, generation, attempts, admitted,
            work["refs"]["owner_request"]["session_id"], type(exc).__name__,
        )
        failure_db = None
        failure_holder = f"owner-resume-failure:{os.getpid()}:{nonce}"
        locked = False
        try:
            if not admitted:
                # Atomically exclude an event turn starting between the failure
                # probe and outbox commit. No model turn ran for this dispatch.
                failure_db = SessionDB(home / "state.db")
                locked = failure_db.try_acquire_session_turn_lease(
                    sid, failure_holder, ttl_seconds=30, patience_s=0
                )
                if not locked:
                    # Deliberately WITHOUT ``attempts``: losing the lease means a
                    # native turn is running the source right now, and that turn
                    # is the legitimate one. Charging this cron dispatch for it
                    # would let an active Owner turn burn the retry budget of the
                    # task it is already working on (pinned by
                    # test_already_queued_cron_cannot_poison_active_native_event_work).
                    _defer_busy_resume(work, home, dispatched=current)
                    print("[SILENT]")
                    return 0
                if attempts < UNADMITTED_RESUME_ATTEMPTS:
                    # ``admitted`` is the native proof that ``bind_turn`` never
                    # ran, so this dispatch executed no model turn: it attempted
                    # no task action, produced no effect and replayed nothing.
                    # That is infrastructure telemetry about Hermes, not an Owner
                    # decision and not Owner-facing speech. Re-arm the SAME
                    # durable wake condition under the existing generation fence
                    # instead of escalating; the responsibility is never dropped.
                    deferred = _defer_busy_resume(
                        work, home, dispatched=current, attempts=attempts
                    )
                    print("[SILENT]")
                    return 0 if deferred is not None else 1
            # `ran_before` decides the wording. Nothing decides the badge here
            # any more: a spent retry budget is an Owner obligation either way.
            ran_before = ever_admitted(current)
            failure = request_finish(
                work_id,
                _interrupted_result(admitted, attempts, ran_before),
                terminal="needs_owner",
                hermes_home=home,
                expected_state="working",
                expected_refs={
                    "dispatch": dispatch,
                    "resume_generation": generation,
                    "pending_owner_result": None,
                },
                extra_refs={"outcome_kind": "uncertain_execution" if admitted else "transport_exhausted"},
                # No marker. Reaching here means the retry budget is SPENT:
                # Hermes tried, could not continue, and has stopped trying. Only
                # the Owner can restart it, so that is a real obligation and it
                # keeps its badge. The single transport failure that started
                # this incident never reaches here at all -- it re-arms above,
                # silently, which is what the defect should have done.
            )
        finally:
            if failure_db is not None:
                if locked:
                    failure_db.release_session_turn_lease(sid, failure_holder)
                failure_db.close()
        print("[SILENT]")
        return 1 if failure is not None else 0
    remaining = store.get_work(work_id, home)
    if (
        remaining
        and (remaining["refs"].get("dispatch") or {}).get("nonce") == nonce
        and remaining["refs"].get("resume_generation") == generation
        and not _source_has_active_turn(remaining, home)
    ):
        _settle_unfinished(remaining, home)
    print("[SILENT]")
    return 0


def deliver_pending(work, hermes_home=None):
    """Idempotent canonical transcript outbox; the native turn lease fences writers."""
    import os, uuid
    from hermes_state import SessionDB

    home = resolve_home(hermes_home)
    if (
        not work["refs"].get("pending_owner_result")
        or not (home / "state.db").is_file()
    ):
        return False
    db = SessionDB(home / "state.db")
    holder = f"owner-result:{os.getpid()}:{uuid.uuid4().hex}"
    source = work["refs"]["owner_request"]
    sid = db.resolve_resume_session_id(source["session_id"]) or source["session_id"]
    locked = False
    try:
        _source_row(db, source["session_id"], source["message_id"])
        locked = db.try_acquire_session_turn_lease(
            sid, holder, ttl_seconds=30, patience_s=0
        )
        if not locked:
            return False
        current = store.get_work(work["id"], home)
        if (
            not current
            or current["state"] in store.TERMINAL_STATES
            or current["refs"].get("pending_owner_result")
            != work["refs"]["pending_owner_result"]
        ):
            return False
        work = current
        pending = work["refs"]["pending_owner_result"]
        existing = next(
            (
                r
                for r in db.get_messages(sid, include_inactive=True)
                if (r.get("display_metadata") or {}).get("owner_work_id") == work["id"]
                and pending.get("id")
                and (r.get("display_metadata") or {}).get("owner_result_id") == pending["id"]
            ),
            None,
        )
        if existing is None:
            _, existing = _delivery_candidate(db, work)
        if existing is None:
            previous = db.get_messages(sid, limit=1, latest=True)
            rows = []
            if previous and previous[-1].get("role") != "user":
                rows.append({
                    "role": "user",
                    "content": "[Owner task result delivery, not the user.]",
                    "display_kind": "control",
                })
            rows.append({
                "role": "assistant",
                "content": pending["text"],
                "display_metadata": {
                    "owner_work_id": work["id"],
                    "owner_result_id": pending.get("id"),
                },
            })
            from agent.message_projection import native_metadata
            rows[-1]["display_metadata"] = native_metadata(
                "agent", "owner", "decision" if pending["state"] == "needs_owner" else "result",
                metadata=rows[-1]["display_metadata"], work_id=work["id"],
                source_session_id=source["session_id"], source_message_id=source["message_id"])
            db.append_messages_batch(sid, rows, turn_lease_holder=holder)
            message_id = rows[-1]["_row_id"]
            content = pending["text"]
        else:
            message_id = existing["id"]
            content = existing["content"]
        updated = store.update_work(
            work["id"],
            state=pending["state"],
            completion_result=pending["text"],
            refs={
                "pending_owner_result": None,
                "owner_delivery": {
                    "result_id": pending.get("id"),
                    "verification": pending.get("verification"),
                    "session_id": sid,
                    "source_session_id": source["session_id"],
                    "message_id": str(message_id),
                    "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                    "delivered_at": time.time(),
                },
            },
            hermes_home=home,
            expected_state=work["state"],
            expected_refs={"pending_owner_result": pending},
        )
        return updated is not None
    finally:
        if locked:
            db.release_session_turn_lease(sid, holder)
        db.close()


def completed_child_delivery(delegation_id, parent_session_id, *, hermes_home=None):
    """Read-only proof for legacy notifications whose profile ACK was misplaced.

    An injected hidden row alone is insufficient: interrupted/unverified work must
    remain recoverable. Only an already completed, source-bound verified delivery
    lets native delegation recovery acknowledge the event without another turn.
    """
    import json, sqlite3
    from hermes_state import SessionDB

    home = resolve_home(hermes_home)
    work_path = home / "autonomy" / "work.db"
    if not parent_session_id or not work_path.is_file() or not (home / "state.db").is_file():
        return False
    try:
        conn = sqlite3.connect(work_path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            refs_rows = conn.execute("SELECT refs_json FROM work WHERE state='completed'").fetchall()
        finally:
            conn.close()
        db = SessionDB(home / "state.db", read_only=True)
        try:
            target = db.resolve_resume_session_id(parent_session_id) or parent_session_id
            lineage = set(db.get_compression_lineage(target) or [parent_session_id]) | {parent_session_id, target}
            messages = [m for sid in lineage for m in db.get_messages(sid, include_inactive=True)]
            events = [m for m in messages if m.get("role") == "user" and m.get("display_kind") == "hidden"
                      and m.get("active", True)
                      and (m.get("display_metadata") or {}).get("delegation_id") == delegation_id]
            if not events:
                return False
            for (raw,) in refs_rows:
                refs = json.loads(raw or "{}")
                source = refs.get("owner_request") or {}
                verification = refs.get("verification") or {}
                delivery = refs.get("owner_delivery") or {}
                resume = refs.get("resume") or {}
                if (source.get("session_id") != parent_session_id
                        or delegation_id not in (refs.get("native_children") or [])
                        or resume.get("kind") != "child" or resume.get("id") != delegation_id
                        or refs.get("owner_stop") or refs.get("pending_owner_result")
                        or not verification.get("kind") or not verification.get("verified_at")
                        or delivery.get("source_session_id") != parent_session_id
                        or delivery.get("session_id") not in lineage):
                    continue
                _source_row(db, source["session_id"], source["message_id"])
                for final in messages:
                    if (str(final.get("id")) == str(delivery.get("message_id"))
                            and final.get("session_id") == delivery["session_id"]
                            and final.get("role") == "assistant" and final.get("active", True)
                            and not final.get("tool_calls") and not final.get("display_kind")
                            and hashlib.sha256(str(final.get("content") or "").encode()).hexdigest() == delivery.get("content_sha256")
                            and delivery.get("delivered_at", 0) >= verification["verified_at"]
                            and any(e.get("timestamp", float("inf")) <= verification["verified_at"]
                                    and e.get("timestamp", float("inf")) < final.get("timestamp", 0) for e in events)):
                        return True
        finally:
            db.close()
    except Exception:
        # Unknown/missing canonical proof never consumes an unfinished result.
        return False
    return False


def prepare_dispatch(agent, name, args):
    """Register before actual asynchronous work leaves a trusted Owner turn."""
    source = getattr(agent, "_owner_continuity_source", None)
    work_id = getattr(agent, "_owner_continuity_work_id", None)
    if isinstance(work_id, str):
        current = store.get_work(work_id)
        if current and current["refs"].get("owner_stop"):
            raise ValueError("Owner stopped this source request before dispatch")
        return
    asynchronous = (
        name == "delegate_task"
        and args.get("action", "delegate") not in {"status", "list", "cancel"}
        or name in {"cronjob", "cronjob_manage"}
        and args.get("action") == "create"
        or name == "terminal"
        and args.get("background") is True
        or name == "message_agent"
    )
    if not asynchronous:
        return
    if not isinstance(source, tuple) or len(source) != 2:
        if getattr(agent, "_owner_continuity_persistence_required", False) is True:
            raise RuntimeError(
                "Owner task source must persist before asynchronous dispatch"
            )
        return
    work = register_owner_request(agent._session_db, *source, force=True)
    _bind_work(agent, work)
    if work["refs"].get("owner_stop"):
        raise ValueError("Owner stopped this source request before dispatch")



def observe_dispatch(agent, name, args, result, *, failed=False, blocked=False):
    work_id = getattr(agent, "_owner_continuity_work_id", None)
    if failed or blocked:
        return result
    try:
        payload = json.loads(result) if isinstance(result, str) else result
    except (TypeError, ValueError):
        return result
    if not isinstance(payload, dict):
        return result
    if not isinstance(work_id, str) or not work_id:
        source = getattr(agent, "_owner_continuity_source", None)
        if (
            name not in {"terminal", "message_agent"}
            or not (payload.get("session_id") or payload.get("process_id"))
            or not isinstance(source, tuple)
            or len(source) != 2
        ):
            return result
        work = register_owner_request(agent._session_db, *source, force=True)
        _bind_work(agent, work)
        work_id = work["id"]
    current = store.get_work(work_id)
    if current and current["refs"].get("owner_stop"):
        return result
    deadline = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    resume = None
    if (
        name == "delegate_task"
        and payload.get("status") == "dispatched"
        and payload.get("delegation_id")
    ):
        current = store.get_work(work_id)
        children = list(current["refs"].get("native_children") or [])
        if str(payload["delegation_id"]) not in children:
            children.append(str(payload["delegation_id"]))
        store.update_work(work_id, refs={"native_children": children})
        resume = wait(work_id, child=str(payload["delegation_id"]), deadline=deadline)
    elif (
        name in {"cronjob", "cronjob_manage"}
        and args.get("action") == "create"
        and payload.get("success")
        and payload.get("job_id")
    ):
        # The original native job owns its effects. This responsibility only
        # watches its native completion; it never copies/replays the job prompt.
        at = payload.get("next_run_at")
        if at:
            try:
                deadline = datetime.fromtimestamp(
                    _timestamp(at) + 600, timezone.utc
                ).isoformat()
            except ValueError:
                pass
        resume = wait(
            work_id, event="cron:" + str(payload["job_id"]), deadline=deadline
        )
    elif (name == "terminal" and payload.get("session_id")) or (
        name == "message_agent"
        and payload.get("status") == "sent"
        and payload.get("process_id")
    ):
        from tools.process_registry import process_registry

        process_id = str(payload.get("session_id") or payload["process_id"])
        process = process_registry.get(process_id)
        if process and process.id == process_id and process.parent_session_id in agent._session_db.get_compression_lineage(current["refs"]["owner_request"]["session_id"]):
            home = str(resolve_home())
            bound = None
            with process._lock:
                ownership = (process.owner_continuity_work_id, process.owner_continuity_home)
                if ownership != ("", ""):
                    if ownership != (work_id, home):
                        raise ValueError("native process already belongs to another Owner responsibility")
                    existing = store.get_work(work_id)
                    if existing["refs"].get("native_process") != _process_identity(process, existing["refs"]["owner_request"]):
                        raise ValueError("previous native process is not this work's current operation")
                    resume = existing
                else:
                    resume = wait(work_id, external="process:" + process_id, deadline=deadline)
                    if resume is not None:
                        identity = _process_identity(process, resume["refs"]["owner_request"])
                        bound = store.update_work(
                            work_id, refs={"native_process": identity, "native_process_completion": None},
                            expected_state="waiting",
                            expected_refs={**resume["refs"], "owner_stop": None, "pending_owner_result": None},
                        )
                        if bound:
                            process.owner_continuity_work_id = work_id
                            process.owner_continuity_home = home
            if bound:
                process_registry._write_checkpoint()
                if process.exited:
                    signal_process_completion(process)
        else:
            # The actual command result remains canonical, but an unproven handle
            # is not a success receipt. Its existing external deadline stays durable.
            resume = wait(work_id, external="process:" + process_id, deadline=deadline)
    if resume:
        payload = {
            **payload,
            "owner_continuity": {
                "work_id": work_id,
                "resume": resume["refs"]["resume"],
                "requirement": "Verify the child/process outcome before work-complete and final Owner delivery.",
            },
        }
        return (
            json.dumps(payload, ensure_ascii=False)
            if isinstance(result, str)
            else payload
        )
    return result



def _poll_native_event(work, home, jobs):
    """Read existing native completion identities; never invent an event bus."""
    if work["state"] != "waiting":
        return
    resume = work["refs"].get("resume") or {}
    if resume.get("kind") == "child":
        import sqlite3

        path = home / "state.db"
        if not path.is_file():
            return
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='async_delegations'"
            ).fetchone():
                return
            row = conn.execute(
                "SELECT parent_session_id,state,completed_at FROM async_delegations WHERE delegation_id=?",
                (resume["id"],),
            ).fetchone()
            # The dispatch hook captured the exact native ID in this source
            # turn; additionally require native originating conversation.
            from hermes_state import SessionDB

            db = SessionDB(path, read_only=True)
            try:
                source = work["refs"]["owner_request"]["session_id"]
                lineage = {source, db.resolve_resume_session_id(source) or source}
            finally:
                db.close()
            children = work["refs"].get("native_children") or [resume["id"]]
            all_done = all(
                (
                    child_row := conn.execute(
                        "SELECT parent_session_id,completed_at FROM async_delegations WHERE delegation_id=?",
                        (child_id,),
                    ).fetchone()
                )
                and child_row[0] in lineage
                and child_row[1] is not None
                for child_id in children
            )
            if row and row[0] in lineage and row[2] is not None and all_done:
                signal_event(
                    work["id"],
                    resume["id"],
                    payload={"native_state": row[1]},
                    hermes_home=home,
                )
        finally:
            conn.close()
    elif resume.get("kind") == "event" and str(resume.get("id", "")).startswith(
        "cron:"
    ):
        job = next((j for j in jobs if j["id"] == resume["id"][5:]), None)
        if job and job.get("last_run_at"):
            signal_event(
                work["id"],
                resume["id"],
                payload={"native_status": job.get("last_status")},
                hermes_home=home,
            )



def _settle_owner_state(work, home, *, result=None, until=None, turn_holder=None,
                        extra_refs=None):
    # ``extra_refs`` rides whichever write this settlement makes -- the wait or
    # the escalation -- so a caller's bookkeeping cannot survive without it.
    """Serialize idle settlement; an actual finishing turn retains its own lease.

    The work CAS still matters: CLI wait/Stop/result updates can occur without
    acquiring a new model turn. A stale scheduler snapshot has no authority over them.
    """
    import os, uuid
    from hermes_state import SessionDB

    if work["refs"].get("pending_owner_result") or work["refs"].get("owner_stop"):
        return None
    db = SessionDB(home / "state.db")
    sid = work["refs"]["owner_request"]["session_id"]
    holder = turn_holder or f"owner-settlement:{os.getpid()}:{uuid.uuid4().hex}"
    acquired = False
    try:
        if turn_holder:
            # The synchronous finish hook runs inside this already admitted lease.
            # Never renew/release it here; its native owner controls that lifecycle.
            with db._read_ctx() as conn:
                key = db._session_turn_lease_key_on_conn(conn, sid)
                row = conn.execute(
                    "SELECT holder,expires_at FROM session_turn_leases WHERE conversation_id=?",
                    (key,),
                ).fetchone()
                if not row or row[0] != turn_holder or float(row[1]) <= time.time():
                    return None
        else:
            acquired = db.try_acquire_session_turn_lease(
                sid, holder, ttl_seconds=30, patience_s=0
            )
            if not acquired:
                return None
        expected = {**work["refs"], "pending_owner_result": None, "owner_stop": None}
        if until is not None:
            return wait(
                work["id"],
                until=until,
                hermes_home=home,
                expected_state=work["state"],
                expected_refs=expected,
                extra_refs=extra_refs,
            )
        return request_finish(
            work["id"],
            result,
            terminal="needs_owner",
            hermes_home=home,
            expected_state=work["state"],
            expected_refs=expected,
            extra_refs={"outcome_kind": "transport_exhausted" if dispatch_never_started(work) else "uncertain_execution",
                        **(extra_refs or {})},
        )
    finally:
        if acquired:
            db.release_session_turn_lease(sid, holder)
        db.close()

def _current_turn_has_admitted_action(agent):
    """Prove an action was admitted by this exact native turn attempt."""
    from agent.execution_scope import get_binding

    binding = get_binding(str(getattr(agent, "_current_turn_id", "")))
    if binding is None or not getattr(binding, "scope_id", None):
        return None
    return binding.db.scope_has_actions(binding.scope_id, attempt_id=str(agent._current_turn_id))



def _settle_unfinished(work, hermes_home=None, *, interrupted=False, turn_holder=None):
    """A finished model turn cannot leave a bare working promise indefinitely."""
    if work["state"] != "working" or work["refs"].get("pending_owner_result"):
        return
    home = resolve_home(hermes_home)
    attempts = int(work["refs"].get("resume_attempts") or 0)
    settlements = int(work["refs"].get("unfinished_settlements") or 0)
    if (interrupted or attempts >= UNADMITTED_RESUME_ATTEMPTS
            or settlements >= UNFINISHED_SETTLEMENTS):
        # Automatic continuation has stopped, either because the budget is
        # spent or because this turn was interrupted. Either way the task is
        # parked until the Owner says otherwise, so it keeps its badge.
        unstarted = dispatch_never_started(work)
        _settle_owner_state(
            work,
            home,
            turn_holder=turn_holder,
            result=(
                "This task has not reached a verified outcome. I stopped automatic continuation; existing effects and any pending approval need review before another attempt."
                if not unstarted
                else _interrupted_result(False, attempts, ever_admitted(work))
            ),
        )
    else:
        # The charge has to commit WITH the retry it pays for. As two writes, a
        # failed or interrupted second one left a durable automatic retry that
        # had never been counted -- and an unbounded loop again.
        _settle_owner_state(
            work,
            home,
            turn_holder=turn_holder,
            until=(datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
            extra_refs={"unfinished_settlements": settlements + 1},
        )


def _recover_orphan(work, home, *, now=None):
    """Dead process recovery is inspection-only; unknown effects are not retried."""
    now = time.time() if now is None else now
    if now - float(work["refs"].get("heartbeat_at") or now) < 300:
        return
    if _source_has_active_turn(work, home, now=now):
        return
    process = work["refs"].get("dispatch") or work["refs"].get("owner_process") or {}
    pid = process.get("pid")
    if not pid:
        return
    from gateway.status import _pid_exists, get_process_start_time

    alive = _pid_exists(int(pid))
    started = process.get("process_started_at", process.get("started_at"))
    # dispatch.started_at is a wall timestamp, not native process identity.
    if work["refs"].get("dispatch"):
        started = process.get("process_started_at")
    if alive and (started is None or get_process_start_time(int(pid)) == started):
        expected = work["refs"].get("active_turn_lease")
        if not expected:
            return
    # A dispatch killed before bind_turn (OOM, host reboot, a deploy restart)
    # leaves the row working with an unadmitted dispatch and a stale heartbeat.
    # That dispatch ran no task turn, which the wording says -- but recovery has
    # stopped, so the row keeps its badge.
    unstarted = dispatch_never_started(work)
    attempts = int(work["refs"].get("resume_attempts") or 0)
    if unstarted and attempts < UNADMITTED_RESUME_ATTEMPTS:
        from hermes_state import SessionDB
        db = SessionDB(home / "state.db")
        holder = "owner-orphan:" + identity()
        sid = work["refs"]["owner_request"]["session_id"]
        try:
            if db.try_acquire_session_turn_lease(sid, holder, ttl_seconds=30, patience_s=0):
                try:
                    _defer_busy_resume(work, home, attempts=attempts)
                finally:
                    db.release_session_turn_lease(sid, holder)
        finally:
            db.close()
        return
    _settle_owner_state(
        work,
        home,
        result=(
            "The worker stopped before a verified outcome. I retained this task and its prior evidence. Existing effects and pending approvals must be inspected before retrying."
            if not unstarted
            # An earlier cycle may have been admitted even though THIS dispatch
            # never was, and the wording has to carry that or the distinction it
            # claims to make is not made.
            else _interrupted_result(False, attempts, ever_admitted(work))
        ),
    )


def _bind_owner_decision(db, session_id, message_id, *, prepared=None):
    """Only trusted ingress calls this; use exactly its preflight-selected target."""
    from agent.autonomy.owner_decision import decision_target, select_owner_decision

    work = select_owner_decision(db, session_id, message_id, strict=prepared is not None)
    if prepared is not None:
        if (prepared.get("session_id") != session_id
                or prepared.get("message_id") != str(message_id)
                or prepared.get("target") != decision_target(work)):
            raise ValueError("Owner decision target changed after scope preparation")
    if work is None:
        return None
    decision = {
        "session_id": session_id,
        "message_id": str(message_id),
        "at": time.time(),
    }
    updated = store.update_work(
        work["id"],
        state="working",
        waiting_reason="",
        completion_result="",
        refs={
            "owner_decision": decision,
            "owner_delivery": None,
            "pending_owner_result": None,
            "verification": None,
            "heartbeat_at": time.time(),
            "resume_generation": int(work["refs"].get("resume_generation") or 0) + 1,
            "resume_attempts": 0,
            "unfinished_settlements": None,
            "dispatch": None,
            # The task is moving again, so a stale marker must not survive to
            # suppress the badge the NEXT time this row genuinely needs the Owner.
            UNSTARTED_CONTINUATION: None,
        },
        expected_state="needs_owner",
        expected_refs=decision_target(work)["refs"],
        hermes_home=Path(db.db_path).parent,
    )
    if prepared is not None and updated is None:
        raise ValueError("Owner decision target changed before its durable transition")
    return updated


def _process_identity(process, source):
    return {
        "id": process.id,
        "started_at": process.started_at,
        "host_start_time": process.host_start_time,
        "parent_session_id": process.parent_session_id,
        "owner_request": source,
    }



def _process_completion_evidence(work, process_id=None):
    refs = work["refs"]
    native = refs.get("native_process")
    completed = refs.get("native_process_completion")
    resume = refs.get("resume") or {}
    if (
        not native
        or not completed
        or refs.get("owner_stop")
        or refs.get("pending_owner_result")
        or work["state"] not in {"waiting", "working"}
        or native.get("owner_request") != refs.get("owner_request")
        or (process_id is not None and process_id != native.get("id"))
        or resume.get("kind") != "external"
        or resume.get("id") != "process:" + native.get("id", "")
        or completed.get("identity") != native
    ):
        raise ValueError("this work requires its exact native process completion")
    if (
        type(completed.get("exit_code")) is not int
        or completed["exit_code"] != 0
        or completed.get("reason") != "exited"
    ):
        raise ValueError("failed or unknown native process cannot verify success")
    return {"kind": "native_process_completion", **completed}



def signal_process_completion(process):
    if (
        not process.owner_continuity_work_id
        or not process.owner_continuity_home
        or not process.exited
    ):
        return False
    home = resolve_home(process.owner_continuity_home)
    for _ in range(3):
        work = store.get_work(process.owner_continuity_work_id, home)
        if not work or not work["refs"].get("owner_request"):
            return False
        refs = work["refs"]
        if (
            work["state"] not in {"waiting", "working"}
            or refs.get("owner_stop")
            or refs.get("pending_owner_result")
        ):
            return False
        from hermes_state import SessionDB

        db = SessionDB(home / "state.db", read_only=True)
        try:
            original = refs["owner_request"]["session_id"]
            if process.parent_session_id not in db.get_compression_lineage(original):
                return False
        finally:
            db.close()
        identity = _process_identity(process, refs["owner_request"])
        # Legacy unbound processes may still wake their old responsibility, but cannot
        # manufacture the new typed verification proof.
        if refs.get("native_process") and refs["native_process"] != identity:
            return False
        if refs.get("native_process") == identity:
            receipt = refs.get("native_process_completion")
            if receipt is None:
                receipt = {
                    "identity": identity,
                    "exit_code": process.exit_code,
                    "reason": process.completion_reason,
                    "completed_at": time.time(),
                    "output_tail_sha256": hashlib.sha256(
                        process.output_buffer.encode()
                    ).hexdigest(),
                    "output_provenance": "native retained output buffer; may be truncated",
                }
                work = store.update_work(
                    work["id"],
                    refs={"native_process_completion": receipt},
                    expected_state=work["state"],
                    expected_refs={**refs, "owner_stop": None, "pending_owner_result": None},
                    hermes_home=home,
                )
                if work is None:
                    continue
        return signal_event(
            work["id"],
            "process:" + process.id,
            payload={
                "exit_code": process.exit_code,
                "reason": process.completion_reason,
            },
            hermes_home=home,
        )
    return False



def _native_resume_timeout():
    """Use the existing gateway timeout contract for native continuations.

    Native cron still supplies its own outer script bound; a zero gateway timeout
    therefore delegates to that bound instead of creating an unbounded child.
    """
    import math
    import os
    raw = os.environ.get("HERMES_AGENT_TIMEOUT")
    if raw is None:
        return 1800.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 1800.0
    if not math.isfinite(value):
        return 1800.0
    return None if value <= 0 else value


def _native_resume_command(argv, *, input, text, capture_output, timeout, env, cwd):
    """Reuse cron's bounded process-tree teardown when the CLI cannot finish."""
    import subprocess
    from cron.scheduler_script import _terminate_cron_script_tree, _drain_script_pipes

    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        env=env,
        cwd=cwd,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(input=input, timeout=timeout)
        return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
    except BaseException:
        _terminate_cron_script_tree(proc)
        _drain_script_pipes(proc)
        raise


def _bind_work(agent, work):
    agent._owner_continuity_work_id = work["id"]
    from agent.execution_scope import attach_work_scope
    attach_work_scope(agent, work)
    holder = getattr(agent, "_active_session_turn_lease_holder", None)
    if isinstance(holder, str) and holder:
        store.update_work(work["id"], refs={"active_turn_lease": holder,
            "execution": {"id": getattr(agent, "_current_turn_id", None) or holder,
                          "generation": work["refs"].get("resume_generation"),
                          "dispatch_nonce": (work["refs"].get("dispatch") or {}).get("nonce"),
                          "admitted": True}})


def _request_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict)
            and part.get("type") in {"text", "input_text"}
            and isinstance(part.get("text"), str)
        )
    return ""


def _file_receipt(file):
    import os
    from agent.file_safety import raise_if_read_blocked

    path = Path(file).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError("verification target is not a file")
    raise_if_read_blocked(str(path))
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("verification target changed while reading")
    return {
        "kind": "file",
        "path": str(path),
        "sha256": digest.hexdigest(),
        "size": after.st_size,
    }


def cancel_owner_session(session_id, *, hermes_home=None):
    """Explicit authenticated Stop only; disconnect/steering do not call this."""
    import os
    from hermes_state import SessionDB
    from gateway.status import get_process_start_time

    home = resolve_home(hermes_home)
    if not (home / "state.db").is_file():
        return 0
    db = SessionDB(home / "state.db", read_only=True)
    cancelled = 0
    try:
        target = db.resolve_resume_session_id(session_id) or session_id
        lineage = db.get_compression_lineage(target) or [target]
        cutoffs = {}
        for source_id in lineage:
            rows = db.get_messages(
                source_id, include_inactive=True, latest=True, limit=1
            )
            if rows:
                cutoffs[source_id] = int(rows[-1]["id"])
        works = store.record_owner_stop(cutoffs, home) if cutoffs else []
        for work in works:
            # Never kill owner_process: that may be the shared gateway. Only
            # the separately spawned, generation-bound continuation is ours.
            dispatch = work["refs"].get("dispatch") or {}
            pid = dispatch.get("pid")
            started = dispatch.get("process_started_at")
            if (
                type(pid) is int
                and pid > 1
                and pid != os.getpid()
                and started is not None
                and get_process_start_time(pid) == started
            ):
                from agent.deadline import kill_process_tree

                if (
                    not kill_process_tree(pid)
                    and get_process_start_time(pid) == started
                ):
                    request_finish(
                        work["id"],
                        "The stop request is recorded and further continuations are disabled, but the current continuation process did not acknowledge termination. Its existing effects need review.",
                        terminal="cancelled",
                        hermes_home=home,
                    )
            cancelled += 1
    finally:
        db.close()
    return cancelled
