"""Trusted owner-origin signal for a task.

Why this exists
---------------

An owner who tells an agent "edit Mishari's charter and remove Nasser" has
already authorised the writes that instruction requires. Asking them again, per
file, is not a safety property — it is a tax on a decision they already made.

The obvious implementation is the wrong one. If the *agent* could declare "this
task came from the owner", the declaration would be prose the model emits, and
any text that reached the model — a web page, a teammate's memo, a file it read
— could ask it to emit that prose. An agent that can grant itself authority is
not a gate.

So the signal is minted **at the server boundary that already authenticated the
owner**, before the model exists in the picture:

    authenticated RPC ingress (the owner's own connection)
      → mint_pending(session, profile home, endpoint)
      → the turn that ingress starts binds it: bind_turn(session, turn id)
      → the protected-file gate consults it for that turn only
      → the turn ends and the grant is gone

Nothing in the model's context can reach `mint_pending`: it is called by the
JSON-RPC method handler, on the connection the owner authenticated, with values
taken from the session rather than from the message. Prose cannot forge it,
quoting the owner cannot forge it, and an agent-originated or scheduled run
never passes through that ingress at all.

What a grant is bound to
------------------------

The **turn id** — `<agent session>:<task>:<random>` — which is unique to one
turn of one agent's session, and which a tool can read wherever it runs. The
grant also records the profile home and every session name it was minted under.

The turn id is the binding for a reason found the hard way: the first version
keyed grants on the session key, and the session key is only put into a
contextvar by the *platforms* gateway. On the path Mobile actually uses nothing
sets it, so the lookup fell back to "default", matched nothing, and every owner
task was asked about anyway — the automated tests passed because they supplied
the key themselves.

A grant therefore cannot be used by another agent (different session, so a
different turn id), another task (different turn id), a replay of the same task
(single-use — binding spends the mint), or another server (a different process
holds a different table).
"""
from __future__ import annotations

from tools.approval_context import _is_unattended_platform_approval_context
from tools.approval_context import _is_cron_approval_context


import copy
import logging
import os
import threading
import time
import uuid

logger = logging.getLogger(__name__)

# Pending mints, keyed by session: one authenticated owner message that has not
# yet become a turn. Single-use — `bind_turn` pops it.
_pending: dict[str, list[dict]] = {}
# Active grants, keyed by turn id — unique to one turn of one agent's session.
_active: dict[str, dict] = {}
_lock = threading.Lock()

# A pending mint that never becomes a turn (submit rejected, client vanished)
# must not sit around waiting to attach itself to some later turn.
PENDING_TTL_SECONDS = 120.0
# An upper bound on a single task. `finalize_turn` drops a grant when the turn
# ends, but a turn that exits early — a preflight timeout, a rate limit — can
# skip that, so this is the backstop. One hour is longer than any real turn and
# short enough that a grant a crash left behind is not still there later in the
# day. A grant is bound to one turn id in any case, so the only thing that could
# use a leftover is a late callback still holding that same finished turn.
ACTIVE_TTL_SECONDS = 60 * 60.0

_MAX_ENTRIES = 256


def _now() -> float:
    return time.monotonic()


