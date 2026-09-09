"""Bound retries at the native turn boundary without interpreting tool prose."""
from __future__ import annotations


def record_gate_outcome(agent, result):
    from agent.execution_scope import ExecutionScopeDenied
    if isinstance(result, ExecutionScopeDenied):
        import json
        agent._execution_scope_last_denial_reason = None
        try:
            payload = json.loads(str(result))
            denial_reason = payload.get("error") if isinstance(payload, dict) else None
        except (TypeError, ValueError):
            denial_reason = None
        if isinstance(denial_reason, str) and denial_reason.strip():
            agent._execution_scope_last_denial_reason = denial_reason.strip()[:600]
        count = getattr(agent, '_execution_scope_denials', 0)
        agent._execution_scope_denials = (count if type(count) is int else 0) + 1
        if result.uncertain:
            agent._execution_scope_stop = 'execution_scope_uncertain'
        elif agent._execution_scope_denials >= 3:
            agent._execution_scope_stop = 'execution_scope_denied'
    else:
        agent._execution_scope_denials = 0
        agent._execution_scope_last_denial_reason = None


def abandon_attempt(agent, invocation_id=None):
    from agent.execution_scope import get_binding, mark_attempt_uncertain
    turn_id = getattr(agent, '_current_turn_id', None)
    binding = get_binding(turn_id)
    if binding is None:
        return None  # Programmatic execution has no scope-ledger evidence.
    admitted = mark_attempt_uncertain(turn_id)
    agent._execution_scope_stop = 'execution_scope_uncertain' if admitted else 'execution_scope_unavailable'
    if invocation_id is None:
        return bool(admitted)
    return binding.db.get_scope_action(binding.scope_id, f'turn:{turn_id}:call:{invocation_id}') is not None


def stop_message(agent):
    reason = getattr(agent, '_execution_scope_stop', None)
    if reason == 'execution_scope_uncertain':
        return 'Execution stopped because an admitted action may still be running or has no confirmed outcome. Its effects must be checked before further execution.'
    if reason == 'execution_scope_unavailable':
        return 'Execution stopped before action admission because authorization or execution preparation did not complete. The pending action was not started.'
    if reason == 'execution_scope_denied':
        last = getattr(agent, '_execution_scope_last_denial_reason', None)
        if isinstance(last, str) and last and "owner turn" in last:
            return last
        return "That step could not start from this turn. Continue with the current request."
    return None
