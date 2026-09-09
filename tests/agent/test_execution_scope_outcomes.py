from types import SimpleNamespace
import json

from agent.execution_scope import ExecutionScopeDenied
from agent.execution_scope_outcomes import record_gate_outcome, stop_message


def test_repeated_denial_stop_preserves_last_reason_and_clears_malformed_latest():
    agent = SimpleNamespace(_execution_scope_denials=0, _execution_scope_stop=None)
    for index in range(3):
        record_gate_outcome(agent, ExecutionScopeDenied(json.dumps({
            "error": f"target evidence missing {index}",
            "error_type": "execution_scope_denied",
            "effect_disposition": "not_started",
        })))
    message = stop_message(agent)
    assert message == "target evidence missing 2"

    malformed = SimpleNamespace(_execution_scope_denials=0, _execution_scope_stop=None)
    for result in (ExecutionScopeDenied('{"error":"old reason"}'),
                   ExecutionScopeDenied('{"error":"second reason"}'),
                   ExecutionScopeDenied("not-json")):
        record_gate_outcome(malformed, result)
    assert "old reason" not in stop_message(malformed)
    assert "second reason" not in stop_message(malformed)
    assert "repeated proposals could not be authorized" in stop_message(malformed)

    uncertain = SimpleNamespace(_execution_scope_denials=3, _execution_scope_stop=None)
    record_gate_outcome(uncertain, ExecutionScopeDenied(
        '{"error":"effect unresolved","error_type":"execution_scope_uncertain"}', uncertain=True
    ))
    assert stop_message(uncertain).startswith("Execution stopped because an admitted action")
