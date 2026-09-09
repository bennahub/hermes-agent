from types import SimpleNamespace
from unittest.mock import MagicMock
import queue

from tools.process_registry import ProcessRegistry, ProcessSession
from tui_gateway import session_notifications as notifications


def test_registry_native_owner_suppresses_legacy_queue_but_unowned_is_once(monkeypatch):
    registry = ProcessRegistry()
    monkeypatch.setattr(registry, "_write_checkpoint", lambda: None)
    native_signal = MagicMock()
    monkeypatch.setattr(
        "agent.autonomy.owner_continuity.signal_process_completion", native_signal
    )
    native = ProcessSession(
        id="proc-native", command="sleep 2", task_id="task", exited=True,
        exit_code=0, owner_continuity_work_id="aw-native",
        owner_continuity_home="/tmp/hermes", notify_on_complete=True,
    )
    registry._running[native.id] = native
    registry._move_to_finished(native)
    assert native_signal.call_count == 1
    assert registry.completion_queue.empty()

    legacy = ProcessSession(
        id="proc-legacy", command="echo done", task_id="task", exited=True,
        exit_code=0, notify_on_complete=True,
    )
    registry._running[legacy.id] = legacy
    registry._move_to_finished(legacy)
    registry._move_to_finished(legacy)
    events = []
    while not registry.completion_queue.empty():
        events.append(registry.completion_queue.get_nowait())
    assert [event["session_id"] for event in events] == ["proc-legacy"]


def test_tui_drops_queued_native_event_after_work_advance_but_dispatches_unowned(monkeypatch):
    native = SimpleNamespace(
        owner_continuity_work_id="aw-native", owner_continuity_home="/tmp/hermes",
        started_at=123.5,
    )
    registry = SimpleNamespace(
        completion_queue=queue.Queue(),
        get=lambda process_id: native if process_id == "proc-native" else None,
        is_completion_consumed=lambda _process_id: False,
    )
    session = {"session_key": "owner", "running": False}
    monkeypatch.setattr(notifications, "_notification_event_belongs_elsewhere", lambda *_: False)
    monkeypatch.setattr(notifications, "_notification_event_requires_owner", lambda _evt: False)
    monkeypatch.setattr(notifications, "_emit", lambda *_: None, raising=False)
    claim = lambda _session: True
    monkeypatch.setattr(notifications, "_notif_claim_turn", claim)
    dispatched = []
    monkeypatch.setattr(notifications, "_notif_dispatch_event", lambda *args: dispatched.append(args))
    event = {"type": "completion", "session_id": "proc-native", "started_at": 123.5}
    assert notifications._notif_handle_event("ui", session, event, set(), registry, lambda _evt: "done", None)
    assert dispatched == []

    legacy = {"type": "completion", "session_id": "proc-legacy", "started_at": 999.0}
    assert notifications._notif_handle_event("ui", session, legacy, set(), registry, lambda _evt: "done", None)
    assert len(dispatched) == 1
