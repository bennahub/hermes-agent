"""Central cleanup for background jobs that have no live owner or purpose."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_KEEP_NAME_MARKERS = (
    "receipts-scan",
    "raise-file",
    "weekly-vault",
    "bennahub",
    "mraia",
    "hermes-news",
)


def should_disable_job(job: dict[str, Any] | None) -> bool:
    """True when a scheduled job is shared leftover noise, not an owner duty."""
    if not isinstance(job, dict):
        return False
    name = str(job.get("name") or job.get("label") or "").strip()
    lower = name.lower()
    if any(marker in lower for marker in _KEEP_NAME_MARKERS):
        return False
    if name == "autonomy-observe" or lower.startswith("autonomy-observe"):
        return True
    if name.startswith("owner-continuation:") and job.get("enabled", True):
        return True
    return False


def _stale_internal_resume(refs: dict[str, Any], reason: str) -> bool:
    """True when Needs you is just a dead timer or finished background process."""
    resume = refs.get("resume") if isinstance(refs, dict) else None
    kind = str((resume or {}).get("kind") or "")
    if kind == "until" or reason.startswith("until:"):
        raw = str((resume or {}).get("deadline") or (resume or {}).get("at") or "")
        if not raw and reason.startswith("until:"):
            raw = reason.split(":", 1)[1].strip()
        try:
            deadline = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return bool(raw)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return deadline <= datetime.now(timezone.utc)
    if kind == "external" or reason.startswith("external: process:"):
        event = refs.get("resume_event") if isinstance(refs, dict) else None
        payload = (event or {}).get("payload") if isinstance(event, dict) else None
        if isinstance(payload, dict) and str(payload.get("reason") or "").lower() == "exited":
            return True
        deadline = str((resume or {}).get("deadline") or "")
        try:
            when = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
        except ValueError:
            return bool(event)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when <= datetime.now(timezone.utc)
    return False


def disable_stale_jobs(jobs: list[dict[str, Any]]) -> list[str]:
    """Return ids of jobs that must be paused. Does not delete owner duties."""
    disabled = []
    for job in jobs:
        if not should_disable_job(job):
            continue
        if not job.get("enabled", True):
            continue
        job_id = str(job.get("id") or job.get("job_id") or "")
        if job_id:
            disabled.append(job_id)
    return disabled


def sweep_runtime_noise(hermes_home: str | Path | None = None) -> dict[str, list[str]]:
    """Pause leftover observe/continuation jobs and settle false Needs you rows."""
    from hermes_constants import get_hermes_home

    home = Path(hermes_home or get_hermes_home())
    paused: list[str] = []
    settled: list[str] = []
    try:
        from cron.jobs import load_jobs, pause_job, use_cron_store

        with use_cron_store(home):
            for job_id in disable_stale_jobs(load_jobs()):
                if pause_job(job_id, reason="stale leftover without a live owner mission"):
                    paused.append(job_id)
    except Exception:
        logger.debug("stale job sweep skipped for %s", home, exc_info=True)
    try:
        from agent.autonomy import store
        from agent.autonomy.needs_you import is_false_needs_you

        for work in store.list_work(home, states=["needs_owner"]):
            refs = work.get("refs") or {}
            reason = str(work.get("waiting_reason") or "")
            kind = refs.get("outcome_kind")
            stale = is_false_needs_you(reason, outcome_kind=kind) or _stale_internal_resume(refs, reason)
            if not stale:
                continue
            try:
                updated = store.update_work(
                    work["id"],
                    state="failed",
                    hermes_home=home,
                    waiting_reason=reason,
                    completion_result=reason or "internal failure",
                    refs={"outcome_kind": "internal_failure"},
                )
            except Exception:
                logger.debug("false Needs you settle skipped for %s", work.get("id"), exc_info=True)
                continue
            if updated is not None and updated.get("state") == "failed":
                settled.append(work["id"])
    except Exception:
        logger.debug("false Needs you sweep skipped for %s", home, exc_info=True)
    return {"paused": paused, "settled": settled}


def sweep_known_homes(profile_homes: list | None = None) -> dict[str, list[str]]:
    """Sweep the active home plus any multiplex profile homes."""
    from hermes_constants import get_hermes_home

    homes: list[Path] = []
    if profile_homes:
        for entry in profile_homes:
            if isinstance(entry, (tuple, list)) and len(entry) >= 2:
                homes.append(Path(entry[1]))
            else:
                homes.append(Path(entry))
    else:
        homes.append(Path(get_hermes_home()))
        try:
            from hermes_cli.profiles import profiles_to_serve

            for _name, home in profiles_to_serve(multiplex=True):
                homes.append(Path(home))
        except Exception:
            pass
    seen: set[str] = set()
    paused: list[str] = []
    settled: list[str] = []
    for home in homes:
        key = str(home.resolve()) if home.exists() else ""
        if not key or key in seen:
            continue
        seen.add(key)
        result = sweep_runtime_noise(home)
        paused.extend(result["paused"])
        settled.extend(result["settled"])
    return {"paused": paused, "settled": settled}
