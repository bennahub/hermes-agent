"""ACP original input is authority; interrupt reconstruction is only context."""

import asyncio
from types import SimpleNamespace

from acp_adapter.server import HermesACPAgent
from acp_adapter.session import SessionState
from tools.owner_task_authority import consume_execution_source


def test_executor_stages_original_not_reconstructed_history(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    seen = []
    actor = SimpleNamespace(session_id="acp-native", _session_db=db)

    def run(**kwargs):
        seen.append((consume_execution_source(actor), kwargs))
        return {"final_response": "ok"}

    actor.run_conversation = run
    state = SessionState("acp-native", actor, history=[{"role": "user", "content": "old echo"}])
    server = HermesACPAgent.__new__(HermesACPAgent)
    loop = asyncio.new_event_loop()
    try:
        server._run_agent_turn(
            state=state, session_id=state.session_id, user_text="old echo then inspect",
            user_content="old echo then inspect", original_instruction="inspect current file",
            conn=None, loop=loop, approval_cb=None, edit_approval_requester=None,
        )
        source, kwargs = seen.pop()
        assert source["instruction"] == "inspect current file"
        assert source["source"] == "acp.prompt"
        assert kwargs["user_message"] == "old echo then inspect"
        assert kwargs["conversation_history"] == state.history
    finally:
        loop.close()
        db.close()


def test_machine_acp_does_not_remint_owner_authority(monkeypatch):
    monkeypatch.setenv("HERMES_EXECUTION_SCOPE", "inherited-locator")
    actor = SimpleNamespace(session_id="machine-acp")
    seen = []
    actor.run_conversation = lambda **kwargs: seen.append(consume_execution_source(actor)) or {}
    state = SessionState("machine-acp", actor)
    loop = asyncio.new_event_loop()
    try:
        HermesACPAgent.__new__(HermesACPAgent)._run_agent_turn(
            state=state, session_id=state.session_id, user_text="new command", user_content="new command",
            original_instruction="new command", conn=None, loop=loop,
            approval_cb=None, edit_approval_requester=None,
        )
        assert seen == [None]
    finally:
        loop.close()


def test_busy_queue_keeps_original_instead_of_interrupt_enrichment():
    state = SessionState("queued-acp", SimpleNamespace(), is_running=True)
    result = HermesACPAgent.__new__(HermesACPAgent)._claim_turn_or_queue(
        state, state.session_id, "historical command plus correction", "historical command plus correction",
        True, original_instruction="current correction",
    )
    assert "Queued" in result
    assert state.queued_prompts == ["current correction"]


async def _capture_native_prompt(monkeypatch, original):
    from unittest.mock import MagicMock

    from acp.schema import TextContentBlock
    from acp_adapter.session import SessionManager

    server = HermesACPAgent(session_manager=SessionManager(agent_factory=lambda: MagicMock()))
    session = await server.new_session(cwd="/tmp")
    captured = []
    monkeypatch.setattr(server, "_rewrite_prompt_for_interrupt", lambda *args: ("historical echo + correction",) * 2)

    def run(**kwargs):
        captured.append(kwargs["original_instruction"])
        return {"final_response": "", "messages": []}

    monkeypatch.setattr(server, "_run_agent_turn", run)
    await server.prompt(prompt=[TextContentBlock(type="text", text=original)], session_id=session.session_id)
    return captured


def test_native_prompt_captures_input_before_interrupt_rewrite(monkeypatch):
    assert asyncio.run(_capture_native_prompt(monkeypatch, "inspect current file")) == ["inspect current file"]
