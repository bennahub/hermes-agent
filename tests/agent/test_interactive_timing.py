from types import SimpleNamespace
import threading

import pytest
from agent import turn_api_call
from agent.interactive_timing import emit_stage


def call(agent):
    request = {"model": "owner-selected", "messages": [{"role": "user", "content": "QA"}]}
    result = turn_api_call.perform_api_call(
        agent, api_kwargs=request, _original_api_kwargs=request, _llm_middleware_trace=[],
        _moa_prepared_request=None, _retry=SimpleNamespace(), thinking_spinner=None,
        retry_count=0, api_call_count=1, api_request_id="qa", effective_task_id="qa",
        turn_id="qa", interrupted=False)
    return request, result


def test_native_api_boundary_orders_content_free_stages_without_changing_request(monkeypatch):
    from hermes_cli import middleware
    stages, requests = [], []
    event = threading.Event()
    agent = SimpleNamespace(api_mode="chat_completions", session_id="qa", provider="qa",
        platform="tui", model="owner-selected", base_url="", thinking_callback=None,
        _model_request_active=event, _has_pending_redirect=lambda: False,
        _interactive_stage_callback=lambda stage, ts: stages.append((stage, ts)))
    def stream(request, on_first_delta):
        assert event.is_set()
        requests.append(request)
        on_first_delta()
        return {"response": "qa"}
    agent._interruptible_streaming_api_call = stream
    monkeypatch.setattr(turn_api_call, "_should_stream", lambda _: True)
    monkeypatch.setattr(middleware, "run_llm_execution_middleware", lambda request, invoke, **kw: invoke(request))
    request, verdict = call(agent)
    assert requests == [request]
    assert requests[0] is request
    assert request == {"model": "owner-selected", "messages": [{"role": "user", "content": "QA"}]}
    assert verdict.action == "fallthrough" and verdict.response == {"response": "qa"}
    assert [stage for stage, _ in stages] == ["MODEL_REQUEST_START", "MODEL_FIRST_STREAM_DELTA", "MODEL_REQUEST_END"]
    assert stages[0][1] <= stages[1][1] <= stages[2][1]
    assert not event.is_set()


def test_failed_request_records_end_without_inventing_first_token_or_retry(monkeypatch):
    from hermes_cli import middleware
    stages, calls = [], []
    agent = SimpleNamespace(api_mode="chat_completions", session_id="qa", provider="qa", platform="tui",
        model="owner-selected", base_url="", thinking_callback=None, _has_pending_redirect=lambda: False,
        _interactive_stage_callback=lambda stage, ts: stages.append(stage))
    def stream(*args, **kwargs):
        calls.append(1)
        raise TimeoutError("synthetic provider timeout")
    agent._interruptible_streaming_api_call = stream
    monkeypatch.setattr(turn_api_call, "_should_stream", lambda _: True)
    monkeypatch.setattr(middleware, "run_llm_execution_middleware", lambda request, invoke, **kw: invoke(request))
    with pytest.raises(TimeoutError):
        call(agent)
    assert calls == [1]
    assert stages == ["MODEL_REQUEST_START", "MODEL_REQUEST_END"]


def test_broken_or_unrecognized_observer_cannot_change_execution():
    def broken(*args): raise RuntimeError("observer unavailable")
    emit_stage(SimpleNamespace(_interactive_stage_callback=broken), "MODEL_REQUEST_START")
    seen = []
    emit_stage(SimpleNamespace(_interactive_stage_callback=lambda *a: seen.append(a)), "private arbitrary text")
    assert not seen


def test_late_stream_callback_keeps_original_observer_identity(monkeypatch):
    from hermes_cli import middleware
    original, newer, pending = [], [], []
    agent = SimpleNamespace(api_mode="chat_completions", session_id="qa", provider="qa", platform="tui",
        model="owner-selected", base_url="", thinking_callback=None, _has_pending_redirect=lambda: False,
        _interactive_stage_callback=lambda stage, ts: original.append(stage))
    def stream(request, on_first_delta):
        pending.append(on_first_delta)
        return {"response": "qa"}
    agent._interruptible_streaming_api_call = stream
    monkeypatch.setattr(turn_api_call, "_should_stream", lambda _: True)
    monkeypatch.setattr(middleware, "run_llm_execution_middleware", lambda request, invoke, **kw: invoke(request))
    call(agent)
    agent._interactive_stage_callback = lambda stage, ts: newer.append(stage)
    pending[0]()
    assert not newer
    assert original == ["MODEL_REQUEST_START", "MODEL_REQUEST_END", "MODEL_FIRST_STREAM_DELTA"]
