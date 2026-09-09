"""Native child dispatch captures durable scope before the parent can finish."""
import json
from types import SimpleNamespace

import pytest

from agent import execution_scope as scope
from hermes_state import SessionDB


@pytest.fixture
def parent_scope(tmp_path, monkeypatch):
    home = tmp_path / ".hermes" / "profiles" / "sender"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    with SessionDB(home / "state.db") as db:
        record = db.create_or_get_scope(
            "owner:source:1", {"kind": "owner", "instruction": "Review the financial definitions."},
            {"allowed": ["review financial definitions"], "prohibited": ["unrelated shell commands"]},
        )
        binding = scope.Binding(db, record["scope_id"])
        monkeypatch.setattr(scope, "capture_binding", lambda: binding)
        yield home, db, binding, record


def test_delegate_constructs_immutable_scope_before_background_start(parent_scope, monkeypatch):
    import run_agent
    from tools import delegate_tool
    from tools import delegate_tool_config

    home, db, binding, parent = parent_scope
    class Child:
        def __init__(self, **kwargs):
            self.valid_tool_names = {"terminal"}
            self.session_id = "child-session"
    monkeypatch.setattr(run_agent, "AIAgent", Child)
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {})
    monkeypatch.setattr(delegate_tool_config, "_load_config", lambda: {})
    agent = SimpleNamespace(
        enabled_toolsets=["terminal"], valid_tool_names={"terminal"}, model="test-model",
        provider="test-provider", base_url="http://example.invalid", api_mode="chat_completions",
        platform="cli", session_id="parent-session",
    )
    child = delegate_tool._build_child_agent(
        task_index=0, goal="Review the MET definitions", context=None, toolsets=None,
        model=None, max_iterations=3, task_count=1, parent_agent=agent,
    )
    locator = child._pending_inherited_execution_scope
    inherited = db.get_scope(locator["scope_id"])
    assert inherited["scope"] == {**parent["scope"], "assignments": [inherited["source"]["instruction"]]}
    assert inherited["source"]["instruction"] == "Review the MET definitions"
    assert inherited["source"]["assignment_id"] == "delegate:" + child._subagent_id
    db.close_scope(binding.scope_id)
    assert db.get_scope(locator["scope_id"])["state"] == "active"


def test_local_message_carries_exact_native_event_scope(parent_scope, monkeypatch):
    from tools import bot_mode_dm as dm, bot_mode_probe as probe

    home, db, binding, parent = parent_scope
    root = home.parent.parent
    recipient = root / "profiles" / "recipient"
    recipient.mkdir()
    monkeypatch.setattr(probe, "is_bot_mode_managed", lambda path: True)
    monkeypatch.setattr(probe, "_hermes_root", lambda path: root)
    monkeypatch.setattr(probe, "_profile_name", lambda path: "sender")
    monkeypatch.setattr(probe, "_roster", lambda path: [("sender", home), ("recipient", recipient)])
    monkeypatch.setattr(probe, "_peers", lambda path: {})
    delivered = []
    def dispatch(recipient, content, *, display_metadata):
        delivered.append((content, display_metadata))
        return True
    agent = SimpleNamespace(
        _session_db=db, session_id="parent-session", _bot_mode_all_sessions=True,
        _message_agent_dispatcher=dispatch,
    )
    result = json.loads(dm.message_agent_tool("recipient", "Review MET definitions", agent=agent))
    assert result["status"] == "sent"
    content, metadata = delivered[0]
    inherited = db.get_scope(metadata["execution_scope"]["scope_id"])
    assert inherited["scope"] == {**parent["scope"], "assignments": [inherited["source"]["instruction"]]}
    assert inherited["source"]["instruction"] == content
    assert inherited["source"]["assignment_id"] == "a2a:" + metadata["projection"]["event_id"]
    # Same words are distinct native sends, not a content-deduplication key.
    assert json.loads(dm.message_agent_tool("recipient", "Review MET definitions", agent=agent))["status"] == "sent"
    assert delivered[1][1]["execution_scope"]["scope_id"] != metadata["execution_scope"]["scope_id"]

    private = root / "scope-dm"
    private.mkdir(mode=0o700)
    monkeypatch.setattr(dm, "_dm_dir", lambda: private)
    monkeypatch.setattr(dm, "cleanup_bot_dm_cache", lambda: 0)
    path = dm._write_dm_file(content, display_metadata=metadata, projection_root=str(root))
    monkeypatch.setenv("HERMES_HOME", str(recipient))
    monkeypatch.setattr(probe, "_profile_name", lambda path: "recipient")
    restored = dm.read_dm_projection(path, content)
    assert restored["execution_scope"] == metadata["execution_scope"]
    assert dm.read_dm_projection(path, content + " extra unauthorized instruction") is None
    dm._unlink_dm_file(path)
