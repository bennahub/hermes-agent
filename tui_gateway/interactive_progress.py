"""Content-free interactive timing and material Computer failure notices.

This observes existing callbacks. It never dispatches a tool or changes model
context, and cannot replay a side effect. Missing timestamps remain missing.
"""
from __future__ import annotations

import logging
import hashlib
import json
import time
import threading
import uuid

_lock = threading.RLock()

logger = logging.getLogger(__name__)
_NOTICES = {
    "COMPUTER_CAPACITY_EXHAUSTED": "Computer could not start because all active slots are occupied. Existing computers were left running.",
    "NATIVE_OPERATION_UNCERTAIN": "Computer operation failed and its runtime stopped. The action's outcome must be verified before retrying.",
}


def _identity_hash(value):
    return hashlib.sha256(value.encode()).hexdigest() if isinstance(value, str) and value else None


def _record(session: dict, event: str, payload: dict, *, monotonic=None) -> None:
    now = time.monotonic() if monotonic is None else monotonic
    if event == "message.start" and not (session.get("_interactive_progress") or {}).pop("awaiting_turn_start", False):
        session["_interactive_progress"] = {"start": now, "trace_id": uuid.uuid4().hex, "stages": {}, "notices": set(), "tools": {}}
    state = session.get("_interactive_progress")
    if not state or state.get("terminal"):
        return
    stages = state["stages"]
    stage = {"message.start": "TURN_START", "message.delta": "FIRST_VISIBLE_TEXT",
             "thinking.delta": "FIRST_REASONING_DELTA", "reasoning.delta": "FIRST_REASONING_DELTA",
             "tool.start": "FIRST_TOOL_CALL", "message.complete": "TURN_COMPLETE", "error": "TURN_COMPLETE"}.get(event)
    if stage and (not event.endswith(".delta") or bool(payload.get("text"))):
        stages.setdefault(stage, max(0., now - state["start"]))
    if event == "message.delta" and payload.get("text") or event == "tool.start":
        state["last_useful"] = now
    if event == "tool.start":
        tid = payload.get("tool_id")
        if isinstance(tid, str) and tid:
            # Concurrent calls remain fenced to their own observed starts.
            state["tools"][tid] = payload.get("name")
    if event in {"message.complete", "error"}:
        state["terminal"] = True
        state["tools"].clear()
        logger.info("interactive_turn_timings trace_id=%s stages_seconds=%s correlation_json=%s model_request_count=%s",
                    state["trace_id"], {k: round(v, 3) for k, v in stages.items()},
                    json.dumps(state.get("correlation", {}), sort_keys=True), state.get("model_request_count", 0))


def _computer_notice(session: dict, tool_id: str, name: str, result: object) -> dict | None:
    state = session.get("_interactive_progress")
    if not state or state.get("terminal") or state["tools"].pop(tool_id, None) != name:
        return None
    observed = time.monotonic()
    state["last_useful"] = observed
    elapsed = max(0., observed - state["start"])
    state["stages"].setdefault("FIRST_TOOL_RESULT", elapsed)
    if name == "computer_wake":
        state["stages"].setdefault("FIRST_COMPUTER_WAKE_RESULT", elapsed)
        if isinstance(result, dict) and result.get("lifecycle") == "ready" and not result.get("error"):
            state["stages"].setdefault("COMPUTER_WAKE_READY", elapsed)
    if name not in {"computer_ensure", "computer_status", "computer_wake", "computer_observe", "computer_act"}:
        return None
    if not isinstance(result, dict) or not result.get("error"):
        return None
    code = result.get("error_code")
    if code not in _NOTICES or code in state["notices"]:
        return None
    state["notices"].add(code)
    return {"key": "interactive-computer-" + code.lower() + "-" + state["trace_id"], "level": "warning",
            "kind": "computer", "text": _NOTICES[code], "ttl_ms": 0}


def record(session: dict, event: str, payload: dict, *, monotonic=None) -> None:
    with _lock:
        _record(session, event, payload, monotonic=monotonic)


def computer_notice(session: dict, tool_id: str, name: str, result: object) -> dict | None:
    with _lock:
        return _computer_notice(session, tool_id, name, result)


