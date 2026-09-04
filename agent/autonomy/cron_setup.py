"""Install per-profile observation cron jobs using native cron APIs."""

from __future__ import annotations

from typing import Any, Dict, Optional

from agent.autonomy import OBSERVE_JOB_NAME, PILOT_PROFILES
from agent.autonomy.missions import (
    install_mission,
    load_state,
    mission_for,
    mission_text,
    save_state,
    write_mission_file,
)
from agent.autonomy.paths import HomeLike, profile_slug, resolve_home, scripts_dir

OBSERVE_SCRIPT = "autonomy_observe_context.py"
MONITOR_SCRIPT = "autonomy_monitor.py"
WEBHOOK_SCRIPT = "autonomy_webhook_filter.py"

_SCRIPT_SOURCES = {
    OBSERVE_SCRIPT: '''#!/usr/bin/env python3
"""Inject Standing Mission + open work into a native cron observation turn."""
from agent.autonomy.kernel import observation_output

print(observation_output(), end="")
''',
    MONITOR_SCRIPT: '''#!/usr/bin/env python3
"""Cheap change fingerprint for native cron --monitor-script suppression."""
from agent.autonomy.kernel import monitor_fingerprint

print(monitor_fingerprint(), end="")
''',
    WEBHOOK_SCRIPT: '''#!/usr/bin/env python3
"""Native webhook script: ignore duplicate events, else pass payload through."""
import json
import sys

from agent.autonomy.kernel import webhook_filter

raw = sys.stdin.read()
try:
    payload = json.loads(raw) if raw.strip() else {}
except json.JSONDecodeError:
    payload = {"raw": raw}
result = webhook_filter(payload)
if result.get("ignore"):
    print("[SILENT]")
    sys.exit(0)
print(json.dumps(result, ensure_ascii=False))
''',
}

OBSERVE_PROMPT = (
    "This is an observation-driven initiative turn. The injected context is "
    "authoritative and overrides a stale «ابدأ» habit for ordinary "
    "self-started in-scope work. If tracking is required, create or bind "
    "the Jira/work identity yourself, then record it with "
    "`hermes autonomy work-start --jira <KEY>`. Follow the Standing Mission "
    "and the initiative contract. Record finite work only through "
    "`hermes autonomy` (work-start / work-complete / work-update / "
    "delegate / noop). Do not use todo. Owner `needs_owner` is only for a "
    "real consequence boundary; the CLI projects that request into Bot Chat. "
    "If nothing is worth doing, reply with exactly [SILENT]."
)


def stagger_schedule(slug: str) -> str:
    digest = sum(ord(ch) for ch in slug) if slug else 0
    minute = 7 + (digest % 50)
    return f"{minute} 8-22/3 * * *"


def install_scripts(hermes_home: HomeLike = None) -> None:
    root = scripts_dir(hermes_home)
    for name, source in _SCRIPT_SOURCES.items():
        (root / name).write_text(source, encoding="utf-8")


def _find_observe_job():
    from cron.jobs import list_jobs

    for job in list_jobs(include_disabled=True):
        if (job.get("name") or "") == OBSERVE_JOB_NAME:
            return job
    return None


def _safe_model_pins() -> Dict[str, Optional[str]]:
    """Follow the profile default unless that default is exhausted Codex."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
    except Exception:
        return {"model": None, "provider": None}
    model = cfg.get("model") if isinstance(cfg, dict) else {}
    if not isinstance(model, dict):
        return {"model": None, "provider": None}
    provider = str(model.get("provider") or "").strip() or None
    name = str(model.get("default") or "").strip() or None
    if (provider and "codex" in provider.lower()) or (name and "codex" in name.lower()):
        fallback = model.get("fallback") if isinstance(model.get("fallback"), dict) else {}
        fb_provider = str(fallback.get("provider") or "").strip() or None
        fb_name = str(fallback.get("model") or fallback.get("default") or "").strip() or None
        if fb_provider and "codex" not in fb_provider.lower():
            return {"model": fb_name, "provider": fb_provider}
        return {"model": None, "provider": "anthropic"}
    return {"model": None, "provider": None}


def _observe_job_fields(slug: str, expr: str) -> Dict[str, Any]:
    pins = _safe_model_pins()
    return {
        "prompt": OBSERVE_PROMPT,
        "schedule": expr,
        "script": OBSERVE_SCRIPT,
        "monitor_script": MONITOR_SCRIPT,
        "deliver": f"bot-chat:{slug}" if slug != "default" else "bot-chat",
        "reasoning_effort": "low",
        "enabled": True,
        "model": pins["model"],
        "provider": pins["provider"],
    }


def enable_observation(
    hermes_home: HomeLike = None,
    *,
    mission: Optional[str] = None,
    schedule: Optional[str] = None,
    no_soul: bool = False,
) -> Dict[str, Any]:
    home = resolve_home(hermes_home)
    slug = profile_slug(home)
    install_scripts(home)
    if no_soul:
        text = (mission or mission_text(slug)).strip()
        write_mission_file(text, home)
        spec = mission_for(slug)
        save_state({"domains": list(spec.get("domains") or ["coordination"])}, home)
        installed = {"profile": slug, "mission": text, "soul_changed": False}
    else:
        installed = install_mission(home, mission)
    expr = schedule or stagger_schedule(slug)

    from cron.jobs import create_job, resume_job, update_job, use_cron_store

    with use_cron_store(home):
        existing = _find_observe_job()
        fields = _observe_job_fields(slug, expr)
        if existing:
            updated = update_job(existing["id"], fields)
            if existing.get("state") == "paused" or not existing.get("enabled", True):
                resume_job(existing["id"])
            job = updated or existing
        else:
            job = create_job(
                prompt=fields["prompt"],
                schedule=fields["schedule"],
                name=OBSERVE_JOB_NAME,
                deliver=fields["deliver"],
                script=fields["script"],
                monitor_script=fields["monitor_script"],
                reasoning_effort=fields["reasoning_effort"],
                model=fields.get("model"),
                provider=fields.get("provider"),
            )
    state = save_state(
        {
            "enabled": True,
            "initiative": True,
            "observation_job_id": job.get("id"),
            "pilot": slug in PILOT_PROFILES,
        },
        home,
    )
    return {"profile": slug, "job": job, "state": state, "mission": installed}


def disable_observation(hermes_home: HomeLike = None) -> Dict[str, Any]:
    home = resolve_home(hermes_home)
    from cron.jobs import pause_job, use_cron_store

    state = load_state(home)
    job_id = state.get("observation_job_id")
    with use_cron_store(home):
        job = _find_observe_job()
        if job:
            job_id = job["id"]
            pause_job(job_id)
    state = save_state({"enabled": False, "initiative": False}, home)
    return {"profile": profile_slug(home), "job_id": job_id, "state": state}
