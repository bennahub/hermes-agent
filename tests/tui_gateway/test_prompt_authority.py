"""Owner scope compilation must not occupy the synchronous RPC dispatcher."""
import threading
from types import SimpleNamespace
from agent import execution_scope
from tui_gateway.prompt_authority import start_owner_amendment


def test_amendment_worker_returns_before_compilation_completes(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    seen = []
    def finish(agent, text, source_id, token):
        seen.append((agent, text, source_id, token))
        entered.set()
        assert release.wait(5)
    monkeypatch.setattr(execution_scope, "finish_amendment", finish, raising=False)
    agent, token = SimpleNamespace(), object()
    worker = start_owner_amendment(agent, "only inspect now", "fresh-correction", token)
    assert entered.wait(5)
    assert worker.is_alive()
    release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert seen == [(agent, "only inspect now", "fresh-correction", token)]


def test_failed_amendment_emits_status_without_unhandled_worker_error(monkeypatch):
    statuses = []
    def finish(*args):
        raise ValueError("invalid policy response")
    monkeypatch.setattr(execution_scope, "finish_amendment", finish, raising=False)
    worker = start_owner_amendment(SimpleNamespace(_emit_status=statuses.append), "stop writes", "new-correction", object())
    worker.join(5)
    assert not worker.is_alive()
    assert len(statuses) == 1
    assert "Execution paused" in statuses[0]
