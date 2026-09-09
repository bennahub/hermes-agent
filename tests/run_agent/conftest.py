"""Fast-path fixtures shared across tests/run_agent/.

Many tests in this directory exercise the retry/backoff paths in the
agent loop. Production code uses ``jittered_backoff(base_delay=5.0)``
with a ``while time.time() < sleep_end`` loop — a single retry test
spends 5+ seconds of real wall-clock time on backoff waits.

Mocking ``jittered_backoff`` to return 0.0 collapses the while-loop
to a no-op (``time.time() < time.time() + 0`` is false immediately),
which handles the most common case without touching ``time.sleep``.

We deliberately DO NOT mock ``time.sleep`` here — some tests
(test_interrupt_propagation, test_primary_runtime_restore, etc.) use
the real ``time.sleep`` for threading coordination or assert that it
was called with specific values. Tests that want to additionally
fast-path direct ``time.sleep(N)`` calls in production code should
monkeypatch ``run_agent.time.sleep`` locally (see
``test_anthropic_error_handling.py`` for the pattern).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _fast_retry_backoff(monkeypatch):
    """Short-circuit retry backoff for all tests in this directory."""
    # The agent.turn_* retry paths import ``jittered_backoff`` lazily from
    # ``agent.retry_utils``; patch it there so rate-limit / invalid-response /
    # server-error retries don't burn real wall-clock seconds.
    from agent import retry_utils as _retry_utils
    monkeypatch.setattr(_retry_utils, "jittered_backoff", lambda *a, **k: 0.0)


@pytest.fixture()
def scoped_test_ingress(tmp_path, monkeypatch):
    """Opt-in real original-source ingress; only auxiliary policy responses are offline."""
    import json
    from types import SimpleNamespace
    from hermes_state import SessionDB
    from tools.owner_task_authority import mint_execution_source
    databases = []
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    def install(agent):
        db = SessionDB(tmp_path / 'state.db')
        databases.append(db)
        agent._session_db = db
        def stage(instruction, calls):
            assert mint_execution_source(agent, instruction)
            allowed = [{'tool': name, 'arguments': arguments} for name, arguments in calls]
            def policy(**kwargs):
                assert kwargs['task'] == 'execution_scope'
                payload = json.loads(kwargs['messages'][-1]['content'])
                if 'invocation' not in payload:
                    assert payload['original_instruction'] == instruction
                    result = {'objective': instruction, 'permitted': [instruction], 'excluded': ['Unrelated tasks']}
                else:
                    assert payload['original_instruction'] == instruction
                    result = {'allowed': payload['invocation'] in allowed, 'reason': 'Exact configured test action'}
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(result)))])
            monkeypatch.setattr('agent.auxiliary_client.call_llm', policy)
        agent._test_execution_request = stage
    yield install
    for db in databases:
        db.close()
