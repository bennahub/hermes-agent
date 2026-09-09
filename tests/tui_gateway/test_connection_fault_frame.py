"""The Connection-fault descriptor must reach every client, and survive a reconnect.

End-to-end over the real turn pipeline: an auth-classified terminal failure must produce a
``message.complete`` frame whose ``text`` is owner-safe and whose ``connection`` descriptor carries
the structural verdict — and the retained snapshot must replay both, because a disconnected
Connection outlives the socket that reported it.
"""

from __future__ import annotations

import json
import threading
import types

import pytest

from agent import connection_health as ch
from tui_gateway import server


class _InlineThread:
    def __init__(self, target=None, daemon=None, args=(), kwargs=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)

    def is_alive(self):
        return False

    def join(self, timeout=None):
        return None


def _session(agent=None, **extra):
    return {
        "agent": agent if agent is not None else types.SimpleNamespace(),
        "session_key": "session-key", "history": [], "history_lock": threading.Lock(),
        "history_version": 0, "running": False, "attached_images": [], "image_counter": 0,
        "cols": 80, "slash_worker": None, "show_reasoning": False, "tool_progress_mode": "all",
        "inflight_turn": None, **extra,
    }


@pytest.fixture()
def emits(monkeypatch):
    captured: list = []
    monkeypatch.setattr(server, "_emit",
                        lambda event, sid, payload=None: captured.append((event, sid, payload)))
    return captured


@pytest.fixture()
def turn_env(monkeypatch, tmp_path):
    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    monkeypatch.setattr(server, "_wire_callbacks", lambda sid: None)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda sid, session: None)
    monkeypatch.setattr(server, "_session_cwd", lambda session: str(tmp_path))
    monkeypatch.setattr(server, "_register_session_cwd", lambda session: None)
    monkeypatch.setattr(server, "_tts_stream_begin", lambda: None)
    monkeypatch.setattr(server, "_sync_session_key_after_compress", lambda *a, **k: None)
    monkeypatch.setattr(server, "_get_usage", lambda agent: {})
    monkeypatch.setattr(ch, "health_store_path", lambda: tmp_path / "state" / "connection_health.json")


def _events(captured, name):
    return [payload for event, _sid, payload in captured if event == name]


RAW_401 = "HTTP 401: OAuth access token has been revoked."


def _revoked_agent():
    """An agent whose turn ends in the exact failure the Owner saw on Mac."""
    from agent.conversation_loop import _connection_failure_result
    from agent.error_classifier import ClassifiedError, FailoverReason

    def run(*a, **k):
        return _connection_failure_result(
            classified=ClassifiedError(reason=FailoverReason.auth, status_code=401,
                                       provider="anthropic", model="claude-opus-5", retryable=True),
            summary=RAW_401, messages=[], api_call_count=1, provider="anthropic",
            base_url="https://api.anthropic.com", model="claude-opus-5", status_code=401,
        )

    return types.SimpleNamespace(session_id="session-key", provider="anthropic",
                                 model="claude-opus-5", run_conversation=run,
                                 clear_interrupt=lambda: None)


def _run_failed_turn(emits, prompt="اربط جيرا وأنشئ التذكرة"):
    session = _session(agent=_revoked_agent(), running=True)
    server._start_inflight_turn(session, prompt)
    server._run_prompt_submit("rid", "sid", session, prompt)
    return session, _events(emits, "message.complete")[0]


def test_frame_carries_the_structured_connection_descriptor(emits, turn_env):
    _session_, payload = _run_failed_turn(emits)
    assert payload["connection"]["reason_code"] == ch.REASON_REVOKED
    assert payload["connection"]["owner_action"] == ch.ACTION_REAUTHORIZE
    assert payload["connection"]["retryable"] is False
    assert payload["connection"]["connection_id"]
    assert payload["failure_reason"] == "auth"
    assert payload["needs_owner"] is True


