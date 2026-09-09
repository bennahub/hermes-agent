"""Keep protected compaction heads at completed conversational boundaries."""


from typing import Any


def completed_head_boundary(messages: list[dict[str, Any]], requested: int) -> int:
    """Shrink a row budget to whole exchanges; never expand it through a long task.

    A new user row starts an exchange. The preceding assistant without tool calls
    closes the prior exchange. A cut inside user/tool/result/final rows would
    preserve its instruction while summarizing away the evidence it completed.
    No message text or inferred command intent participates in this boundary.
    """
    floor = 1 if messages and messages[0].get("role") == "system" else 0
    limit = min(len(messages), max(floor, requested))
    boundary = floor
    for index in range(floor + 1, limit + 1):
        previous = messages[index - 1]
        if (previous.get("role") == "assistant" and not previous.get("tool_calls")
                and (index == len(messages) or messages[index].get("role") == "user")):
            boundary = index
    return boundary
