import threading
from types import SimpleNamespace

import pytest
from tui_gateway.interactive_progress import OwnerProgressWatch, record


class Schedule:
    def __init__(self):
        self.cancelled = False
    def __call__(self, fn, interval):
        self.tick = fn
        return self
    def cancel(self):
        self.cancelled = True


def watch_fixture(agent=None):
    clock = [100.]
    events = []
    scheduler = Schedule()
    session = {"running": True, "agent": agent}
    waiting = [False]
    watch = OwnerProgressWatch(session, lambda e, p: events.append((e, p)), lambda: waiting[0],
                               received=99., clock=lambda: clock[0], scheduler=scheduler)
    watch.mark_agent_ready()
    return watch, session, clock, events, waiting, scheduler


def test_degraded_at_30s_not_repeated_and_cleared_by_real_progress():
    request = threading.Event(); request.set()
    watch, session, clock, events, _, scheduler = watch_fixture(SimpleNamespace(_model_request_active=request))
    record(session, "message.start", {}, monotonic=100.)
    record(session, "thinking.delta", {"text": "reasoning"}, monotonic=125.)
    clock[0] = 129.9
    watch.tick()
    assert events == []
    clock[0] = 130.
    watch.tick()
    assert events == []
    request.clear()
    clock[0] = 159.9
    watch.tick()
    assert events == []
    clock[0] = 160.
    watch.tick()
    assert len(events) == 1
    assert "failed" not in events[0][1]["text"]
    record(session, "message.delta", {"text": "Here is the result"}, monotonic=160.)
    watch.tick()
    assert events[-1][0] == "notification.clear"
    watch.close()
    assert scheduler.cancelled


def test_real_tool_and_owner_wait_do_not_trigger_false_stall():
    watch, session, clock, events, waiting, _ = watch_fixture()
    record(session, "message.start", {}, monotonic=100.)
    record(session, "tool.start", {"tool_id": "a", "name": "terminal"}, monotonic=101.)
    clock[0] = 150.
    watch.tick()
    assert events == []
    session["_interactive_progress"]["tools"].clear()
    waiting[0] = True
    clock[0] = 200.
    watch.tick()
    assert events == []
    waiting[0] = False
    clock[0] = 229.9
    watch.tick()
    assert events == []
    clock[0] = 230.
    watch.tick()
    assert "Agent setup is still pending" in events[0][1]["text"]
    watch.close()


def test_new_turn_and_terminal_cancel_old_watch_without_replay():
    watch, session, clock, events, _, scheduler = watch_fixture()
    session["_interactive_progress"] = {}
    clock[0] = 160.
    assert watch.tick() is False
    assert events == []
    watch.close()
    assert scheduler.cancelled
    watch, session, clock, events, _, scheduler = watch_fixture()
    record(session, "message.start", {}, monotonic=100.)
    record(session, "message.complete", {}, monotonic=101.)
    clock[0] = 160.
    assert watch.tick() is False
    assert events == []
    watch.close()


@pytest.mark.parametrize("hidden,expected", [(False, 1), (True, 0)])
def test_canonical_prompt_submit_mints_watch_only_for_accepted_owner(tmp_path, monkeypatch, hidden, expected):
    from tui_gateway import server, interactive_progress
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    session = {"running": False, "history_lock": threading.RLock(), "history": [],
               "session_key": "qa-session", "profile_home": str(tmp_path), "agent": object()}
    monkeypatch.setattr(server, "_sessions", {"qa": session})
    monkeypatch.setattr(server, "_sess_nowait", lambda *a: (session, None))
    monkeypatch.setattr(server, "_legacy_group_fence_error", lambda *a: None)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a: None)
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a: False)
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {})
    monkeypatch.setattr(server, "_persist_session_row_for_submit", lambda *a: None)
    monkeypatch.setattr(server, "_restart_completed_failed_agent_build", lambda *a: False)
    monkeypatch.setattr(server, "_start_agent_build", lambda *a: None)
    monkeypatch.setattr(server, "_wait_agent_for_prompt", lambda *a: None)
    monkeypatch.setattr(server, "_resolve_reply_reference", lambda *a: (None, ""))
    dispatched = []
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *a, **kw: dispatched.append(kw) or True)
    class InlineThread:
        def __init__(self, target, **kw): self.target = target
        def start(self): self.target()
        def is_alive(self): return False
    monkeypatch.setattr(server, "threading", SimpleNamespace(Thread=InlineThread, current_thread=threading.current_thread))
    observed = []
    class Watch:
        def __init__(self, *a, **kw):
            observed.append(kw); self.ready = False; self.closed = False
        def mark_agent_ready(self): self.ready = True
        def bind_agent(self, agent): self.agent = agent
        def close(self): self.closed = True
    monkeypatch.setattr(interactive_progress, "OwnerProgressWatch", Watch)
    params = {"session_id": "qa", "text": "Open Wikipedia", "client_message_id": "eb742a66-8f2a-4450-8062-159285d6d150"}
    if hidden: params["display_kind"] = "hidden"
    result = server._methods["prompt.submit"]("request", params)
    assert "error" not in result, result
    assert len(observed) == expected
    assert len(dispatched) == 1
    assert bool(dispatched[0]["owner_task_nonce"]) is (not hidden)
    if observed:
        assert observed[0]["received"] > 0
        assert observed[0]["ui_session_id"] == "qa"
        assert observed[0]["client_message_id"] == params["client_message_id"]
    server._sessions.pop("qa", None)