def test_frame_text_is_never_the_raw_provider_401(emits, turn_env):
    """The defect itself: this string was delivered to the Owner as an ordinary agent answer."""
    _session_, payload = _run_failed_turn(emits)
    for field in ("text", "rendered"):
        assert RAW_401 not in str(payload.get(field) or "")
        assert "OAuth access token" not in str(payload.get(field) or "")
    # The raw text survives only in the diagnostic ``error`` field, which clients render as a
    # failure cause, never as the assistant's reply.
    assert payload["error"] == RAW_401


def test_frame_reports_the_auth_layer_as_non_retryable(emits, turn_env):
    _session_, payload = _run_failed_turn(emits)
    assert payload["error_surface"]["layer"] == "auth"
    assert payload["error_surface"]["retryable"] is False


def test_descriptor_survives_a_reconnect(emits, turn_env):
    """A dropped socket must not downgrade a reconnect card back to raw error prose."""
    session, _payload = _run_failed_turn(emits)
    snapshot = server._inflight_snapshot(session)
    assert snapshot is not None
    assert snapshot["connection"]["reason_code"] == ch.REASON_REVOKED
    assert snapshot["status"] == "error"


def test_a_healthy_turn_carries_no_connection_descriptor(emits, turn_env):
    agent = types.SimpleNamespace(
        session_id="session-key", provider="anthropic", model="claude-opus-5",
        run_conversation=lambda *a, **k: {"final_response": "تم", "completed": True},
        clear_interrupt=lambda: None)
    session = _session(agent=agent, running=True)
    server._start_inflight_turn(session, "مرحبا")
    server._run_prompt_submit("rid", "sid", session, "مرحبا")
    payload = _events(emits, "message.complete")[0]
    assert "connection" not in payload
    assert server._inflight_snapshot(session) is None


def test_a_non_auth_failure_still_carries_no_connection_descriptor(emits, turn_env):
    agent = types.SimpleNamespace(
        session_id="session-key", provider="openrouter", model="test/model",
        run_conversation=lambda *a, **k: {
            "final_response": "", "error": "Rate limit exceeded", "failed": True,
            "failure_reason": "rate_limit"},
        clear_interrupt=lambda: None)
    session = _session(agent=agent, running=True)
    server._start_inflight_turn(session, "go")
    server._run_prompt_submit("rid", "sid", session, "go")
    payload = _events(emits, "message.complete")[0]
    assert "connection" not in payload
    assert payload["error_surface"]["layer"] == "provider"


def test_no_secret_reaches_the_wire(emits, turn_env):
    _session_, payload = _run_failed_turn(emits)
    wire = json.dumps(payload["connection"])
    assert "sk-ant" not in wire and "Bearer" not in wire and "eyJ" not in wire


def test_a_successful_turn_clears_a_recorded_fault(emits, turn_env):
    """The recovery half: after the Owner reconnects, the first good turn must clear the fault —
    otherwise Connections stays stuck on ``needs_auth`` forever."""
    ch.record_fault(ch.build_connection_block(
        provider="anthropic", reason_code=ch.REASON_REVOKED).to_dict())
    recorded = ch.connection_id_for("anthropic")
    assert ch.observed_fault(recorded) is not None

    agent = types.SimpleNamespace(
        session_id="session-key", provider="anthropic", model="claude-opus-5",
        base_url="https://api.anthropic.com",
        run_conversation=lambda *a, **k: {"final_response": "تم", "completed": True},
        clear_interrupt=lambda: None)
    session = _session(agent=agent, running=True)
    server._start_inflight_turn(session, "مرحبا")
    server._run_prompt_submit("rid", "sid", session, "مرحبا")

    assert ch.observed_fault(recorded) is None


def test_a_failed_turn_does_not_clear_the_fault(emits, turn_env):
    _session_, _payload = _run_failed_turn(emits)
    recorded = ch.connection_id_for("anthropic")
    assert ch.observed_fault(recorded) is not None
