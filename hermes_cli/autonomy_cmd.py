"""``hermes autonomy`` handlers."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from agent.autonomy import SILENT_TOKEN, kernel, store
from agent.autonomy.cron_setup import disable_observation, enable_observation
from agent.autonomy.missions import install_mission, load_state, read_mission, write_mission_file
from agent.autonomy.paths import profile_slug


def _print_json(payload: Any, exit_code: int = 0) -> int:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return exit_code


def _cmd_status(_args: argparse.Namespace) -> int:
    ctx = kernel.observe_context()
    state = load_state()
    return _print_json(
        {
            "profile": ctx["profile"],
            "display_name": ctx["display_name"],
            "enabled": bool(state.get("enabled")),
            "initiative": bool(state.get("initiative")),
            "domains": ctx["domains"],
            "open_work": ctx["open_work"],
            "cooldown_active": ctx["cooldown_active"],
            "metrics": ctx["metrics"],
        }
    )


def _cmd_mission_show(_args: argparse.Namespace) -> int:
    print(read_mission())
    return 0


def _cmd_mission_set(args: argparse.Namespace) -> int:
    text = args.text
    if getattr(args, "file", None):
        text = Path(args.file).read_text(encoding="utf-8")
    if not text or not str(text).strip():
        print("mission text is required", file=sys.stderr)
        return 2
    write_mission_file(str(text))
    return _print_json(install_mission(mission=str(text)))


def _cmd_enable(args: argparse.Namespace) -> int:
    return _print_json(
        enable_observation(
            schedule=getattr(args, "schedule", None),
            no_soul=bool(getattr(args, "no_soul", False)),
        )
    )


def _cmd_disable(_args: argparse.Namespace) -> int:
    return _print_json(disable_observation())


def _cmd_observe(_args: argparse.Namespace) -> int:
    print(kernel.format_observe_prompt(kernel.observe_context()), end="")
    return 0


def _cmd_monitor(_args: argparse.Namespace) -> int:
    print(kernel.monitor_fingerprint(), end="")
    return 0


def _cmd_noop(_args: argparse.Namespace) -> int:
    kernel.mark_noop()
    print(SILENT_TOKEN)
    return 0


def _cmd_work_start(args: argparse.Namespace) -> int:
    refs = {}
    jira = getattr(args, "jira", None)
    if jira:
        refs["jira"] = str(jira).strip()
    result = kernel.begin_work(
        why=args.why,
        outcome=args.outcome,
        done_contract=getattr(args, "done_contract", None) or getattr(args, "done", None),
        idempotency_key=getattr(args, "idempotency_key", None) or getattr(args, "key", None),
        objective=getattr(args, "objective", None),
        parent_id=getattr(args, "parent", None),
        refs=refs or None,
    )
    return _print_json(result)


def _cmd_work_get(args: argparse.Namespace) -> int:
    work_id = getattr(args, "work_id", None) or getattr(args, "id", None)
    item = store.get_work(work_id)
    if item is None:
        return _print_json({"ok": False, "error": "not found"}, 2)
    return _print_json({"ok": True, "work": item})


def _cmd_work_list(args: argparse.Namespace) -> int:
    states = [args.state] if getattr(args, "state", None) else None
    return _print_json({"ok": True, "work": store.list_work(states=states)})


def _cmd_work_update(args: argparse.Namespace) -> int:
    work_id = getattr(args, "work_id", None) or getattr(args, "id", None)
    jira = getattr(args, "jira", None)
    if jira:
        bound = kernel.bind_tracking(work_id, jira=jira)
        if not bound.get("ok"):
            return _print_json(bound, 2)
    state = getattr(args, "state", None)
    reason = getattr(args, "waiting_reason", None)
    if state == "needs_owner":
        result = kernel.request_owner(work_id, reason or "owner decision required")
        if not result.get("ok"):
            return _print_json(result, 2)
        return _print_json(result)
    item = store.update_work(
        work_id,
        state=state,
        waiting_reason=reason,
        objective=getattr(args, "objective", None),
    )
    if item is None:
        return _print_json({"ok": False, "error": "not found"}, 2)
    return _print_json({"ok": True, "work": item})


def _cmd_work_complete(args: argparse.Namespace) -> int:
    work_id = getattr(args, "work_id", None) or getattr(args, "id", None)
    item = store.complete_work(
        work_id,
        args.result,
        material=not getattr(args, "quiet", False),
    )
    if item is None:
        return _print_json({"ok": False, "error": "not found"}, 2)
    return _print_json(item)


def _cmd_work_drop(args: argparse.Namespace) -> int:
    work_id = getattr(args, "work_id", None) or getattr(args, "id", None)
    item = store.drop_work(work_id, args.reason)
    if item is None:
        return _print_json({"ok": False, "error": "not found"}, 2)
    return _print_json(item)


def _cmd_event_claim(args: argparse.Namespace) -> int:
    key = getattr(args, "key", None)
    if getattr(args, "stdin", False):
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            payload = {"raw": raw}
        key = kernel.event_key(payload)
    return _print_json(kernel.claim_event(key or ""))


def _cmd_tick_claim(args: argparse.Namespace) -> int:
    job_id = getattr(args, "job_id", None) or "autonomy-observe"
    scheduled_at = getattr(args, "scheduled_at", None) or getattr(args, "key", None)
    return _print_json(kernel.claim_tick(job_id, scheduled_at or ""))


def _cmd_delegate(args: argparse.Namespace) -> int:
    result = kernel.delegate(
        target=getattr(args, "to", None) or getattr(args, "target", None),
        goal=args.goal,
        context=getattr(args, "context", "") or "",
        deliverable=getattr(args, "deliverable", "") or "",
        scope=getattr(args, "scope", "") or "",
        evidence=getattr(args, "evidence", "") or "",
        work_id=getattr(args, "work", None) or getattr(args, "work_id", "") or "",
        send=not getattr(args, "dry_run", False),
    )
    return _print_json(result, 0 if result.get("ok") else 2)


def _cmd_metrics(_args: argparse.Namespace) -> int:
    return _print_json({"metrics": store.get_metrics()})


ISOLATED_ROLLOUT_SLUGS = frozenset({"hamad"})


def select_rollout_slugs(
    *,
    pilot: bool = True,
    profile: str | None = None,
    all_profiles: bool = False,
) -> list:
    """Default is the four-agent pilot. Hamad is excluded unless explicitly named."""
    from agent.autonomy.missions import PILOT_PROFILES, all_owner_facing_slugs

    if profile:
        return [str(profile).strip().lower()]
    if all_profiles:
        return [slug for slug in all_owner_facing_slugs() if slug not in ISOLATED_ROLLOUT_SLUGS]
    return list(PILOT_PROFILES)


def _cmd_rollout(args: argparse.Namespace) -> int:
    import subprocess

    slugs = select_rollout_slugs(
        pilot=not bool(getattr(args, "all_profiles", False)),
        profile=getattr(args, "profile", None),
        all_profiles=bool(getattr(args, "all_profiles", False)),
    )
    results = []
    for slug in slugs:
        cmd = [sys.executable, "-m", "hermes_cli.main", "-p", slug, "autonomy", "enable"]
        if getattr(args, "schedule", None):
            cmd.extend(["--schedule", args.schedule])
        if getattr(args, "no_soul", False):
            cmd.append("--no-soul")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        entry = {"profile": slug, "ok": proc.returncode == 0, "returncode": proc.returncode}
        if proc.stdout:
            try:
                entry["result"] = json.loads(proc.stdout)
            except json.JSONDecodeError:
                entry["stdout"] = proc.stdout[-1500:]
        if proc.returncode != 0:
            entry["stderr"] = (proc.stderr or "")[-1500:]
        results.append(entry)
    failed = sum(1 for row in results if not row["ok"])
    return _print_json({"ok": failed == 0, "results": results}, 0 if failed == 0 else 1)


def cmd_autonomy(args: argparse.Namespace) -> None:
    action = getattr(args, "autonomy_command", None)
    handlers = {
        "status": _cmd_status,
        "mission-show": _cmd_mission_show,
        "mission-set": _cmd_mission_set,
        "enable": _cmd_enable,
        "disable": _cmd_disable,
        "observe-context": _cmd_observe,
        "monitor-fingerprint": _cmd_monitor,
        "noop": _cmd_noop,
        "work-start": _cmd_work_start,
        "work-get": _cmd_work_get,
        "work-list": _cmd_work_list,
        "work-update": _cmd_work_update,
        "work-complete": _cmd_work_complete,
        "work-drop": _cmd_work_drop,
        "event-claim": _cmd_event_claim,
        "tick-claim": _cmd_tick_claim,
        "delegate": _cmd_delegate,
        "metrics": _cmd_metrics,
        "rollout": _cmd_rollout,
    }
    if not action or action not in handlers:
        print("usage: hermes autonomy <command>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(handlers[action](args))
