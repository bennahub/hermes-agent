"""Initiative kernel: observation context, event claims, bounded work, A2A."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from hermes_time import now as _hermes_now

from agent.autonomy import OBSERVE_JOB_NAME, SILENT_TOKEN, SOUL_BEGIN

CRON_SKIP = '{"wakeAgent": false}\n'

JOB_NAME = OBSERVE_JOB_NAME
SOUL_MARKER = SOUL_BEGIN
from agent.autonomy import missions as mission_catalog
from agent.autonomy import store
from agent.autonomy.paths import HomeLike, profile_slug

COOLDOWN_MINUTES = 90
RESUME_BUCKET_SECONDS = 30 * 60

INITIATIVE_CONTRACT = (
    "Autonomy First — governance only at real consequence boundaries. "
    "Ordinary in-scope work does not need the Owner. Self-initiated ordinary "
    "work does NOT wait for Owner «ابدأ». If the existing lifecycle requires "
    "tracking, create or bind the Jira/work identity yourself with the tools "
    "you already have, then `hermes autonomy work-start` and bind it with "
    "`--jira <KEY>`. Do not invent a second tracker. If nothing is worth "
    f"doing, run `hermes autonomy noop` or reply with exactly {SILENT_TOKEN}. "
    "The durable ledger is the `hermes autonomy` CLI — do not use `todo` or "
    "files. Start at most one finite work unit. Unrelated findings get their "
    "own key. Verify, then `hermes autonomy work-complete`. Owner "
    "`needs_owner` is only for secrets, Production write, merge/deploy, "
    "money/legal, unavailable credentials, or a material architecture/"
    "product decision the evidence cannot choose. The CLI projects that "
    "request into the canonical Bot Chat; do not rely on cron output alone."
)

LEDGER_CLI = (
    "Use `terminal` to run these. Do not use `todo`.\n"
    "- Start: hermes autonomy work-start --key <stable-id> --why ... --outcome ... --done ... [--jira KEY]\n"
    "- Complete: hermes autonomy work-complete <id> --result ...\n"
    "  (also accepts --work-id <id>)\n"
    "- Drop: hermes autonomy work-drop <id> --reason ...\n"
    "- Teammate: hermes autonomy delegate --to <profile> --work <id> --goal ... --deliverable ... --evidence ...\n"
    "- Owner needed: hermes autonomy work-update <id> --state needs_owner --waiting-reason ...\n"
    "- Bind tracking: hermes autonomy work-update <id> --jira <KEY>\n"
    f"- Nothing worth doing: hermes autonomy noop  (prints {SILENT_TOKEN})"
)


def _home(hermes_home: HomeLike = None) -> HomeLike:
    return hermes_home


def event_key(payload: Any) -> str:
    if not isinstance(payload, dict):
        raw = json.dumps(payload, sort_keys=True, default=str)
        return "sha:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    action = str(payload.get("action") or payload.get("status") or "").strip()
    run = payload.get("workflow_run") if isinstance(payload.get("workflow_run"), dict) else None
    if run and run.get("id") not in (None, ""):
        return f"gh:run:{run['id']}:{action or 'event'}"
    check = payload.get("check_suite") if isinstance(payload.get("check_suite"), dict) else None
    if check and check.get("id") not in (None, ""):
        return f"gh:check:{check['id']}:{action or 'event'}"
    issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else None
    if issue and issue.get("key"):
        return f"jira:{issue['key']}:{action or 'event'}"
    if issue and issue.get("id") not in (None, ""):
        return f"jira:{issue['id']}:{action or 'event'}"
    headers = payload.get("headers") if isinstance(payload.get("headers"), dict) else {}
    for header in ("x-github-delivery", "X-GitHub-Delivery", "delivery_id"):
        if headers.get(header):
            return str(headers[header])
    for key in ("delivery_id", "event_id", "id"):
        if payload.get(key) not in (None, ""):
            return str(payload[key])
    raw = json.dumps(payload, sort_keys=True, default=str)
    return "sha:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def claim_event(event_key_value: str, *, hermes_home: HomeLike = None) -> Dict[str, Any]:
    return store.claim_signal("event", event_key_value, hermes_home=hermes_home)


def tick_key(scheduled_at: str, *, hermes_home: HomeLike = None) -> str:
    slug = profile_slug(hermes_home)
    hour = (scheduled_at or "").strip()
    prefix = f"{slug}:"
    if hour.startswith(prefix):
        return hour
    return f"{prefix}{hour}"


def claim_tick(job_id: str, scheduled_at: str, *, hermes_home: HomeLike = None) -> Dict[str, Any]:
    del job_id  # job name is not part of the event-contract tick identity
    return store.claim_signal("tick", tick_key(scheduled_at, hermes_home=hermes_home), hermes_home=hermes_home)


def webhook_filter(payload: Any, *, hermes_home: HomeLike = None) -> Dict[str, Any]:
    key = event_key(payload)
    claim = claim_event(key, hermes_home=hermes_home)
    if claim.get("duplicate"):
        store.increment_metric("duplicate_events_ignored", hermes_home=hermes_home)
        return {"ignore": True, "reason": "duplicate_event", "event_key": key}
    ctx = observe_context(hermes_home)
    if isinstance(payload, dict):
        out = dict(payload)
    else:
        out = {"payload": payload}
    out["ignore"] = False
    out["event_key"] = key
    autonomy = {
        "prompt": format_observe_prompt(ctx),
        "event_key": key,
    }
    out["autonomy"] = autonomy
    out["_autonomy"] = autonomy
    return out


def begin_work(
    *,
    why: str,
    outcome: str,
    done_contract: str,
    idempotency_key: str,
    objective: Optional[str] = None,
    parent_id: Optional[str] = None,
    refs: Optional[Dict[str, Any]] = None,
    hermes_home: HomeLike = None,
) -> Dict[str, Any]:
    existing = store.get_work_by_key(idempotency_key, hermes_home)
    if existing:
        store.increment_metric("duplicate_work_prevented", hermes_home=hermes_home)
        return {"created": False, "duplicate": True, "work": existing}
    if store.count_open_work(hermes_home) >= store.MAX_OPEN_WORK:
        store.increment_metric("open_work_capped", hermes_home=hermes_home)
        return {"created": False, "blocked": True, "reason": "open_work_cap"}
    if parent_id:
        parent = store.get_work(parent_id, hermes_home)
        if parent is None or parent.get("state") not in store.OPEN_STATES:
            store.increment_metric("invalid_parent_blocked", hermes_home=hermes_home)
            return {"created": False, "blocked": True, "reason": "invalid_parent"}
    else:
        active = store.already_working(hermes_home)
        if active:
            store.increment_metric("already_working_blocked", hermes_home=hermes_home)
            return {
                "created": False,
                "blocked": True,
                "reason": "already_working",
                "work": active,
            }
    started = store.start_work(
        why=why,
        outcome=outcome,
        done_contract=done_contract,
        idempotency_key=idempotency_key,
        objective=objective,
        parent_id=parent_id,
        refs=refs,
        hermes_home=hermes_home,
    )
    if started.get("created"):
        store.increment_metric("work_started", hermes_home=hermes_home)
    return started


def mark_noop(hermes_home: HomeLike = None) -> None:
    store.set_meta("last_noop_at", _hermes_now().isoformat(), hermes_home)
    store.increment_metric("nothing_worth_doing", hermes_home=hermes_home)


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _cooldown_active(hermes_home: HomeLike = None) -> bool:
    seen = _parse_iso(store.get_meta("last_noop_at", hermes_home))
    if seen is None:
        return False
    return _hermes_now() < seen + timedelta(minutes=COOLDOWN_MINUTES)


def observe_context(hermes_home: HomeLike = None) -> Dict[str, Any]:
    slug = profile_slug(hermes_home)
    state = mission_catalog.load_state(hermes_home)
    store.prune_terminal(hermes_home)
    open_items = store.list_work(hermes_home, states=list(store.OPEN_STATES))
    return {
        "profile": slug,
        "display_name": mission_catalog.display_name(slug),
        "enabled": bool(state.get("enabled")),
        "domains": list(state.get("domains") or []),
        "mission": mission_catalog.read_mission(hermes_home),
        "open_work": open_items,
        "already_working": store.already_working(hermes_home),
        "cooldown_active": _cooldown_active(hermes_home),
        "metrics": store.get_metrics(hermes_home),
    }


def format_observe_prompt(ctx: Dict[str, Any]) -> str:
    lines = [
        f"PROFILE={ctx.get('profile')}",
        f"DISPLAY={ctx.get('display_name')}",
        f"COOLDOWN_ACTIVE={bool(ctx.get('cooldown_active'))}",
        "",
        "## Standing Mission",
        str(ctx.get("mission") or "").strip(),
        "",
        "## Initiative contract",
        INITIATIVE_CONTRACT,
        "",
        "## Ledger CLI",
        LEDGER_CLI,
        "",
        "## Open work",
    ]
    open_items = ctx.get("open_work") or []
    if not open_items:
        lines.append("(none)")
    else:
        for item in open_items:
            lines.append(
                f"- {item['id']} [{item['state']}] {item.get('objective') or item.get('outcome')}"
            )
    if ctx.get("cooldown_active"):
        lines.extend(
            [
                "",
                f"Nothing material changed recently. Default to {SILENT_TOKEN} "
                "unless a new event or open work requires resume.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                f"If nothing is worth doing, reply with exactly {SILENT_TOKEN}.",
            ]
        )
    return "\n".join(lines) + "\n"


def monitor_fingerprint(hermes_home: HomeLike = None) -> str:
    items = store.list_work(hermes_home, states=list(store.OPEN_STATES))
    parts = [f"{item['id']}:{item['state']}:{item['idempotency_key']}" for item in items]
    if any(item["state"] in store.ACTIVE_STATES for item in items):
        bucket = int(_hermes_now().timestamp()) // RESUME_BUCKET_SECONDS
        parts.append(f"resume:{bucket}")
    if _cooldown_active(hermes_home) and not items:
        parts.append("cooldown")
    mission = mission_catalog.read_mission(hermes_home)
    parts.append("mission:" + hashlib.sha256(mission.encode("utf-8")).hexdigest()[:12])
    return "\n".join(parts) + "\n"


def owner_line(profile: str, kind: str, detail: str) -> str:
    name = mission_catalog.display_name(profile)
    detail = re.sub(r"\s+", " ", detail or "").strip()
    if kind in {"investigating", "working"}:
        return f"{name} is investigating {detail}"
    if kind == "completed":
        return f"{name} resolved {detail}"
    if kind == "needs_owner":
        return f"{name} needs you — {detail}"
    return f"{name}: {detail}"


def is_silent(text: str) -> bool:
    return (text or "").strip() == SILENT_TOKEN


def _goal_hash(goal: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", (goal or "").strip().lower()).encode("utf-8")).hexdigest()[:16]


def _evidence_hash(evidence: str) -> str:
    return hashlib.sha256((evidence or "").strip().encode("utf-8")).hexdigest()[:16]


def delegate(
    *,
    target: str,
    goal: str,
    context: str = "",
    deliverable: str = "",
    scope: str = "",
    evidence: str = "",
    work_id: str = "",
    hermes_home: HomeLike = None,
    send: bool = True,
) -> Dict[str, Any]:
    slug = profile_slug(hermes_home)
    to_agent = (target or "").strip().lstrip("@").lower()
    if not to_agent:
        return {"ok": False, "error": "missing_target"}
    if to_agent == slug:
        return {"ok": False, "error": "self_delegate"}
    if slug == "hamad" or to_agent == "hamad":
        return {"ok": False, "error": "isolated_profile"}
    if work_id:
        work = store.get_work(work_id, hermes_home)
        if work is None:
            return {"ok": False, "error": "unknown_work"}
    guard = store.record_collab(
        work_id=work_id or "unbound",
        from_agent=slug,
        to_agent=to_agent,
        evidence_hash=_evidence_hash(evidence),
        goal_hash=_goal_hash(goal),
        hermes_home=hermes_home,
    )
    if not guard.get("allowed"):
        return {
            "ok": False,
            "allowed": False,
            "error": guard.get("reason") or "rejected",
            "reason": guard.get("reason") or "rejected",
            "goal_hash": guard.get("goal_hash"),
        }
    if work_id:
        store.update_work(
            work_id,
            state="waiting",
            waiting_reason=f"awaiting {to_agent}: {goal}",
            hermes_home=hermes_home,
        )
    message = (
        f"Autonomous collaboration request from {mission_catalog.display_name(slug)}.\n"
        f"Origin work: {work_id or 'none'}\n"
        f"Goal: {goal.strip()}\n"
        f"Requested deliverable: {deliverable.strip()}\n"
        f"Bounded scope: {scope.strip() or 'do only this ask'}\n"
        f"Evidence needed: {evidence.strip()}\n"
        f"If you have nothing useful, reply {SILENT_TOKEN}.\n"
    )
    if context.strip():
        message += f"\nContext:\n{context.strip()}\n"
    delivery: Dict[str, Any] = {"sent": False, "dry_run": not send}
    if send:
        delivery = _send_bot_chat(to_agent, message)
        if not delivery.get("sent"):
            store.undo_collab(
                work_id=work_id or "unbound",
                from_agent=slug,
                to_agent=to_agent,
                goal_hash=str(guard.get("goal_hash") or ""),
                hermes_home=hermes_home,
            )
            if work_id:
                store.update_work(
                    work_id,
                    state="working",
                    waiting_reason="",
                    hermes_home=hermes_home,
                )
            return {
                "ok": False,
                "allowed": True,
                "error": "delivery_failed",
                "reason": "delivery_failed",
                "retryable": True,
                "to": to_agent,
                "work_id": work_id,
                "message": message,
                "delivery": delivery,
                "goal_hash": guard.get("goal_hash"),
            }
    return {
        "ok": True,
        "allowed": True,
        "to": to_agent,
        "work_id": work_id,
        "message": message,
        "delivery": delivery,
        "goal_hash": guard.get("goal_hash"),
        "reason": None,
    }


def _send_bot_chat(target: str, message: str) -> Dict[str, Any]:
    handle, path = tempfile.mkstemp(prefix="hermes-autonomy-dm-", suffix=".txt")
    os.close(handle)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(message)
        env = os.environ.copy()
        env.pop("HERMES_HOME", None)
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "hermes_cli.main",
                "-p",
                target,
                "chat",
                "--in",
                "~",
                "-c",
                "Bot Chat",
                "--create-if-missing",
                "-Q",
                "--query-file",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=600,
            env=env,
        )
        return {
            "sent": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": (proc.stdout or "")[-2000:],
            "stderr": (proc.stderr or "")[-2000:],
        }
    except Exception as exc:
        return {"sent": False, "error": str(exc)}
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def observation_output(hermes_home: HomeLike = None) -> str:
    """Cron observe script body: duplicate ticks and idle cooldown stay silent."""
    hour = _hermes_now().strftime("%Y-%m-%dT%H")
    claim = claim_tick(OBSERVE_JOB_NAME, hour, hermes_home=hermes_home)
    if claim.get("duplicate"):
        return CRON_SKIP
    ctx = observe_context(hermes_home)
    if ctx.get("cooldown_active") and not ctx.get("open_work"):
        return CRON_SKIP
    return format_observe_prompt(ctx)


def emit_observe_context() -> None:
    print(observation_output(), end="")


def emit_monitor_fingerprint() -> None:
    print(monitor_fingerprint(), end="")


def emit_webhook_filter() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {"raw": raw}
    result = webhook_filter(payload)
    if result.get("ignore"):
        print(SILENT_TOKEN)
        return
    print(json.dumps(result, ensure_ascii=False))


def start_work(**kwargs: Any) -> Dict[str, Any]:
    return begin_work(**kwargs)


def bind_tracking(
    work_id: str,
    *,
    jira: Optional[str] = None,
    hermes_home: HomeLike = None,
) -> Dict[str, Any]:
    refs: Dict[str, Any] = {}
    if jira:
        refs["jira"] = str(jira).strip()
    if not refs:
        item = store.get_work(work_id, hermes_home)
        return {"ok": bool(item), "work": item, "error": None if item else "not found"}
    item = store.update_work(work_id, hermes_home=hermes_home, refs=refs)
    if item is None:
        return {"ok": False, "error": "not found"}
    return {"ok": True, "work": item}


def request_owner(
    work_id: str,
    reason: str,
    *,
    hermes_home: HomeLike = None,
) -> Dict[str, Any]:
    """Mark needs_owner and project one line into canonical Bot Chat."""
    from agent.autonomy.owner_projection import append_bot_chat_notice

    item = store.update_work(
        work_id,
        hermes_home=hermes_home,
        state="needs_owner",
        waiting_reason=reason,
    )
    if item is None:
        return {"ok": False, "error": "not found"}
    slug = profile_slug(hermes_home)
    line = owner_line(slug, "needs_owner", reason)
    claim = store.claim_signal(
        "owner_notice",
        work_id,
        work_id=work_id,
        hermes_home=hermes_home,
    )
    if claim.get("duplicate"):
        store.increment_metric("owner_notice_deduped", hermes_home=hermes_home)
        return {
            "ok": True,
            "work": item,
            "line": line,
            "projection": {"ok": True, "duplicate": True, "projected": False},
        }
    projection = append_bot_chat_notice(line, hermes_home=hermes_home, profile=slug)
    if not projection.get("ok"):
        store.release_claim("owner_notice", work_id, hermes_home=hermes_home)
        store.increment_metric("owner_notice_failed", hermes_home=hermes_home)
        return {"ok": True, "work": item, "line": line, "projection": projection}
    store.increment_metric("owner_notices_projected", hermes_home=hermes_home)
    return {
        "ok": True,
        "work": item,
        "line": line,
        "projection": {**projection, "duplicate": False, "projected": True},
    }


def complete_work(work_id: str, result: str, hermes_home: HomeLike = None) -> Dict[str, Any]:
    item = store.complete_work(work_id, result, hermes_home=hermes_home)
    if item is None:
        return {"ok": False, "error": "not found"}
    store.prune_terminal(hermes_home)
    return item


def drop_work(work_id: str, reason: str, hermes_home: HomeLike = None) -> Dict[str, Any]:
    item = store.drop_work(work_id, reason, hermes_home=hermes_home)
    if item is None:
        return {"ok": False, "error": "not found"}
    return item


def record_delegate(
    *,
    work_id: str,
    to_profile: str,
    goal: str,
    deliverable: str,
    evidence: str,
    context: str = "",
    send: bool = False,
    hermes_home: HomeLike = None,
) -> Dict[str, Any]:
    return delegate(
        target=to_profile,
        goal=goal,
        deliverable=deliverable,
        evidence=evidence,
        context=context,
        work_id=work_id,
        send=send,
        hermes_home=hermes_home,
    )


def enable_profile(
    profile: Optional[str] = None,
    schedule: Optional[str] = None,
    no_soul: bool = False,
    hermes_home: HomeLike = None,
) -> Dict[str, Any]:
    from agent.autonomy.cron_setup import enable_observation

    result = enable_observation(hermes_home, schedule=schedule, no_soul=no_soul)
    mission = result.get("mission") or {}
    return {
        "profile": result.get("profile") or profile,
        "job": result.get("job"),
        "state": result.get("state"),
        "mission": mission.get("mission") if isinstance(mission, dict) else mission,
        "soul_updated": bool(mission.get("soul_changed")) if isinstance(mission, dict) else False,
        "no_soul": no_soul,
    }


def disable_profile(hermes_home: HomeLike = None) -> Dict[str, Any]:
    from agent.autonomy.cron_setup import disable_observation

    return disable_observation(hermes_home)


def status_snapshot(hermes_home: HomeLike = None) -> Dict[str, Any]:
    return observe_context(hermes_home)
