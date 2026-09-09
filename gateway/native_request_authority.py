"""Original authenticated request envelopes, separate from display/history data."""
import copy
import uuid


def original_request(instruction, *, source, source_id=None):
    return {"id": source_id or uuid.uuid4().hex, "instruction": copy.deepcopy(instruction), "source": source}


def stage_original_request(agent, envelope):
    if envelope is None:
        return None
    from tools.owner_task_authority import mint_execution_source
    return mint_execution_source(agent, envelope["instruction"],
        source=envelope["source"], source_id=envelope["id"])


def admit_event_amendment(agent, event, text, accept):
    if getattr(event, "internal", False):
        return bool(accept(text))
    from agent.owner_amendment import admit_owner_amendment
    return admit_owner_amendment(agent, text, accept)