def _realpath(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return os.path.realpath(str(value))
    except (OSError, ValueError, RuntimeError):
        return str(value)


def mint_pending(session_keys, *, profile_home: str | None,
                 endpoint: str | None = None, source: str = "gateway",
                 instruction=None, source_id: str | None = None) -> str | None:
    """Record that an authenticated owner just sent this session a task.

    Called from the RPC ingress only. ``session_keys`` is every identifier the
    ingress knows for this conversation, because the turn that starts next
    knows only some of them: the tui path names a session three ways (the UI
    session, the session key and the agent's own session id) and sets none of
    the contextvars the platforms gateway sets. Indexing the mint under all of
    them is what lets the turn find it without depending on which name it
    happens to hold.
    """
    if isinstance(session_keys, str):
        session_keys = [session_keys]
    keys = [str(key).strip() for key in session_keys if str(key or "").strip()]
    # "default" is what an unresolved session key falls back to. Minting under
    # it would let one conversation's grant be claimed by another's turn.
    keys = [key for key in keys if key != "default"]
    if not keys:
        return None
    nonce = uuid.uuid4().hex
    record = {
        "nonce": nonce,
        "session_keys": list(dict.fromkeys(keys)),
        "profile_home": _realpath(profile_home),
        "endpoint": (endpoint or "").strip() or None,
        "source": source,
        "minted_at": _now(),
        "execution_source": ({"id": source_id or "ingress:" + nonce, "kind": "owner",
                              "instruction": copy.deepcopy(instruction), "source": source}
                             if instruction is not None else None),
    }
    with _lock:
        _expire_locked()
        if record["execution_source"] is not None:
            if len(_execution_pending) >= _MAX_ENTRIES:
                return None
            _execution_pending[nonce] = {
                **copy.deepcopy(record["execution_source"]),
                "session_keys": record["session_keys"], "minted_at": record["minted_at"],
            }
        if len(_pending) >= _MAX_ENTRIES:
            _pending.clear()
        for key in record["session_keys"]:
            # A queue, not a slot. Two messages sent while the agent is busy are
            # two tasks and run as two turns, in order — overwriting meant the
            # first turn claimed the second task's grant and the second turn ran
            # with none.
            _pending.setdefault(key, []).append(record)
    # Traced, because the first two attempts at this failed silently in
    # production while every test passed: without a line here there is no way
    # to tell "never minted" from "minted and never claimed".
    logger.warning("owner task authority minted: keys=%s home=%s source=%s nonce=%s",
                   record["session_keys"], record["profile_home"], source, nonce)
    return nonce


def _claiming_context_is_the_owners_turn() -> bool:
    """Whether the turn now starting is the one the owner's message started.

    A mint is filed under a conversation's names, so whatever turn binds first
    on that conversation takes it. Most of the time that is the owner's task —
    but a background review fork or a delegated child copies the parent session
    id and could bind first, and a grant is not something to hand to whoever
    asks earliest. These are the contexts that must never claim one.

    Fail-closed: an error here means "do not claim".
    """
    try:
        from agent.delegation_context import is_delegated_child_context
        if is_delegated_child_context():
            return False
    except Exception:
        return False
    try:
        import tools.approval as _approval
        if _is_cron_approval_context():
            return False
        if _is_unattended_platform_approval_context():
            return False
    except Exception:
        return False
    return True


def revoke_pending(nonce: str) -> bool:
    """Drop a pending mint by its nonce.

    A mint is created at ingress, but not every submit goes on to start a turn:
    a message steered or redirected into the running turn, or one whose deferred
    agent init fails, produces no turn to claim it. Left in place, that mint
    could be claimed by a LATER turn on the same session — a hidden continuation
    included. The ingress revokes its own nonce on exactly those paths, so
    nothing claimable is left behind.
    """
    nonce = (nonce or "").strip()
    if not nonce:
        return False
    removed = False
    with _lock:
        removed = _execution_pending.pop(nonce, None) is not None
        for key, queue in list(_pending.items()):
            kept = [r for r in queue if r.get("nonce") != nonce]
            if len(kept) != len(queue):
                removed = True
            if kept:
                _pending[key] = kept
            else:
                _pending.pop(key, None)
    return removed


def bind_turn(session_keys, turn_id: str, *, nonce: str | None = None) -> bool:
    """Attach the pending mint this turn was dispatched with to the turn.

    Single-use, and keyed on the turn's own ``nonce`` — the one the ingress
    minted and the dispatch chokepoint carried to exactly this turn — not
    whatever mint happens to sit first in the session's queue. That is what
    makes an orphaned mint safe: a mint whose turn was refused before it reached
    here is carried by no other turn, so no later or synthesized turn on the
    same session can bind it; it simply expires. A turn dispatched WITHOUT a
    nonce — a wake-up, an auto-continue, a platform delivery, any turn the owner
    did not start through the authenticated ingress — carries no owner authority
    and binds nothing.

    Keyed on the turn id from here on. A turn id is
    ``<agent session>:<task>:<random>``, unique to one turn of one agent's
    session — the scope a grant may have, and, unlike the session key, readable
    wherever a tool runs.
    """
    if isinstance(session_keys, str):
        session_keys = [session_keys]
    keys = [str(key).strip() for key in session_keys if str(key or "").strip()]
    keys = [key for key in keys if key != "default"]
    turn_id = (turn_id or "").strip()
    nonce = (nonce or "").strip() or None
    if not keys or not turn_id:
        return False
    # No carried nonce → this is not the turn the ingress minted for. Only that
    # exact turn may hold a grant, so fail closed.
    if nonce is None:
        return False
    if not _claiming_context_is_the_owners_turn():
        logger.info("owner task authority not claimed: this turn may not hold one (turn=%s)", turn_id)
        return False
    with _lock:
        # Execution source outlives the short protected-file privilege. The
        # exact carried nonce and session identity still must match; no queue
        # head, elapsed time, or historical content can supply this source.
        execution_source = _execution_pending.get(nonce)
        if (execution_source is not None
                and set(keys).intersection(execution_source["session_keys"])):
            if len(_execution_for_turn) >= _MAX_ENTRIES:
                return False
            _execution_for_turn[turn_id] = _execution_pending.pop(nonce)
        _expire_locked()
        record = None
        for key in keys:
            # The mint must be pending under one of THIS turn's session names AND
            # carry the nonce this turn was dispatched with — both, so a nonce
            # alone is never enough to reach across conversations.
            for item in _pending.get(key, []):
                if item.get("nonce") == nonce:
                    record = item
                    break
            if record is not None:
                break
        if record is None:
            logger.info(
                "owner task authority not claimed: no pending mint matching this turn's "
                "nonce for keys=%s turn=%s (pending keys present: %s)",
                keys, turn_id, sorted(_pending.keys()))
            return False
        # Single-use: every name this mint was filed under is spent together.
        for key in record.get("session_keys", []):
            queue = _pending.get(key)
            if not queue:
                continue
            _pending[key] = [item for item in queue if item is not record]
            if not _pending[key]:
                _pending.pop(key, None)
        if len(_active) >= _MAX_ENTRIES:
            _active.clear()
        bound = dict(record)
        bound["turn_id"] = turn_id
        bound["bound_at"] = _now()
        _active[turn_id] = bound
    logger.warning("owner task authority claimed: keys=%s turn=%s nonce=%s",
                   keys, turn_id, bound.get("nonce"))
    return True


def turn_authority(turn_id: str | None, *, profile_home: str | None = None) -> dict | None:
    """The grant for this exact turn, if the owner minted one.

    ``profile_home`` is compared only when both sides have one: the tui path
    runs a turn under the installation root rather than the profile directory,
    so requiring a match there would reject every real grant. The binding that
    does the work is the turn id, and the scope rule decides which files it
    reaches.
    """
    turn_id = (turn_id or "").strip()
    if not turn_id:
        return None
    with _lock:
        _expire_locked()
        record = _active.get(turn_id)
        if record is None:
            return None
        minted_home = record.get("profile_home")
        if profile_home and minted_home and _realpath(profile_home) != minted_home:
            return None
        return copy.deepcopy(record)


def clear_turn(turn_id: str | None) -> int:
    """Drops a task's grant. Called when the turn ends."""
    turn_id = (turn_id or "").strip()
    if not turn_id:
        return 0
    with _lock:
        _execution_for_turn.pop(turn_id, None)
        return 1 if _active.pop(turn_id, None) is not None else 0


def clear_pending_for_session(session_keys) -> int:
    """Drop every un-bound mint for a session.

    Called when a session's queue is cleared — a stop, a cancel, a reset — which
    discards the submissions those mints were for. Coarse by design: without a
    nonce binding each mint to its exact queue envelope, the safe move is to drop
    all of that session's pending mints, so a later turn cannot claim one from a
    submission that was thrown away. A legitimate not-yet-started turn simply
    re-prompts, which is the safe direction.
    """
    if isinstance(session_keys, str):
        session_keys = [session_keys]
    keys = {str(k).strip() for k in session_keys if str(k or "").strip()}
    keys.discard("default")
    if not keys:
        return 0
    dropped = 0
    with _lock:
        for nonce, source in list(_execution_pending.items()):
            if keys.intersection(source["session_keys"]):
                _execution_pending.pop(nonce, None)
        for key in list(keys):
            queue = _pending.pop(key, None)
            if queue:
                dropped += len(queue)
    return dropped


def clear_all() -> None:
    with _lock:
        _pending.clear()
        _active.clear()
        _execution_pending.clear()
        _execution_for_turn.clear()


def _expire_locked() -> None:
    now = _now()
    for key, queue in list(_pending.items()):
        fresh = [r for r in queue if now - r.get("minted_at", now) <= PENDING_TTL_SECONDS]
        if fresh:
            _pending[key] = fresh
        else:
            _pending.pop(key, None)
    for key, record in list(_active.items()):
        if now - record.get("bound_at", now) > ACTIVE_TTL_SECONDS:
            _active.pop(key, None)


# Execution provenance is separate from the protected-file owner privilege above.
# Native CLI/platform inputs can supply scope without acquiring that privilege.
_execution_pending: dict[str, dict] = {}
_execution_for_turn: dict[str, dict] = {}


def mint_execution_source_token(session_keys, instruction, *, source: str,
                                source_id: str | None = None) -> str | None:
    """Single-use envelope from a trusted ingress, never from reconstructed history."""
    if isinstance(session_keys, str):
        session_keys = [session_keys]
    keys = [str(k) for k in session_keys if k and str(k) != "default"]
    if not keys or instruction is None:
        return None
    token = uuid.uuid4().hex
    with _lock:
        now = _now()
        if len(_execution_pending) >= _MAX_ENTRIES:
            return None
        _execution_pending[token] = {
            "id": source_id or "ingress:" + token, "kind": "owner",
            "instruction": copy.deepcopy(instruction), "source": source,
            "session_keys": keys, "minted_at": now,
        }
    return token


def mint_execution_source(agent, instruction, *, source="cli", source_id=None):
    """Stage a fresh local input; machine-spawned CLI must inherit, never remint."""
    with _lock:
        _execution_pending.pop(getattr(agent, "_pending_execution_source", None), None)
    agent._pending_execution_source = None
    from agent.delegation_context import is_delegated_child_process_context
    from gateway.background_delivery import background_delivery_active
    if (os.environ.get("HERMES_EXECUTION_SCOPE") is not None
            or os.environ.get("HERMES_OWNER_CONTINUATION_ID")
            or os.environ.get("HERMES_KANBAN_TASK")
            or is_delegated_child_process_context() or background_delivery_active()):
        return None
    token = mint_execution_source_token(
        [getattr(agent, "session_id", None)], instruction, source=source, source_id=source_id)
    agent._pending_execution_source = token
    return token


def consume_execution_source(agent):
    """Consume only this exact dispatched source; queue residence is not expiry."""
    token = getattr(agent, "_pending_execution_source", None)
    nonce = getattr(agent, "_pending_owner_task_nonce", None)
    agent._pending_execution_source = None
    with _lock:
        bound = _execution_for_turn.pop(getattr(agent, "_current_turn_id", None), None)
        if bound is not None:
            return copy.deepcopy(bound)
        for key in (token, nonce):
            if not isinstance(key, str):
                continue
            record = _execution_pending.pop(key, None)
            if record and getattr(agent, "session_id", None) in record["session_keys"]:
                return copy.deepcopy(record)
        return None


def turn_execution_source(turn_id):
    """Original source provenance without conferring protected-file privilege."""
    with _lock:
        source = _execution_for_turn.get(turn_id)
        return copy.deepcopy(source) if source is not None else None


def peek_execution_source(agent):
    """Read exact current ingress for pre-admission compilation, without spending it."""
    with _lock:
        source = _execution_for_turn.get(getattr(agent, "_current_turn_id", None))
        if source is None:
            source = _execution_pending.get(getattr(agent, "_pending_execution_source", None))
        if source is None:
            source = _execution_pending.get(getattr(agent, "_pending_owner_task_nonce", None))
        if source is None or getattr(agent, "session_id", None) not in source["session_keys"]:
            return None
        return copy.deepcopy(source)
