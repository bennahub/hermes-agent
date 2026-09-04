"""Profile-local durable autonomy store.

Follows the cron notepad pattern: one SQLite file under the active
``HERMES_HOME``, WAL, bounded rows. This is not a second task platform —
it is the missing restart-safe work + idempotency ledger that in-memory
``todo`` cannot provide and that kanban would over-model.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from hermes_time import now as _hermes_now

from agent.autonomy.paths import HomeLike, profile_slug, work_db_path

WORK_STATES = (
    "observed",
    "investigating",
    "actionable",
    "working",
    "waiting",
    "completed",
    "dropped",
    "needs_owner",
)
OPEN_STATES = (
    "observed",
    "investigating",
    "actionable",
    "working",
    "waiting",
    "needs_owner",
)
TERMINAL_STATES = ("completed", "dropped")
ACTIVE_STATES = ("working", "investigating", "actionable")
RESUMABLE_STATES = ("working", "waiting", "investigating", "actionable", "needs_owner")
MAX_OBJECTIVE_CHARS = 240
MAX_TEXT_CHARS = 4000
MAX_FANOUT = 3
MAX_OPEN_WORK = 3

_lock = threading.RLock()


def _now() -> str:
    return _hermes_now().isoformat()


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _connect(hermes_home: HomeLike = None) -> sqlite3.Connection:
    path = work_db_path(hermes_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(str(path), timeout=5)


def _initialize_schema(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS work (
            id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL,
            why TEXT NOT NULL,
            outcome TEXT NOT NULL,
            done_contract TEXT NOT NULL,
            objective TEXT NOT NULL,
            waiting_reason TEXT,
            completion_result TEXT,
            parent_id TEXT,
            refs_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS work_state_idx ON work(state, updated_at);
        CREATE TABLE IF NOT EXISTS claims (
            kind TEXT NOT NULL,
            claim_key TEXT NOT NULL,
            work_id TEXT,
            result TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (kind, claim_key)
        );
        CREATE TABLE IF NOT EXISTS collab (
            from_work_id TEXT NOT NULL,
            from_profile TEXT NOT NULL,
            to_profile TEXT NOT NULL,
            evidence_hash TEXT NOT NULL DEFAULT '',
            goal_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (from_work_id, to_profile, goal_hash)
        );
        CREATE TABLE IF NOT EXISTS metrics (
            key TEXT PRIMARY KEY,
            value INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            text TEXT NOT NULL,
            work_id TEXT,
            created_at TEXT NOT NULL,
            delivered INTEGER NOT NULL DEFAULT 0
        );
        """
    )


@contextmanager
def _transaction(hermes_home: HomeLike = None) -> Iterator[sqlite3.Connection]:
    with _lock:
        conn = _connect(hermes_home)
        try:
            _initialize_schema(conn)
            with conn:
                # Lock before read/decide/write, including separate CLI
                # processes. A process-local RLock alone cannot fence them.
                conn.execute("BEGIN IMMEDIATE")
                yield conn
        finally:
            conn.close()


transaction = _transaction


def _row_to_work(row: sqlite3.Row) -> Dict[str, Any]:
    refs: Dict[str, Any] = {}
    raw = row["refs_json"] if "refs_json" in row.keys() else "{}"
    try:
        parsed = json.loads(raw or "{}")
        if isinstance(parsed, dict):
            refs = parsed
    except json.JSONDecodeError:
        refs = {}
    return {
        "id": row["id"],
        "idempotency_key": row["idempotency_key"],
        "state": row["state"],
        "why": row["why"],
        "outcome": row["outcome"],
        "done_contract": row["done_contract"],
        "objective": row["objective"],
        "waiting_reason": row["waiting_reason"],
        "completion_result": row["completion_result"],
        "parent_id": row["parent_id"],
        "refs": refs,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_work_by_id(work_id: str, hermes_home: HomeLike = None) -> Optional[Dict[str, Any]]:
    return get_work(work_id, hermes_home)


def count_open_work(hermes_home: HomeLike = None) -> int:
    return len(list_work(hermes_home, states=list(OPEN_STATES)))


def count_collab(from_work_id: str, hermes_home: HomeLike = None) -> int:
    with _transaction(hermes_home) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM collab WHERE from_work_id=?",
            (from_work_id,),
        ).fetchone()
    return int(row[0])