def test_notice_transport_failure_cannot_fail_owner_turn_cleanup():
    watch, session, clock, events, _, scheduler = watch_fixture()
    def unavailable(*args):
        raise OSError("Disconnected transport")
    watch.emit = unavailable
    clock[0] = 131.
    watch.tick()
    watch.close()
    assert scheduler.cancelled


def test_replaced_turn_clears_only_its_own_warning():
    watch, session, clock, events, _, _ = watch_fixture()
    clock[0] = 131.
    watch.tick()
    old_key = events[0][1]["key"]
    session["_interactive_progress"] = {"trace_id": "new-turn"}
    assert watch.tick() is False
    assert events[-1] == ("notification.clear", {"key": old_key})
    watch.close()


def test_cold_setup_uses_existing_build_notice_until_agent_ready():
    clock, events, session = [100.], [], {"running": True, "agent": None}
    watch = OwnerProgressWatch(session, lambda *a: events.append(a), lambda: False,
                               clock=lambda: clock[0], scheduler=Schedule())
    clock[0] = 200.
    watch.tick()
    assert events == []
    session["agent"] = object()
    watch.mark_agent_ready()
    clock[0] = 229.9
    watch.tick()
    assert events == []
    clock[0] = 230.
    watch.tick()
    assert len(events) == 1
    assert "no tool is currently active" in events[0][1]["text"]
    watch.close()


def test_transport_does_not_hold_global_progress_lock_or_invert_activity_lock():
    from tui_gateway import activity
    watch, session, clock, events, _, _ = watch_fixture()
    clock[0] = 131.
    activity_held, emit_started, recording_done = threading.Event(), threading.Event(), threading.Event()
    def record_from_other_session():
        with activity._lock:
            activity_held.set()
            assert emit_started.wait(2)
            record({}, "message.start", {})
            recording_done.set()
    def transport(event, payload):
        emit_started.set()
        with activity._lock:
            events.append((event, payload))
    watch.emit = transport
    recording = threading.Thread(target=record_from_other_session, daemon=True)
    delivery = threading.Thread(target=watch.tick, daemon=True)
    recording.start()
    assert activity_held.wait(2)
    delivery.start()
    assert recording_done.wait(2)
    recording.join(2); delivery.join(2)
    assert not recording.is_alive() and not delivery.is_alive()
    watch.close()


def test_progress_during_transport_reconciles_stale_warning_immediately():
    watch, session, clock, events, _, _ = watch_fixture()
    clock[0] = 131.
    def transport(event, payload):
        events.append((event, payload))
        if event == "notification.show":
            record(session, "message.delta", {"text": "progress"}, monotonic=132.)
    watch.emit = transport
    watch.tick()
    assert [e for e, _ in events] == ["notification.show", "notification.clear"]
    assert events[0][1]["key"] == events[1][1]["key"]
    watch.close()


def test_stage_callback_is_scoped_first_occurrence_and_closed_safely():
    watch, session, clock, events, _, _ = watch_fixture()
    agent = SimpleNamespace()
    watch.bind_agent(agent)
    callback = agent._interactive_stage_callback
    callback("CONTEXT_BUILD_START", 101.)
    callback("MODEL_REQUEST_START", 105.)
    callback("MODEL_REQUEST_START", 110.)
    callback("MODEL_FIRST_STREAM_DELTA", 112.)
    assert watch.state["stages"]["MODEL_REQUEST_START"] == 6.
    assert watch.state["model_request_count"] == 2
    watch.close()
    assert agent._interactive_stage_callback is None
    callback("MODEL_REQUEST_END", 120.)
    assert "MODEL_REQUEST_END" not in watch.state["stages"]


def test_old_observer_cannot_remove_new_agent_callback():
    watch, session, _, _, _, _ = watch_fixture()
    agent = SimpleNamespace()
    watch.bind_agent(agent)
    newer = lambda *a: None
    agent._interactive_stage_callback = newer
    watch.close()
    assert agent._interactive_stage_callback is newer


def test_context_preparation_warning_uses_observed_stage():
    watch, session, clock, events, _, _ = watch_fixture(SimpleNamespace())
    watch.record_stage("CONTEXT_BUILD_START", 101.)
    clock[0] = 131.
    watch.tick()
    assert "Conversation context is still being prepared" in events[0][1]["text"]
    watch.close()
