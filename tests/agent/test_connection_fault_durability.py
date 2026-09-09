"""A broken Connection must survive the process that observed it.

Round 1 gave the fault a structural descriptor and one canonical store. It still rode only the live
``message.complete`` frame and ``session.resume``'s in-memory inflight snapshot — both of which die
with the process — so the failed turn persisted NO history row at all. After a relaunch the Owner
saw their own prompt, no answer, and nothing to press. These pin the durable half:

* the failed turn writes its own assistant row carrying the descriptor structurally;
* both history projections republish it under the SAME key the live frame uses;
* the exception path carries it too, instead of erasing it and offering a Retry that cannot work;
* the second owner-facing REST surface reads the same canonical state as the first.
"""

from __future__ import annotations

import json
import threading
import types

import pytest

from agent import connection_health as ch
from agent.error_classifier import ClassifiedError, FailoverReason


RAW_401 = "HTTP 401: OAuth access token has been revoked."
_LEAK_MARKERS = ("HTTP 401", "OAuth access token", "revoked.", "Error code", "request_id")


@pytest.fixture()
def health_home(tmp_path, monkeypatch):
    monkeypatch.setattr(ch, "health_store_path", lambda: tmp_path / "state" / "connection_health.json")
    return tmp_path


def _auth_classified():
    return ClassifiedError(reason=FailoverReason.auth, status_code=401, provider="anthropic",
                           model="claude-opus-5", retryable=True)


class _RecordingAgent:
    """Just enough agent to observe what the terminal path persists."""

    def __init__(self):
        self.persisted: list = []

    def _persist_session(self, messages, conversation_history=None):
        self.persisted.append(list(messages))


def _failed_turn(messages=None, agent=None):
    from agent.conversation_loop import _connection_failure_result
    return _connection_failure_result(
        classified=_auth_classified(), summary=RAW_401,
        messages=[] if messages is None else messages, api_call_count=17, provider="anthropic",
        base_url="https://api.anthropic.com", model="claude-opus-5", status_code=401,
        agent=agent, conversation_history=None,
    )


# ── P0-3: the failed turn leaves a durable row ──


def test_the_failed_turn_appends_an_assistant_row(health_home):
    """Without this the turn persisted nothing: prompt, no answer, no affordance, forever."""
    messages = [{"role": "user", "content": "اربط جيرا وأنشئ التذكرة"}]
    _failed_turn(messages)
    assert len(messages) == 2
    row = messages[-1]
    assert row["role"] == "assistant"
    assert row["content"] and row["content"] != RAW_401


def test_the_persisted_row_carries_the_descriptor_structurally(health_home):
    messages: list = []
    _failed_turn(messages)
    fault = messages[-1]["display_metadata"]["connection"]
    assert fault["reason_code"] == ch.REASON_REVOKED
    assert fault["owner_action"] == ch.ACTION_REAUTHORIZE
    assert fault["retryable"] is False
    assert fault["connection_id"]


def test_the_persisted_row_never_carries_provider_prose(health_home):
    messages: list = []
    _failed_turn(messages)
    serialized = json.dumps(messages[-1], ensure_ascii=False)
    for marker in _LEAK_MARKERS:
        assert marker not in serialized, f"provider text {marker!r} reached the durable transcript"


def test_the_owner_bubble_invariant_holds(health_home):
    """One Owner message per send: the failure adds an ASSISTANT row, never a second user turn."""
    messages = [{"role": "user", "content": "اربط جيرا"}]
    _failed_turn(messages)
    assert [m["role"] for m in messages] == ["user", "assistant"]


def test_the_row_is_flushed_because_the_callers_already_persisted(health_home):
    """Both terminal callers flush BEFORE building the result, so the append needs its own flush."""
    agent = _RecordingAgent()
    _failed_turn([{"role": "user", "content": "hi"}], agent=agent)
    assert agent.persisted, "the connection-failure row was never flushed"
    assert agent.persisted[-1][-1]["role"] == "assistant"


