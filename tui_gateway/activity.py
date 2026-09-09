"""Bounded client-neutral activity projection of existing runtime and work state.

No prompt, result, task title or generated prose is returned. Ephemeral phase
lives in the existing session dictionary; durable work is read without creating
or migrating its profile-local database.
"""
from __future__ import annotations

from contextlib import closing
import sqlite3
import threading
import time
from pathlib import Path

_lock = threading.RLock()
_PHASE_PRIORITY = {"needs_owner": 100, "computer": 80, "searching": 70,
                   "fetching": 70, "acting": 60, "typing": 50, "thinking": 40}
_SEARCH = frozenset({"web_search", "search_files"})
_FETCH = frozenset({"web_extract", "read_file", "read_terminal", "read_preview", "read_window_below"})
_COMPUTER = frozenset({"computer_act", "computer_observe", "computer_use", "browser_exec",
    "browser_navigate", "browser_snapshot", "browser_click", "browser_type", "browser_scroll",
    "browser_back", "browser_press", "browser_get_images", "browser_vision", "browser_console",
    "browser_cdp", "browser_dialog"})


def tool_phase(name: str) -> str:
    if name in _COMPUTER:
        return "computer"
    if name in _SEARCH:
        return "searching"
    if name in _FETCH:
        return "fetching"
    return "acting"


def record(session: dict, event: str, payload: dict | None = None, *, now: float | None = None) -> bool:
    """Return whether the small activity projection changed, not every delta."""
    now = time.time() if now is None else now
    payload = payload or {}
    with _lock:
        from tui_gateway.interactive_progress import record as record_progress
        record_progress(session, event, payload)
        previous = session.get("_client_activity")
        data = dict(previous) if isinstance(previous, dict) else {"phase": "thinking", "tools": {}, "terminal": False}
        data["tools"] = dict(data.get("tools") or {})
        before = (data.get("phase"), data.get("terminal"), tuple(sorted(data["tools"].items())))
        if event == "message.start":
            data.update(phase="thinking", tools={}, terminal=False)
        elif event in ("message.complete", "error"):
            data.update(phase="idle", tools={}, terminal=True)
        elif event == "message.delta":
            # Presence of output is structural; its contents are never classified.
            if not isinstance(payload.get("text"), str) or not payload["text"]:
                return False
            if data.get("terminal"):
                return False
            data["phase"] = "typing"
        elif event in ("thinking.delta", "reasoning.delta"):
            if data.get("terminal"):
                return False
            data["phase"] = "thinking"
        elif event == "tool.start":
            tool_id, name = payload.get("tool_id"), payload.get("name")
            if not isinstance(tool_id, str) or not tool_id or not isinstance(name, str) or not name:
                return False
            if data.get("terminal"):
                return False
            data["tools"][tool_id] = tool_phase(name)
            data["phase"] = "thinking"
        elif event == "tool.complete":
            data["tools"].pop(payload.get("tool_id"), None)
            if not data.get("terminal"):
                data["phase"] = "thinking"
        else:
            return False
        data["updated_at"] = now
        session["_client_activity"] = data
        after = (data.get("phase"), data.get("terminal"), tuple(sorted(data["tools"].items())))
        return previous is None or before != after


def work_state(profile_home: str | Path | None) -> str | None:
    if not profile_home:
        return None
    # Canonical agent.autonomy.paths layout, without its mkdir side effect.
    path = Path(profile_home) / "autonomy" / "work.db"
    if not path.is_file():
        return None
    try:
        # Do not initialize the store or issue its write transaction merely to
        # show a header. Only canonical state values are read, never objectives.
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.05)) as conn:
            rows = conn.execute(
                "SELECT DISTINCT state FROM work WHERE state IN "
                "('needs_owner','working','investigating','waiting') AND "
                "(state != 'needs_owner' OR CASE WHEN json_valid(refs_json) THEN "
                "CASE WHEN json_type(refs_json, '$.owner_obligation') = 'object' THEN "
                "json_extract(refs_json, '$.owner_obligation.status') = 'unresolved' ELSE "
                "COALESCE(CAST(json_extract(refs_json, '$.continuation_unstarted.delivery') AS TEXT) = "
                "CAST(json_extract(refs_json, '$.owner_delivery.message_id') AS TEXT), 0) = 0 "
                "END ELSE 1 END)"
            ).fetchall()
        states = {row[0] for row in rows}
        if "needs_owner" in states:
            return "needs_owner"
        if states & {"working", "investigating"}:
            return "working"
        if "waiting" in states:
            return "waiting"
    except (sqlite3.Error, OSError):
        pass
    return None


def snapshot(session: dict, runtime_status: str, *, profile_home=None,
             include_work: bool = False, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    with _lock:
        data = session.get("_client_activity") or {}
        tools = list((data.get("tools") or {}).values())
        if runtime_status == "waiting":
            # _session_live_status uses waiting only for pending owner input.
            phase = "needs_owner"
        elif runtime_status in ("working", "starting") and not data.get("terminal") and (
            runtime_status != "starting" or session.get("running") or bool(data)
        ):
            # Building an agent for an opened chat is not a model turn. Only
            # an admitted running turn or actual callback can make it thinking.
            phase = max(tools, key=lambda p: _PHASE_PRIORITY.get(p, 0)) if tools else data.get("phase", "thinking")
            if phase == "typing" and now - data.get("updated_at", 0) > 15:
                phase = "thinking"
        else:
            phase = "idle"
        result = {"version": 1, "phase": phase, "observed_at": now}
    if include_work:
        result["work_state"] = work_state(profile_home)
    return result