def get_work(work_id: str, hermes_home: HomeLike = None) -> Optional[Dict[str, Any]]:
    if not work_id:
        return None
    with _transaction(hermes_home) as conn:
        row = conn.execute("SELECT * FROM work WHERE id=?", (work_id,)).fetchone()
    return None if row is None else _row_to_work(row)


def get_work_by_key(idempotency_key: str, hermes_home: HomeLike = None) -> Optional[Dict[str, Any]]:
    if not idempotency_key:
        return None
    with _transaction(hermes_home) as conn:
        row = conn.execute(
            "SELECT * FROM work WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
    return None if row is None else _row_to_work(row)


def list_work(hermes_home: HomeLike = None, *, states: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    with _transaction(hermes_home) as conn:
        if states:
            placeholders = ",".join("?" * len(states))
            rows = conn.execute(
                f"SELECT * FROM work WHERE state IN ({placeholders}) "
                "ORDER BY updated_at DESC",
                tuple(states),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM work ORDER BY updated_at DESC").fetchall()
    return [_row_to_work(row) for row in rows]


def already_working(hermes_home: HomeLike = None) -> Optional[Dict[str, Any]]:
    open_items = list_work(hermes_home, states=list(ACTIVE_STATES))
    return open_items[0] if open_items else None


def start_work(
    *,
    why: str,
    outcome: str,
    done_contract: str,
    idempotency_key: str,
    hermes_home: HomeLike = None,
    objective: Optional[str] = None,
    state: Optional[str] = None,
    parent_id: Optional[str] = None,
    refs: Optional[Dict[str, Any]] = None,
    enforce_admission: bool = False,
) -> Dict[str, Any]:
    existing = get_work_by_key(idempotency_key, hermes_home)
    if existing is not None:
        return {"created": False, "duplicate": True, "work": existing}

    open_count = len(list_work(hermes_home, states=list(OPEN_STATES)))
    chosen = state or ("observed" if parent_id else "working")
    if chosen not in WORK_STATES:
        raise ValueError(f"invalid work state: {chosen}")
    if open_count >= MAX_OPEN_WORK and chosen in OPEN_STATES:
        chosen = "dropped"

    now = _now()
    item = {
        "id": "aw_" + uuid.uuid4().hex[:16],
        "idempotency_key": idempotency_key,
        "state": chosen,
        "why": _clip(why, MAX_TEXT_CHARS),
        "outcome": _clip(outcome, MAX_TEXT_CHARS),
        "done_contract": _clip(done_contract, MAX_TEXT_CHARS),
        "objective": _clip(objective or why or outcome, MAX_OBJECTIVE_CHARS),
        "waiting_reason": None,
        "completion_result": None,
        "parent_id": parent_id,
        "refs_json": json.dumps(refs or {}, ensure_ascii=False),
        "created_at": now,
        "updated_at": now,
    }
    try:
        with _transaction(hermes_home) as conn:
            existing = conn.execute("SELECT * FROM work WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if existing is not None:
                return {"created": False, "duplicate": True, "work": _row_to_work(existing)}
            placeholders = ",".join("?" for _ in OPEN_STATES)
            open_count = conn.execute(f"SELECT COUNT(*) FROM work WHERE state IN ({placeholders})", OPEN_STATES).fetchone()[0]
            if open_count >= MAX_OPEN_WORK and (state or "working") in OPEN_STATES:
                if enforce_admission:
                    return {"created": False, "blocked": True, "reason": "open_work_cap"}
                item["state"] = "dropped"
            if enforce_admission:
                if parent_id:
                    parent = conn.execute("SELECT state FROM work WHERE id=?", (parent_id,)).fetchone()
                    if parent is None or parent["state"] not in OPEN_STATES:
                        return {"created": False, "blocked": True, "reason": "invalid_parent"}
                else:
                    placeholders = ",".join("?" for _ in ACTIVE_STATES)
                    active = conn.execute(f"SELECT * FROM work WHERE state IN ({placeholders}) LIMIT 1", ACTIVE_STATES).fetchone()
                    if active is not None:
                        return {"created": False, "blocked": True, "reason": "already_working", "work": _row_to_work(active)}
            conn.execute(
                """INSERT INTO work (
                     id, idempotency_key, state, why, outcome, done_contract,
                     objective, waiting_reason, completion_result, parent_id,
                     refs_json, created_at, updated_at
                   ) VALUES (
                     :id, :idempotency_key, :state, :why, :outcome, :done_contract,
                     :objective, :waiting_reason, :completion_result, :parent_id,
                     :refs_json, :created_at, :updated_at
                   )""",
                item,
            )
            row = conn.execute("SELECT * FROM work WHERE id=?", (item["id"],)).fetchone()
    except sqlite3.IntegrityError:
        existing = get_work_by_key(idempotency_key, hermes_home)
        if existing is not None:
            return {"created": False, "duplicate": True, "work": existing}
        raise
    return {"created": True, "work": _row_to_work(row)}


def update_work(
    work_id: str,
    *,
    hermes_home: HomeLike = None,
    state: Optional[str] = None,
    waiting_reason: Optional[str] = None,
    objective: Optional[str] = None,
    completion_result: Optional[str] = None,
    refs: Optional[Dict[str, Any]] = None,
    **_ignored: Any,
) -> Optional[Dict[str, Any]]:
    current = get_work(work_id, hermes_home)
    if current is None:
        return None
    fields: Dict[str, Any] = {}
    if state is not None:
        if state not in WORK_STATES:
            raise ValueError(f"invalid work state: {state}")
        if current["state"] in TERMINAL_STATES and state not in TERMINAL_STATES:
            return current
        fields["state"] = state
    if waiting_reason is not None:
        fields["waiting_reason"] = waiting_reason
    if objective is not None:
        fields["objective"] = _clip(objective, MAX_OBJECTIVE_CHARS)
    if completion_result is not None:
        fields["completion_result"] = _clip(completion_result, MAX_TEXT_CHARS)
    if refs is not None:
        merged = dict(current.get("refs") or {})
        merged.update(refs)
        fields["refs_json"] = json.dumps(merged, ensure_ascii=False)
    if not fields:
        return current
    fields["updated_at"] = _now()
    assignments = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [work_id]
    with _transaction(hermes_home) as conn:
        conn.execute(f"UPDATE work SET {assignments} WHERE id=?", values)
        row = conn.execute("SELECT * FROM work WHERE id=?", (work_id,)).fetchone()
    return None if row is None else _row_to_work(row)


def complete_work(
    work_id: str,
    result: str,
    hermes_home: HomeLike = None,
    *,
    material: bool = True,
) -> Optional[Dict[str, Any]]:
    current = get_work(work_id, hermes_home)
    if current is None:
        return None
    updated = update_work(
        work_id,
        hermes_home=hermes_home,
        state="completed",
        completion_result=result,
    )
    if updated is not None and current["state"] != "completed" and material:
        increment_metric("useful_autonomous_completions", hermes_home=hermes_home)
    return updated


def drop_work(work_id: str, reason: str, hermes_home: HomeLike = None) -> Optional[Dict[str, Any]]:
    current = get_work(work_id, hermes_home)
    updated = update_work(
        work_id,
        hermes_home=hermes_home,
        state="dropped",
        completion_result=reason,
    )
    if updated is not None and current is not None and current["state"] != "dropped":
        increment_metric("dropped_as_not_worth_doing", hermes_home=hermes_home)
    return updated


def claim_signal(
    kind: str,
    claim_key: str,
    *,
    hermes_home: HomeLike = None,
    work_id: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        with _transaction(hermes_home) as conn:
            conn.execute(
                """INSERT INTO claims (kind, claim_key, work_id, result, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (kind, claim_key, work_id, "claimed", _now()),
            )
        return {"claimed": True, "duplicate": False, "kind": kind, "key": claim_key}
    except sqlite3.IntegrityError:
        return {"claimed": False, "duplicate": True, "kind": kind, "key": claim_key}


def release_claim(
    kind: str,
    claim_key: str,
    *,
    hermes_home: HomeLike = None,
) -> None:
    with _transaction(hermes_home) as conn:
        conn.execute(
            "DELETE FROM claims WHERE kind=? AND claim_key=?",
            (kind, claim_key),
        )


def record_collab(
    *,
    work_id: str,
    from_agent: str,
    to_agent: str,
    evidence_hash: str,
    goal_hash: str,
    hermes_home: HomeLike = None,
) -> Dict[str, Any]:
    from_agent = (from_agent or "").strip().lower()
    to_agent = (to_agent or "").strip().lower()
    rejected = {"allowed": False, "goal_hash": goal_hash}
    with _transaction(hermes_home) as conn:
        bounce_evidence = conn.execute(
            """SELECT 1 FROM collab
               WHERE from_work_id=? AND from_profile=? AND to_profile=?
                 AND evidence_hash=? AND evidence_hash != ''""",
            (work_id, to_agent, from_agent, evidence_hash),
        ).fetchone()
        bounce_goal = conn.execute(
            """SELECT 1 FROM collab
               WHERE from_profile=? AND to_profile=? AND goal_hash=?""",
            (to_agent, from_agent, goal_hash),
        ).fetchone()
        if bounce_evidence is not None or bounce_goal is not None:
            return {**rejected, "reason": "ping_pong"}
        duplicate = conn.execute(
            """SELECT 1 FROM collab
               WHERE from_work_id=? AND from_profile=? AND to_profile=?
                 AND goal_hash=?""",
            (work_id, from_agent, to_agent, goal_hash),
        ).fetchone()
        if duplicate is not None:
            return {**rejected, "reason": "duplicate_delegation"}
        fanout = conn.execute(
            "SELECT COUNT(DISTINCT to_profile) FROM collab WHERE from_work_id=?",
            (work_id,),
        ).fetchone()
        if int(fanout[0]) >= MAX_FANOUT:
            return {**rejected, "reason": "fanout_cap"}
        try:
            conn.execute(
                """INSERT INTO collab (
                     from_work_id, from_profile, to_profile,
                     evidence_hash, goal_hash, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (work_id, from_agent, to_agent, evidence_hash, goal_hash, _now()),
            )
        except sqlite3.IntegrityError:
            return {**rejected, "reason": "duplicate_delegation"}
    _mirror_collab(
        work_id=work_id,
        from_agent=from_agent,
        to_agent=to_agent,
        evidence_hash=evidence_hash,
        goal_hash=goal_hash,
        hermes_home=hermes_home,
    )
    return {"allowed": True, "goal_hash": goal_hash}


def undo_collab(
    *,
    work_id: str,
    from_agent: str,
    to_agent: str,
    goal_hash: str,
    hermes_home: HomeLike = None,
) -> None:
    from_agent = (from_agent or "").strip().lower()
    to_agent = (to_agent or "").strip().lower()
    with _transaction(hermes_home) as conn:
        conn.execute(
            """DELETE FROM collab
               WHERE from_work_id=? AND from_profile=? AND to_profile=?
                 AND goal_hash=?""",
            (work_id, from_agent, to_agent, goal_hash),
        )
    from agent.autonomy.paths import resolve_home, sibling_profile_home

    target_home = sibling_profile_home(to_agent, hermes_home)
    if target_home.resolve() == resolve_home(hermes_home):
        return
    try:
        with _transaction(target_home) as conn:
            conn.execute(
                """DELETE FROM collab
                   WHERE from_work_id=? AND from_profile=? AND to_profile=?
                     AND goal_hash=?""",
                (work_id, from_agent, to_agent, goal_hash),
            )
    except sqlite3.Error:
        return


def _mirror_collab(
    *,
    work_id: str,
    from_agent: str,
    to_agent: str,
    evidence_hash: str,
    goal_hash: str,
    hermes_home: HomeLike = None,
) -> None:
    """Copy the outbound row into the target's ledger so reverse ping-pong works."""
    from agent.autonomy.paths import resolve_home, sibling_profile_home

    target_home = sibling_profile_home(to_agent, hermes_home)
    if target_home.resolve() == resolve_home(hermes_home):
        return
    try:
        with _transaction(target_home) as conn:
            conn.execute(
                """INSERT OR IGNORE INTO collab (
                     from_work_id, from_profile, to_profile,
                     evidence_hash, goal_hash, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (work_id, from_agent, to_agent, evidence_hash, goal_hash, _now()),
            )
    except sqlite3.Error:
        return


def prune_terminal(hermes_home: HomeLike = None, keep: int = 20) -> int:
    """Drop old noise (dropped) rows only. Completed keys stay reserved."""
    with _transaction(hermes_home) as conn:
        rows = conn.execute(
            "SELECT id FROM work WHERE state='dropped' ORDER BY updated_at DESC"
        ).fetchall()
        extra = [row["id"] for row in rows[keep:]]
        if extra:
            conn.executemany("DELETE FROM work WHERE id=?", [(wid,) for wid in extra])
    return len(extra)


def increment_metric(key: str, delta: int = 1, hermes_home: HomeLike = None) -> int:
    with _transaction(hermes_home) as conn:
        conn.execute(
            """INSERT INTO metrics (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value=value + ?""",
            (key, delta, delta),
        )
        row = conn.execute("SELECT value FROM metrics WHERE key=?", (key,)).fetchone()
    return int(row["value"])


def get_metrics(hermes_home: HomeLike = None) -> Dict[str, int]:
    with _transaction(hermes_home) as conn:
        rows = conn.execute("SELECT key, value FROM metrics ORDER BY key").fetchall()
    return {row["key"]: int(row["value"]) for row in rows}


def get_meta(key: str, hermes_home: HomeLike = None) -> Optional[str]:
    with _transaction(hermes_home) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return None if row is None else row["value"]


def set_meta(key: str, value: str, hermes_home: HomeLike = None) -> None:
    with _transaction(hermes_home) as conn:
        conn.execute(
            """INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,
               updated_at=excluded.updated_at""",
            (key, value, _now()),
        )


def add_notice(
    kind: str,
    text: str,
    *,
    work_id: Optional[str] = None,
    hermes_home: HomeLike = None,
) -> None:
    with _transaction(hermes_home) as conn:
        conn.execute(
            """INSERT INTO notices (kind, text, work_id, created_at, delivered)
               VALUES (?, ?, ?, ?, 0)""",
            (kind, text, work_id, _now()),
        )


def pending_notices(hermes_home: HomeLike = None) -> List[Dict[str, Any]]:
    with _transaction(hermes_home) as conn:
        rows = conn.execute(
            "SELECT id, kind, text, work_id, created_at FROM notices "
            "WHERE delivered=0 ORDER BY id"
        ).fetchall()
    return [
        {
            "id": row["id"],
            "kind": row["kind"],
            "text": row["text"],
            "work_id": row["work_id"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]
