"""Recover an accepted owner correction through the existing owner-result outbox."""
from __future__ import annotations

MESSAGE = (
    "I could not establish the execution scope for your latest correction. "
    "Execution is paused. Please restate how to continue."
)


def pending_amendment(work: dict | None) -> dict | None:
    marker = (work or {}).get("refs", {}).get("execution_amendment")
    return marker if isinstance(marker, dict) and marker.get("state") == "pending" else None


def settle_pending_amendment(work: dict, home) -> dict | None:
    """Idempotently publish a structured source-bound request, never replay old policy."""
    marker = pending_amendment(work)
    if marker is None:
        return None
    if work["refs"].get("pending_owner_result"):
        return work
    if work["state"] not in {"working", "waiting", "investigating", "actionable"}:
        return None
    from agent.autonomy.owner_continuity import request_finish

    return request_finish(
        work["id"], MESSAGE, terminal="needs_owner", hermes_home=home,
        expected_state=work["state"],
        expected_refs={"execution_scope": marker["prior_scope"],
                       "resume_generation": marker["generation"],
                       "execution_amendment": marker, "owner_stop": None,
                       "pending_owner_result": None},
        extra_refs={"outcome_kind": "execution_scope_unavailable"},
    )
