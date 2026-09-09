"""Compile accepted owner amendments without blocking the RPC event loop."""
import contextvars
import logging
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)


def start_owner_amendment(agent: Any, text: Any, source_id: str, token: Any) -> threading.Thread:
    """The caller has fenced admission before making the steering text visible."""
    from agent.execution_scope import finish_amendment
    def run():
        try:
            finish_amendment(agent, text, source_id, token)
        except Exception as exc:
            logger.warning("Owner amendment execution authority could not bind: %s", type(exc).__name__)
            emit = getattr(agent, "_emit_status", None)
            if callable(emit):
                emit("Execution paused: the current instruction's updated scope could not be established.")
    context = contextvars.copy_context()
    worker = threading.Thread(target=context.run, args=(run,), daemon=True,
                              name="owner-execution-amendment")
    worker.start()
    return worker


def admit_owner_amendment(agent: Any, text: Any, accept: Callable[[Any], bool]) -> bool:
    """Fence native action admission before exposing a current owner correction."""
    import os
    import uuid

    from agent.delegation_context import is_delegated_child_process_context
    from gateway.background_delivery import background_delivery_active

    # A machine-spawned surface may narrow actor input but cannot mint owner authority.
    if (os.environ.get("HERMES_EXECUTION_SCOPE") is not None
            or os.environ.get("HERMES_OWNER_CONTINUATION_ID")
            or os.environ.get("HERMES_KANBAN_TASK")
            or is_delegated_child_process_context() or background_delivery_active()):
        return bool(accept(text))

    from agent.execution_scope import begin_amendment, cancel_amendment

    source_id = "owner-amendment:" + uuid.uuid4().hex
    token = begin_amendment(agent, source_id, text=text)
    try:
        accepted = accept(text)
    except BaseException:
        cancel_amendment(token)
        raise
    if not accepted:
        cancel_amendment(token)
        return False
    start_owner_amendment(agent, text, source_id, token)
    return True
