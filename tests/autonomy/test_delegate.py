"""Collaboration loop safety does not require a live teammate process."""

from __future__ import annotations

import pytest
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from agent.autonomy import kernel, store


@pytest.fixture
def home(tmp_path, monkeypatch):
    hermes_home = tmp_path / "profiles" / "nasser"
    hermes_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return hermes_home


def test_delegate_records_waiting_and_rejects_replay(home, monkeypatch):
    started = store.start_work(
        why="ERP workstation latency",
        outcome="find cause and restore acceptable load time",
        done_contract="p95 load < 2s or root cause documented",
        idempotency_key="erp:latency",
        hermes_home=home,
    )
    work_id = started["work"]["id"]

    from hermes_cli.native_execution_scope import dispatch_native_command
    from hermes_state import SessionDB
    monkeypatch.delenv("HERMES_EXECUTION_SCOPE", raising=False)
    assignment = dict(target="sami", goal="diagnose infra path for ERP latency",
        deliverable="evidence of infra vs app cause", scope="read-only diagnosis",
        evidence="timings + service health", work_id=work_id)

    def offline_policy(**kwargs):
        assert kwargs["task"] == "execution_scope"
        payload = json.loads(kwargs["messages"][-1]["content"])
        assert payload["original_instruction"] == {
            "command":"autonomy", "action":"delegate", "arguments":assignment}
        decision = {"objective":assignment["goal"], "permitted":["Read-only peer diagnosis"],
                    "excluded":["Unrelated actions"]}
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(decision)))])
    monkeypatch.setattr("agent.auxiliary_client.call_llm", offline_policy)

    # Keep native process registration and scope validation; replace only the
    # teammate program with a harmless child that checks its durable assignment.
    native_popen = subprocess.Popen
    spawned = []
    def peer_program(argv, **kwargs):
        assert "hermes_cli.main" in argv
        locator = json.loads(kwargs["env"]["HERMES_EXECUTION_SCOPE"])
        spawned.append(locator)
        kwargs["env"] = {**kwargs["env"], "HERMES_HOME":str(home)}
        script = """import json,os
from agent.execution_scope import _inherited_binding
binding=_inherited_binding(json.loads(os.environ['HERMES_EXECUTION_SCOPE']),runtime=None)
source=binding.db.get_scope(binding.scope_id)['source']
assert source['instruction']['scope']=='read-only diagnosis'
assert source['owner_original_instruction']['arguments']['goal']=='diagnose infra path for ERP latency'
binding.db.close()
"""
        return native_popen([sys.executable, "-c", script], **kwargs)
    monkeypatch.setattr(subprocess, "Popen", peer_program)

    def dispatch():
        args = SimpleNamespace(command="autonomy", autonomy_command="delegate", **assignment)
        args.func = lambda _: kernel.delegate(**assignment,
            context="4-6s Sales Workstation load", hermes_home=home)
        return dispatch_native_command(args)

    first = dispatch()
    replay = dispatch()
    assert len(spawned) == 1
    with SessionDB(Path(spawned[0]["db_path"])) as db:
        assert db.get_scope(spawned[0]["scope_id"])["state"] == "closed"
    assert first["ok"] is True
    assert replay["ok"] is False
    assert replay["error"] == "duplicate_delegation"
    assert store.get_work(work_id, home)["state"] == "waiting"


def test_self_delegate_rejected(home):
    result = kernel.delegate(
        target="nasser",
        goal="talk to myself",
        context="",
        deliverable="",
        scope="",
        evidence="",
        work_id="w",
        hermes_home=home,
    )
    assert result["ok"] is False
    assert result["error"] == "self_delegate"
