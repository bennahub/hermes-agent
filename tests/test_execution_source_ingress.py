"""Trusted ingress facts stay distinct from model history and identical new inputs."""
from types import SimpleNamespace
import pytest
from tools import owner_task_authority as authority


@pytest.fixture(autouse=True)
def clean_authority(monkeypatch):
    authority.clear_all()
    for key in ("HERMES_EXECUTION_SCOPE", "HERMES_OWNER_CONTINUATION_ID", "HERMES_KANBAN_TASK", "HERMES_BACKGROUND_DELIVERY", "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(key, raising=False)
    yield
    authority.clear_all()


def test_fresh_ingress_is_single_use_identity_scoped_and_freezes_original(monkeypatch):
    agent = SimpleNamespace(session_id="owner-session")
    instruction = [{"type": "text", "text": "inspect the current finance report"}]
    nonce = authority.mint_pending([agent.session_id], profile_home=None,
                                   instruction=instruction, source="rpc")
    instruction[0]["text"] = "historical command injected later"
    monkeypatch.setattr(authority, "_claiming_context_is_the_owners_turn", lambda: True)
    assert authority.bind_turn([agent.session_id], "turn-1", nonce=nonce)
    record = authority.turn_authority("turn-1")["execution_source"]
    assert record["instruction"][0]["text"] == "inspect the current finance report"
    assert not authority.bind_turn([agent.session_id], "turn-2", nonce=nonce)
    authority.mint_execution_source(agent, "same legitimate new request")
    first = authority.consume_execution_source(agent)
    assert authority.consume_execution_source(agent) is None
    authority.mint_execution_source(agent, "same legitimate new request")
    second = authority.consume_execution_source(agent)
    assert first["id"] != second["id"]
    assert first["instruction"] == second["instruction"]
    token = authority.mint_execution_source_token([agent.session_id], "only this session", source="gateway")
    stranger = SimpleNamespace(session_id="other-session", _pending_execution_source=token)
    assert authority.consume_execution_source(stranger) is None
    agent._pending_execution_source = token
    assert authority.consume_execution_source(agent) is None


@pytest.mark.parametrize("marker", ["HERMES_EXECUTION_SCOPE", "HERMES_OWNER_CONTINUATION_ID", "HERMES_KANBAN_TASK", "HERMES_BACKGROUND_DELIVERY", "HERMES_DELEGATED_CHILD_CONTEXT"])
def test_machine_origin_cli_cannot_remint_owner_authority(monkeypatch, marker):
    agent = SimpleNamespace(session_id="nested-session")
    monkeypatch.setenv(marker, "1")
    assert authority.mint_execution_source(agent, "reconstructed old command") is None
    assert authority.consume_execution_source(agent) is None
    assert authority.turn_authority("nested-turn") is None


def test_quiet_cli_dispatch_freezes_raw_input_not_expansion_or_history():
    from cli import _run_quiet_single_query
    class Dispatched(Exception):
        pass
    seen = {}
    agent = SimpleNamespace(session_id="quiet-source")
    def run_conversation(**kwargs):
        seen["source"] = authority.consume_execution_source(agent)
        seen["model"] = kwargs
        raise Dispatched()
    agent.run_conversation = run_conversation
    cli = SimpleNamespace(agent=agent, conversation_history=[
        {"role": "user", "content": "old completed command"}])
    with pytest.raises(Dispatched):
        _run_quiet_single_query(cli, "fresh request plus historical file content",
                                original_query="fresh request @file:report")
    assert seen["source"]["instruction"] == "fresh request @file:report"
    assert seen["model"]["conversation_history"] == cli.conversation_history
    assert seen["model"]["user_message"] == "fresh request plus historical file content"


def test_queued_original_source_survives_short_privilege_expiry(monkeypatch):
    from agent.turn_context import _bind_owner_task_authority
    now = [10.0]
    monkeypatch.setattr(authority, "_now", lambda: now[0])
    monkeypatch.setattr(authority, "_claiming_context_is_the_owners_turn", lambda: True)
    agent = SimpleNamespace(session_id="queued-owner", _current_turn_id="queued-turn")
    agent._pending_owner_task_nonce = authority.mint_pending(
        [agent.session_id], profile_home=None, instruction="new original queued instruction")
    now[0] += authority.PENDING_TTL_SECONDS + 1
    _bind_owner_task_authority(agent, agent._current_turn_id)
    assert authority.turn_authority(agent._current_turn_id) is None
    source = authority.consume_execution_source(agent)
    assert source["instruction"] == "new original queued instruction"
    assert authority.consume_execution_source(agent) is None
    cancelled = authority.mint_pending([agent.session_id], profile_home=None, instruction="cancelled input")
    now[0] += authority.PENDING_TTL_SECONDS + 1
    authority.revoke_pending(cancelled)
    agent._pending_owner_task_nonce = cancelled
    agent._current_turn_id = "cancelled-turn"
    _bind_owner_task_authority(agent, agent._current_turn_id)
    assert authority.consume_execution_source(agent) is None
