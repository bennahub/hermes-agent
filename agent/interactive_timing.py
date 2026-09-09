"""Optional content-free clocks for the owning interactive session observer."""
from __future__ import annotations

import time

STAGES = frozenset({"CONTEXT_BUILD_START", "CONTEXT_BUILD_DONE", "MODEL_REQUEST_START",
                    "MODEL_FIRST_STREAM_DELTA", "MODEL_REQUEST_END"})


def stage_emitter(agent):
    """Capture one observer identity so late callbacks cannot enter a newer turn."""
    try:
        callback = getattr(agent, "_interactive_stage_callback", None)
    except Exception:
        callback = None
    def emit(stage):
        try:
            if stage in STAGES and callable(callback):
                callback(stage, time.monotonic())
        except Exception:
            pass  # Optional diagnostics cannot fail, retry, or replay work.
    return emit


def emit_stage(agent, stage: str) -> None:
    """One-shot convenience; paired start/end events should capture stage_emitter."""
    stage_emitter(agent)(stage)
