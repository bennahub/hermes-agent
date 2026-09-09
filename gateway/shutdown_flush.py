"""Flush pending messages and agent transcripts to disk before shutdown to prevent data loss.

When FTS5 corruption blocks ``INSERT INTO messages``, ``_pending_messages`` and the live
``agent._session_messages`` are the only surviving copies; shutdown ``.clear()`` would drop them.
All hooks write atomic JSON payloads under ``<hermes_home>/pending_messages/``:
``flush_pending_to_file`` / ``flush_overflow_to_file`` (queue head / FIFO tail, before clear),
``recover_pending_to_db`` (after ``runner.start()``; replays via ``SessionDB.append_message``,
deletes each file on success), ``flush_agent_history_to_file`` (DB flush raised),
``spool_dropped_transcript_message`` / ``drain_transcript_spool``.

Every one of them resolves that directory through ``_get_flush_dir``, which refuses a
tombstoned or missing named profile home: a permanently deleted profile must never have
conversation content written back into it, and the ``mkdir`` here was the last writer that
could recreate its tree.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Reason tag for transcript messages dropped by the in-memory pending cap during live
# operation. Payloads carry the full transcript message dict for verbatim replay.
# See #78182.
TRANSCRIPT_CAP_DROP_REASON = "transcript_cap_drop"
# Monotonic tiebreaker so same-second spool files replay in drop order.
_TRANSCRIPT_SPOOL_SEQ = itertools.count()


def _get_flush_dir() -> Path:
    """Return the pending-messages flush directory under the active HERMES_HOME.

    Refuses a tombstoned/missing named profile home *before* the ``mkdir`` can recreate it.
    ``hermes -p <name> chat`` is interactive, so ``_profile_bound_backend_pids`` never sweeps
    it (``_BACKEND_TOKENS`` is serve/dashboard/gateway only): it keeps running with
    ``HERMES_HOME`` pointed at a profile the Owner has since permanently deleted, and every
    writer below reaches this ``mkdir`` on its way out. Ungated, the shutdown flush wrote the
    in-memory transcript back into the deleted tree — recreating ``profiles/<name>/`` behind
    ``rmtree`` with conversation content in it, which contradicts the delete dialog's "This
    cannot be undone". The gate is a no-op for a live home: the default home and every
    existing profile flush exactly as before.
    """
    from hermes_constants import assert_named_profile_home_live, get_hermes_home
    home = get_hermes_home()
    assert_named_profile_home_live(home)
    flush_dir = home / "pending_messages"
    flush_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        os.chmod(flush_dir, 0o700)
    return flush_dir


def _flush_dir_or_none(kind: str, reason: str) -> Optional[Path]:
    """``_get_flush_dir()``, or ``None`` when it refuses — a shutdown flush must never raise.

    Both shutdown writers already run inside ``suppress(Exception)`` at their call sites, so
    nothing crashed; but that suppression is silent. Returning ``None`` here keeps the
    never-take-down-the-process guarantee independent of any future caller, and leaves the
    operator a log line that distinguishes a deleted-profile skip from a disk failure.
    """
    try:
        return _get_flush_dir()
    except Exception as exc:
        logger.warning("Skipping %s flush (reason=%s): %s", kind, reason, exc)
        return None


def _write_payload(flush_dir: Path, payload: Dict[str, Any]) -> Path:
    """Atomically write one private, uniquely named recovery payload; return its path."""
    from utils import atomic_json_write
    final_path = flush_dir / f"pending-{uuid.uuid4().hex}.json"
    atomic_json_write(final_path, payload, mode=0o600, default=str)
    if os.name == "posix":
        # Persist the directory entry too; keep the published file (the only recovery copy) even if
        # fsync fails.
        try:
            directory_fd = os.open(flush_dir, os.O_RDONLY)
        except OSError as exc:
            logger.debug("Failed to fsync pending-message directory: %s", exc)
        else:
            try:
                os.fsync(directory_fd)
            except OSError as exc:
                logger.debug("Failed to fsync pending-message directory: %s", exc)
            finally:
                os.close(directory_fd)
    return final_path


def _flush_value(flush_dir: Path, kind: str, session_key: str, value: Any, **extra: Any) -> bool:
    """Serialise and write one pending value; return True when a payload was written."""
    try:
        serialised = _serialise_value(value)
        if serialised is None:
            return False
        _write_payload(flush_dir, {"session_key": session_key, **extra, "data": serialised})
        return True
    except Exception as exc:
        logger.debug("Failed to flush %s message for %s: %s", kind, session_key, exc)
        return False


def flush_pending_to_file(pending: Dict[str, Any], *, reason: str = "shutdown") -> int:
    """Serialise non-empty ``_pending_messages`` slots (``MessageEvent`` or str); return count."""
    if not pending:
        return 0
    flush_dir = _flush_dir_or_none("pending", reason)
    if flush_dir is None:
        return 0
    ts, flushed = int(time.time()), 0
    for session_key, value in list(pending.items()):
        if value is not None:
            flushed += _flush_value(flush_dir, "pending", session_key, value, reason=reason, ts=ts)
    if flushed:
        logger.info("Flushed %d pending message(s) to %s (reason=%s)", flushed, flush_dir, reason)
    return flushed


def flush_overflow_to_file(overflow_by_session: Dict[str, Any], *, reason: str = "shutdown") -> int:
    """Serialise the FIFO overflow tails (``queued_events``) to disk; return events flushed.

    The adapter slot holds the queue head and ``SessionState.conversation.queued_events`` the
    tail; both must survive restart. Each event is its own payload in the slot-flush shape so
    ``recover_pending_to_db`` replays them unchanged; ``seq`` preserves arrival order per session.
    """
    if not overflow_by_session:
        return 0
    flush_dir = _flush_dir_or_none("queued overflow", reason)
    if flush_dir is None:
        return 0
    ts, flushed = int(time.time()), 0
    for session_key, events in list(overflow_by_session.items()):
        if not session_key or not events:
            continue
        for seq, value in enumerate(list(events)):
            if value is not None:
                flushed += _flush_value(flush_dir, "overflow", session_key, value, reason=reason,
                                        ts=ts, seq=seq)
    if flushed:
        logger.info("Flushed %d queued overflow message(s) to %s (reason=%s)", flushed, flush_dir,
                    reason)
    return flushed


def spool_dropped_transcript_message(session_id: str, message: Dict[str, Any]) -> Optional[Path]:
    """Spool a cap-evicted transcript message; ``None`` on failure (callers degrade to drop+log).

    Uses the same on-disk pending spool as :func:`flush_pending_to_file` (one atomic JSON payload per
    message under ``<hermes_home>/pending_messages/``), so a runtime cap rotation no longer silently
    discards user data while the process stays up (#78182).

    Refuses a tombstoned/missing named profile home. The replaced-state.db handler in
    ``gateway.session_transcript`` spools here one line before it diverts to JSONL, and a profile
    DELETE is what "state.db was replaced under a live process" looks like from inside a turn —
    without this, a fragment of the permanently-deleted conversation is written back under the
    deleted profile, recreating its tree behind ``rmtree``. The same refusal now sits in
    ``_get_flush_dir`` for every other writer; the explicit call here keeps the check ahead of
    ``_write_payload``.
    """
    try:
        from hermes_constants import assert_named_profile_home_live, get_hermes_home
        assert_named_profile_home_live(get_hermes_home())
        return _write_payload(_get_flush_dir(), {
            "session_key": session_id, "reason": TRANSCRIPT_CAP_DROP_REASON, "ts": int(time.time()),
            "seq": next(_TRANSCRIPT_SPOOL_SEQ),
            "data": {"session_id": session_id, "message": message},
        })
    except Exception as exc:
        logger.debug("Failed to spool cap-dropped transcript message for %s: %s", session_id, exc)
        return None


def drain_transcript_spool(session_id: str, replay) -> tuple[int, int]:
    """Replay cap-dropped transcript messages spooled for *session_id*; return ``(replayed,
    remaining)``. ``replay(message_dict)`` runs per message in drop order; a spool file is deleted
    only after its replay succeeds. The first failure stops the drain (the DB is likely still
    unhealthy) and keeps the rest for retry.
    """
    try:
        candidates = list(_get_flush_dir().glob("pending-*.json"))
    except Exception as exc:
        logger.debug("Cannot scan transcript spool: %s", exc)
        return 0, 0
    entries = []
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if (payload.get("reason") != TRANSCRIPT_CAP_DROP_REASON
                or payload.get("session_key") != session_id):
            continue
        message = (payload.get("data") or {}).get("message")
        if not isinstance(message, dict):
            logger.warning("Removing structurally invalid transcript spool file %s", path)
            path.unlink(missing_ok=True)
            continue
        entries.append((payload.get("ts", 0), payload.get("seq", 0), path.name, path, message))
    ordered, replayed, remaining = sorted(entries, key=lambda e: e[:3]), 0, 0
    for idx, (_ts, _seq, _name, path, message) in enumerate(ordered):
        try:
            replay(message)
        except Exception as exc:
            logger.warning("Replay of spooled transcript message %s for %s failed; "
                           "keeping spool file for retry: %s", path, session_id, exc)
            remaining = len(ordered) - idx
            break
        path.unlink(missing_ok=True)
        replayed += 1
    if replayed:
        logger.info("Replayed %d spooled transcript message(s) for %s after DB recovery", replayed,
                    session_id)
    return replayed, remaining


def _json_safe(value: Any) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


def _serialise_value(value: Any) -> Optional[dict]:
    """Convert a pending message value to a JSON-serialisable dict."""
    if hasattr(value, "text"):  # MessageEvent-like object
        result: Dict[str, Any] = {"text": getattr(value, "text", "")}
        for attr in ("session_id", "platform", "sender_id", "sender_name", "reply_to", "media",
                     "raw_event"):
            val = getattr(value, attr, None)
            if val is not None:
                result[attr] = val if _json_safe(val) else str(val)
        return result
    if isinstance(value, str):  # runner-level _pending_messages
        return {"text": value}
    if isinstance(value, dict) and _json_safe(value):
        return value
    return {"text": str(value)}


def recover_pending_to_db(session_db=None) -> int:
    """Replay flush-dir ``*.json`` files via ``SessionDB.append_message``, deleting each on success.

    ``session_db=None`` opens (and afterwards releases) the shared default ``state.db``.
    Returns the number of messages recovered.
    """
    flush_files = sorted(_get_flush_dir().glob("*.json"))
    if not flush_files:
        return 0
    own_db = session_db is None
    if own_db:
        from hermes_state_registry import acquire
        session_db = acquire()
    recovered = 0
    try:
        for path in flush_files:
            payload = json.loads(path.read_text(encoding="utf-8"))
            # Agent-history snapshots are for manual operator recovery, not automatic DB insertion.
            if payload.get("reason") == "shutdown-with-unpersisted-agent-history":
                continue
            if _recover_one_payload(session_db, path, payload):
                recovered += 1
                path.unlink(missing_ok=True)
    finally:
        if own_db:  # shutdown cancellation/interrupt must not strand an owned DB
            with contextlib.suppress(Exception):
                from hermes_state_registry import release_or_close
                release_or_close(session_db)
    if recovered:
        logger.info("Recovered %d pending message(s) from shutdown flush", recovered)
    return recovered


def _recover_one_payload(session_db, path: Path, payload: Dict[str, Any]) -> bool:
    """Append one flush payload to ``session_db``; False (file kept) when structurally invalid."""
    # Cap-dropped transcript payloads carry the full message dict keyed by session_id — replay directly
    # (#78182). This handles spool files that were never drained before a restart.
    if payload.get("reason") == TRANSCRIPT_CAP_DROP_REASON:
        # Cap-dropped payloads carry the full message dict keyed by session_id — replay directly.
        data = payload.get("data", {}) or {}
        spooled_sid, message = data.get("session_id", ""), data.get("message")
        if not spooled_sid or not isinstance(message, dict):
            logger.warning("Cannot recover structurally invalid transcript spool "
                           "file %s; preserved for manual inspection", path)
            return False
        session_db.append_message(session_id=spooled_sid, role=message.get("role", "unknown"),
                                  content=message.get("content") or "",
                                  timestamp=message.get("timestamp") or payload.get("ts"))
        return True
    session_key, data = payload.get("session_key", ""), payload.get("data", {})
    text = data.get("text", "")
    if not text or not session_key:
        logger.warning("Cannot recover structurally invalid pending message from %s; "
                       "the flush file has been preserved", path)
        return False
    # session_key is a gateway routing key (e.g. "agent:main:telegram:..."); appending a row
    # needs the real session_id, which only the serialised data can supply at this stage.
    session_id = data.get("session_id", "")
    if not session_id:
        logger.warning("Cannot recover pending message for %s: no session_id in flush file and "
                       "session_key-to-id resolution is not available at this recovery stage. "
                       "The message text is preserved in %s", session_key, path)
        return False
    session_db.append_message(session_id=session_id, role="user", content=text,
                              timestamp=payload.get("ts", int(time.time())))
    return True


def flush_agent_history_to_file(session_id: Optional[str], history: list) -> None:
    """Best-effort dump of an agent's in-memory transcript before teardown. Used when
    ``_flush_messages_to_session_db`` raises (e.g. FTS/SQLite corruption): the transcript is written
    outside the broken DB so an operator can salvage it after repairing state.db. Failures are
    swallowed — shutdown must never block on a best-effort backup."""
    if not history:
        return
    try:
        flush_dir = _get_flush_dir()
        snapshot = []
        for _m in history:
            try:
                plain = isinstance(_m, (dict, list, str, int, float, bool, type(None)))
                snapshot.append(_m if plain else str(_m))
            except Exception:
                continue
        _write_payload(flush_dir, {
            "reason": "shutdown-with-unpersisted-agent-history", "issue": "#72680",
            "session_id": session_id, "count": len(snapshot), "messages": snapshot,
        })
        logger.warning("Preserved %d in-memory message(s) for session %s "
                       "(possible FTS corruption — recover after repairing state.db)",
                       len(snapshot), session_id)
    except Exception as _e:
        logger.warning("Agent-history shutdown preservation failed for session %s: %s", session_id,
                       _e)