def test_a_persist_failure_never_turns_the_handled_fault_into_a_crash(health_home):
    class _Broken(_RecordingAgent):
        def _persist_session(self, messages, conversation_history=None):
            raise RuntimeError("state.db is read-only")

    result = _failed_turn([{"role": "user", "content": "hi"}], agent=_Broken())
    assert result["needs_owner"] is True and result["failure_retryable"] is False


# ── P0-3: both history projections republish it under one key ──


def _durable_row(health_home):
    messages: list = []
    _failed_turn(messages)
    return messages[-1]


def test_rest_history_republishes_the_descriptor(health_home):
    """REST cold launch: /api/sessions/{id}/messages projects rows through ``display_row``."""
    from agent.message_projection import display_row
    projected = display_row({**_durable_row(health_home), "id": 7})
    assert projected["connection"]["reason_code"] == ch.REASON_REVOKED
    # ``display_row`` only stamps display_kind to hide a row; the failure must stay visible.
    assert projected.get("display_kind") != "hidden"


def test_gateway_history_republishes_the_descriptor(health_home):
    """Cold launch over the socket: the same key name as the live ``message.complete`` frame."""
    from tui_gateway import server
    row = _durable_row(health_home)
    projected = server._history_to_messages([{**row, "_row_id": 7}])
    assert len(projected) == 1
    assert projected[0]["connection"]["reason_code"] == ch.REASON_REVOKED
    assert projected[0]["role"] == "assistant"


def test_an_ordinary_row_carries_no_connection_key(health_home):
    from agent.message_projection import display_row
    from tui_gateway import server
    ordinary = {"role": "assistant", "content": "تم", "id": 3}
    assert "connection" not in display_row(ordinary)
    assert "connection" not in server._history_to_messages([{"role": "assistant", "content": "تم"}])[0]


def test_a_sidecar_without_a_reason_code_is_not_a_renderable_fault(health_home):
    """``reason_code`` is what a client localizes from; without it there is nothing to render."""
    from agent.message_projection import connection_fault
    assert connection_fault({"connection": {"provider": "anthropic"}}) is None
    assert connection_fault({"connection": "not-a-dict"}) is None
    assert connection_fault(None) is None


def test_the_republished_fields_are_exactly_the_descriptor(health_home):
    """A strict allow-list: a sidecar that grew a key cannot smuggle it out under this name."""
    from dataclasses import fields as dataclass_fields
    from agent.message_projection import _CONNECTION_FAULT_FIELDS, connection_fault
    assert set(_CONNECTION_FAULT_FIELDS) == {f.name for f in dataclass_fields(ch.ConnectionBlock)}
    block = ch.build_connection_block(provider="anthropic", reason_code=ch.REASON_REVOKED).to_dict()
    projected = connection_fault({"connection": {**block, "internal_note": "/Users/abdulrahman/x"}})
    assert "internal_note" not in projected


# ── P1-5: the exception path carries the descriptor too ──


def _gateway_session(agent):
    return {
        "agent": agent, "session_key": "session-key", "history": [],
        "history_lock": threading.Lock(), "history_version": 0, "running": False,
        "cols": 80, "inflight_turn": None,
    }


class _Revoked(Exception):
    status_code = 401

    def __str__(self):
        return RAW_401


@pytest.fixture()
def gateway(monkeypatch, tmp_path):
    from tui_gateway import server
    captured: list = []
    monkeypatch.setattr(server, "_emit",
                        lambda event, sid, payload=None: captured.append((event, sid, payload)))
    monkeypatch.setattr(server, "_get_usage", lambda agent: {})
    monkeypatch.setattr(server, "_retire_turn_marker", lambda *a, **k: None)
    monkeypatch.setattr(ch, "health_store_path", lambda: tmp_path / "state" / "connection_health.json")
    return server, captured