class OwnerProgressWatch:
    """Observe one admitted Owner turn without interrupting or replaying it.

    A provider reasoning delta is not useful Owner progress. Active tools and
    explicit Owner decisions are real waits; this watchdog does not cancel them.
    The existing core liveness watchdog remains responsible for forced aborts.
    """

    def __init__(self, session, emit, waiting, *, received=None, clock=time.monotonic, scheduler=None,
                 ui_session_id=None, client_message_id=None):
        self.session, self.emit, self.waiting, self.clock = session, emit, waiting, clock
        self._delivery_lock = threading.RLock()
        now = clock()
        self.state = {"start": now if received is None else received,
                      "trace_id": uuid.uuid4().hex, "stages": {}, "notices": set(), "tools": {},
                      "awaiting_turn_start": True, "last_useful": now}
        # Hash native session IDs: exact join keys without exposing titles or
        # caller-supplied identifier text. Client message IDs are UUIDs at ingress.
        try:
            message_id = str(uuid.UUID(client_message_id)) if isinstance(client_message_id, str) else None
        except ValueError:
            message_id = None
        self.state["correlation"] = {
            "ui_session_sha256": _identity_hash(ui_session_id),
            "canonical_session_sha256": None,
            "client_message_id": message_id,
        }
        self.state["stages"]["OWNER_DISPATCH_THREAD_START"] = max(0., now - self.state["start"])
        if received is not None:
            self.state["stages"]["OWNER_RPC_RECEIVED"] = 0.
        self.warned, self.visible, self.closed = False, False, False
        self.worker_owned = False
        self.notice_key = "interactive-progress-degraded-" + self.state["trace_id"]
        with _lock:
            session["_interactive_progress"] = self.state
        if scheduler is None:
            from agent.periodic_scheduler import schedule
            scheduler = schedule
        self.handle = scheduler(self.tick, 1.)

    def claim_worker(self):
        """Fence dispatch-thread cleanup once a real worker owns this watch."""
        with _lock:
            self.worker_owned = True

    def mark_agent_ready(self):
        with _lock:
            if self.session.get("_interactive_progress") is self.state and not self.closed:
                now = self.clock()
                self.state["stages"]["AGENT_READY"] = max(0., now - self.state["start"])
                self.state["last_useful"] = now

    def bind_agent(self, agent):
        """Install only on this turn's AIAgent; never attach to a child/global provider."""
        if agent is None:
            return  # cancellation may finish the readiness wait without an agent
        try:
            self._agent = agent
            self.state["correlation"]["canonical_session_sha256"] = _identity_hash(getattr(agent, "session_id", None))
            self._previous_stage_callback = getattr(agent, "_interactive_stage_callback", None)
            self._stage_callback = self.record_stage
            agent._interactive_stage_callback = self._stage_callback
        except Exception:
            logger.debug("interactive timing callback unavailable")

    def record_stage(self, stage, observed):
        from agent.interactive_timing import STAGES
        if stage not in STAGES:
            return
        with _lock:
            if self.closed or self.session.get("_interactive_progress") is not self.state or self.state.get("terminal"):
                return
            self.state["stages"].setdefault(stage, max(0., observed - self.state["start"]))
            if stage == "MODEL_REQUEST_START":
                self.state["model_request_count"] = self.state.get("model_request_count", 0) + 1
            self.state["pipeline_phase"] = stage

    def tick(self):
        # A slow transport must not hold the process-wide progress lock. This
        # per-watch lock only orders this turn's show/clear with its close.
        with self._delivery_lock:
            waiting, now = self.waiting(), self.clock()
            with _lock:
                result, event = self._tick_locked(waiting, now)
            if event:
                self._emit(*event)
                # Progress/replacement can race transport delivery. Reconcile
                # a just-published stale warning using its unique old-turn key.
                with _lock:
                    stale = (self.closed or self.session.get("_interactive_progress") is not self.state
                             or self.state.get("terminal") or bool(self.state["tools"])
                             or self.state.get("last_useful", self.state["start"]) > now)
                    clear = self._clear_locked() if event[0] == "notification.show" and stale else None
                if clear:
                    self._emit(*clear)
            return result

    def _tick_locked(self, waiting, now):
        if self.closed or self.session.get("_interactive_progress") is not self.state:
            return False, self._clear_locked()
        if self.state.get("terminal") or self.session.get("_finalized") or not self.session.get("running"):
            return False, self._clear_locked()
        if "AGENT_READY" not in self.state["stages"]:
            return None, None  # existing deferred-build notice owns this interval
        if waiting or self.state["tools"]:
            self.state["last_useful"] = now
            return None, self._clear_locked()
        agent = self.session.get("agent")
        request = getattr(agent, "_model_request_active", None)
        if request is not None and request.is_set():
            # An in-flight provider request is active progress even when no
            # token has arrived yet; its own liveness/failure path owns timeout.
            self.state["last_useful"] = now
            return None, self._clear_locked()
        elapsed = now - self.state.get("last_useful", self.state["start"])
        if elapsed < 30.:
            return None, self._clear_locked()
        if self.warned:
            return None, None
        agent = self.session.get("agent")
        request = getattr(agent, "_model_request_active", None)
        phase = "context" if self.state.get("pipeline_phase") == "CONTEXT_BUILD_START" else (
            "agent_setup" if agent is None else (
                "provider" if request is not None and request.is_set() else "turn_execution"))
        self.state["stages"].setdefault("DEGRADED_OBSERVED", max(0., now - self.state["start"]))
        self.warned = self.visible = True
        return None, ("notification.show", {"key": self.notice_key, "level": "warning",
            "kind": "interactive", "ttl_ms": 0,
            "text": "This request has not produced useful progress for 30 seconds. " + {
                "context": "Conversation context is still being prepared.",
                "agent_setup": "Agent setup is still pending.",
                "provider": "The model request is still active.",
                "turn_execution": "Execution is still pending; no tool is currently active.",
            }[phase]})

    def _emit(self, event, payload):
        try:
            self.emit(event, payload)
        except Exception:
            logger.debug("interactive progress notice transport unavailable")

    def _clear_locked(self):
        if self.visible:
            self.visible = False
            return "notification.clear", {"key": self.notice_key}
        return None

    def close(self):
        with self._delivery_lock:
            with _lock:
                if self.closed:
                    return
                self.closed = True
                agent = getattr(self, "_agent", None)
                callback = getattr(self, "_stage_callback", None)
                try:
                    if agent is not None and getattr(agent, "_interactive_stage_callback", None) is callback:
                        agent._interactive_stage_callback = self._previous_stage_callback
                except Exception:
                    logger.debug("interactive timing callback cleanup unavailable")
                clear = self._clear_locked()
            if clear:
                self._emit(*clear)
        self.handle.cancel()
