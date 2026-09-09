"""Oneshot stages only the explicit command argument as original authority."""

from types import SimpleNamespace

import pytest

from hermes_cli import oneshot
from hermes_state import SessionDB
from tools.owner_task_authority import consume_execution_source


@pytest.mark.parametrize("inherited", [False, True])
def test_oneshot_native_source_and_machine_exclusion(monkeypatch, tmp_path, inherited):
    import run_agent
    import hermes_cli.config
    import hermes_cli.runtime_provider
    import hermes_cli.mcp_startup

    db = SessionDB(tmp_path / "state.db")
    seen = []
    monkeypatch.setattr(hermes_cli.config, "load_config", lambda: {})
    monkeypatch.setattr(oneshot, "_resolve_model_and_provider", lambda *args: oneshot._ModelChoice("offline", None))
    monkeypatch.setattr(hermes_cli.runtime_provider, "resolve_runtime_provider", lambda **kwargs: {})
    monkeypatch.setattr(hermes_cli.mcp_startup, "ensure_mcp_discovery_before_agent_build", lambda **kwargs: None)
    monkeypatch.setattr(oneshot, "_create_session_db_for_oneshot", lambda: db)
    monkeypatch.setattr(oneshot, "_close_agent", lambda *args: None)
    if inherited:
        monkeypatch.setenv("HERMES_EXECUTION_SCOPE", "parent-native-scope")

    def factory(**kwargs):
        assert kwargs["session_db"] is db
        actor = SimpleNamespace(session_id="oneshot-native", _session_db=db)

        def run(prompt):
            seen.append((prompt, consume_execution_source(actor)))
            return {"final_response": "ok"}

        actor.run_conversation = run
        return actor

    monkeypatch.setattr(run_agent, "AIAgent", factory)
    try:
        answer, _ = oneshot._run_agent("inspect current file", toolsets=[], use_config_toolsets=False)
        assert answer == "ok"
        assert seen[0][0] == "inspect current file"
        if inherited:
            assert seen[0][1] is None
        else:
            assert seen[0][1]["instruction"] == "inspect current file"
            assert seen[0][1]["source"] == "cli.oneshot"
    finally:
        db.close()
