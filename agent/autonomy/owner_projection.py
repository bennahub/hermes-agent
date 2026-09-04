"""Project a needs-owner request into the canonical Bot Chat session.

This writes the existing Bot Chat transcript and unread watermark. It does
not take the live-owner lock, does not spawn ``hermes chat``, and does not
create a second inbox.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from typing import Any, Dict, Iterable, Optional, Set

from agent.autonomy.paths import HomeLike, resolve_home

BOT_CHAT_TITLE = "Bot Chat"
_NOTICE_KIND = "autonomy_owner_notice"


def state_db_path(hermes_home: HomeLike = None):
    return resolve_home(hermes_home) / "state.db"


def _columns(conn: sqlite3.Connection, table: str) -> Set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _has(columns: Iterable[str], name: str) -> bool:
    return name in set(columns)


def _connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _find_bot_chat(conn: sqlite3.Connection) -> Optional[str]:
    if "title" not in _columns(conn, "sessions"):
        return None
    row = conn.execute(
        "SELECT id FROM sessions WHERE title=? ORDER BY started_at DESC",
        (BOT_CHAT_TITLE,),
    ).fetchone()
    return None if row is None else str(row["id"])


def _ensure_bot_chat(conn: sqlite3.Connection, slug: str) -> str:
    existing = _find_bot_chat(conn)
    if existing:
        return existing
    now = time.time()
    session_id = time.strftime("%Y%m%d_%H%M%S", time.gmtime(now)) + "_" + uuid.uuid4().hex[:6]
    cols = _columns(conn, "sessions")
    fields = {"id": session_id, "source": "cli", "started_at": now}
    if _has(cols, "title"):
        fields["title"] = BOT_CHAT_TITLE
    if _has(cols, "title_source"):
        fields["title_source"] = "user"
    if _has(cols, "hidden"):
        fields["hidden"] = 1
    if _has(cols, "profile_name"):
        fields["profile_name"] = slug
    if _has(cols, "last_activity_at"):
        fields["last_activity_at"] = now
    if _has(cols, "last_read_at"):
        fields["last_read_at"] = 0.0
    if _has(cols, "message_count"):
        fields["message_count"] = 0
    names = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(
        f"INSERT INTO sessions ({names}) VALUES ({placeholders})",
        list(fields.values()),
    )
    return session_id


def _last_role(conn: sqlite3.Connection, session_id: str) -> Optional[str]:
    cols = _columns(conn, "messages")
    active_clause = "AND active=1" if _has(cols, "active") else ""
    row = conn.execute(
        f"SELECT role FROM messages WHERE session_id=? {active_clause} "
        "ORDER BY timestamp DESC, id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return None if row is None else str(row["role"])


def _insert_message(
    conn: sqlite3.Connection,
    session_id: str,
    role: str,
    content: str,
    now: float,
) -> None:
    cols = _columns(conn, "messages")
    fields: Dict[str, Any] = {
        "session_id": session_id,
        "role": role,
        "content": content,
        "timestamp": now,
    }
    if _has(cols, "active"):
        fields["active"] = 1
    if _has(cols, "observed"):
        fields["observed"] = 0
    if _has(cols, "_compressed_summary"):
        fields["_compressed_summary"] = 0
    if _has(cols, "compacted"):
        fields["compacted"] = 0
    if _has(cols, "display_kind"):
        fields["display_kind"] = _NOTICE_KIND
    usable = {k: v for k, v in fields.items() if k in cols}
    if "session_id" not in usable:
        raise RuntimeError("state.db messages table is missing session_id")
    names = ", ".join(usable)
    placeholders = ", ".join("?" for _ in usable)
    conn.execute(
        f"INSERT INTO messages ({names}) VALUES ({placeholders})",
        list(usable.values()),
    )


def _mark_unread(conn: sqlite3.Connection, session_id: str, now: float, added: int) -> None:
    cols = _columns(conn, "sessions")
    assignments = []
    values: list[Any] = []
    if _has(cols, "last_read_at"):
        assignments.append("last_read_at=?")
        values.append(0.0)
    if _has(cols, "last_activity_at"):
        assignments.append("last_activity_at=?")
        values.append(now)
    if _has(cols, "message_count"):
        assignments.append("message_count=COALESCE(message_count,0)+?")
        values.append(added)
    if not assignments:
        return
    values.append(session_id)
    conn.execute(
        f"UPDATE sessions SET {', '.join(assignments)} WHERE id=?",
        values,
    )


def append_bot_chat_notice(
    line: str,
    *,
    hermes_home: HomeLike = None,
    profile: str = "",
) -> Dict[str, Any]:
    """Append one owner-visible request to the canonical Bot Chat.

    Does not become a live session owner. Replay callers must claim first.
    """
    home = resolve_home(hermes_home)
    path = state_db_path(home)
    if not path.is_file():
        return {"ok": False, "error": "state_db_missing", "path": str(path)}
    slug = profile or home.name
    now = time.time()
    user_line = (
        "[Autonomy notice — not the owner. A bounded request needs you "
        "in this canonical Bot Chat.]"
    )
    conn = _connect(path)
    try:
        session_id = _ensure_bot_chat(conn, slug)
        last = _last_role(conn, session_id)
        added = 0
        if last != "user":
            _insert_message(conn, session_id, "user", user_line, now)
            added += 1
            now += 0.001
        _insert_message(conn, session_id, "assistant", line, now)
        added += 1
        _mark_unread(conn, session_id, now, added)
        conn.commit()
        return {
            "ok": True,
            "session_id": session_id,
            "title": BOT_CHAT_TITLE,
            "path": str(path),
            "messages_added": added,
            "live_owner_taken": False,
        }
    except sqlite3.Error as exc:
        conn.rollback()
        return {"ok": False, "error": str(exc), "path": str(path)}
    finally:
        conn.close()


def session_is_unread(hermes_home: HomeLike = None, session_id: str = "") -> bool:
    path = state_db_path(hermes_home)
    if not path.is_file() or not session_id:
        return False
    conn = _connect(path)
    try:
        cols = _columns(conn, "sessions")
        if "last_read_at" not in cols:
            return False
        row = conn.execute(
            "SELECT last_read_at, last_activity_at, started_at FROM sessions WHERE id=?",
            (session_id,),
        ).fetchone()
        if row is None:
            return False
        last_read = row["last_read_at"]
        if last_read is None:
            return False
        last_active = row["last_activity_at"] if "last_activity_at" in row.keys() else None
        if last_active is None:
            last_active = row["started_at"]
        return float(last_active or 0) > float(last_read)
    finally:
        conn.close()