def _emit_exception_turn(gateway):
    server, captured = gateway
    agent = types.SimpleNamespace(provider="anthropic", model="claude-opus-5",
                                  base_url="https://api.anthropic.com")
    session = _gateway_session(agent)
    server._emit_terminal_turn_error("sid", session, _Revoked())
    return session, [p for event, _sid, p in captured if event == "message.complete"][0]


def test_the_exception_frame_carries_the_connection_descriptor(gateway):
    """This frame becomes a notice row with a Retry button; it must say what actually broke."""
    _session, payload = _emit_exception_turn(gateway)
    assert payload["connection"]["reason_code"] == ch.REASON_REVOKED
    assert payload["needs_owner"] is True
    assert payload["failure_retryable"] is False


def test_the_exception_frame_pins_retry_as_impossible(gateway):
    """A supplied surface claiming a retry can work is overruled once the fault is Owner-gated.

    Callers may hand this path their own ``error_surface`` (``methods_prompt`` does), and the shared
    classifier can call a 401 retryable because Hermes may rotate to another credential. By the time
    the turn is terminal that has been tried and lost, so the frame must not tell a client a bare
    retry can succeed — that is the button the Owner pressed against a dead grant.
    """
    server, captured = gateway
    session = _gateway_session(types.SimpleNamespace(provider="anthropic", model="claude-opus-5",
                                                     base_url="https://api.anthropic.com"))
    server._emit_terminal_turn_error(
        "sid", session, _Revoked(),
        error_surface={"layer": "auth", "code": "auth", "retryable": True})
    payload = [p for event, _sid, p in captured if event == "message.complete"][0]
    assert payload["error_surface"]["layer"] == "auth"
    assert payload["error_surface"]["retryable"] is False
    # And the retained snapshot replays the corrected verdict, not the optimistic one.
    assert server._inflight_snapshot(session)["error_surface"]["retryable"] is False


def test_the_exception_frame_text_is_never_the_provider_401(gateway):
    _session, payload = _emit_exception_turn(gateway)
    assert RAW_401 not in str(payload["text"])
    assert "OAuth access token" not in str(payload.get("rendered") or "")


def test_the_exception_path_records_the_fault(gateway):
    """An observation is an observation however the turn died."""
    _emit_exception_turn(gateway)
    assert ch.observed_fault("claude-code") or ch.observed_fault("anthropic")


def test_the_retained_snapshot_replays_the_descriptor(gateway):
    server, _captured = gateway
    session, _payload = _emit_exception_turn(gateway)
    snapshot = server._inflight_snapshot(session)
    assert snapshot["connection"]["reason_code"] == ch.REASON_REVOKED
    assert snapshot["error_surface"]["retryable"] is False


def test_a_retained_descriptor_is_not_erased_by_the_error_path(gateway):
    """The concrete regression: ``_fail_inflight_turn(..., connection=None)`` popped it."""
    server, _captured = gateway
    session = _gateway_session(types.SimpleNamespace(provider="anthropic", model="claude-opus-5"))
    block = ch.build_connection_block(provider="anthropic", reason_code=ch.REASON_REVOKED).to_dict()
    with session["history_lock"]:
        server._fail_inflight_turn(session, "first failure", connection=block)
    server._emit_terminal_turn_error("sid", session, RuntimeError("something else broke"))
    assert session["inflight_turn"]["connection"]["reason_code"] == ch.REASON_REVOKED


def test_an_ordinary_gateway_crash_gets_no_connection_descriptor(gateway):
    """A gateway bug whose message happens to mention 401 is not a broken Connection."""
    server, captured = gateway
    session = _gateway_session(types.SimpleNamespace(provider="anthropic", model="claude-opus-5"))
    server._emit_terminal_turn_error("sid", session, RuntimeError("index 401 out of range"))
    payload = [p for event, _sid, p in captured if event == "message.complete"][0]
    assert "connection" not in payload
    assert "needs_owner" not in payload
