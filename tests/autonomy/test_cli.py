"""CLI handlers talk to the kernel without a second backend."""

from __future__ import annotations

import argparse
import json

import pytest

from hermes_cli.autonomy_cmd import cmd_autonomy


@pytest.fixture
def home(tmp_path, monkeypatch):
    hermes_home = tmp_path / "profiles" / "abu-saud"
    hermes_home.mkdir(parents=True)
    (hermes_home / "SOUL.md").write_text("# Abu Saud — Chief of Staff\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return hermes_home


def _run(command, **kwargs):
    args = argparse.Namespace(autonomy_command=command, **kwargs)
    with pytest.raises(SystemExit) as exited:
        cmd_autonomy(args)
    return exited.value.code


def test_status_and_work_lifecycle(home, capsys):
    _run("status")
    status = json.loads(capsys.readouterr().out)
    assert status["profile"] == "abu-saud"
    assert status["enabled"] is False

    _run(
        "work-start",
        why="blocked launch dependency",
        outcome="unblock or escalate the real decision",
        done_contract="blocker cleared or Owner asked once",
        idempotency_key="cos:launch-block",
        objective="launch blocker",
    )
    started = json.loads(capsys.readouterr().out)
    assert started["created"] is True
    work_id = started["work"]["id"]

    _run(
        "work-start",
        why="blocked launch dependency",
        outcome="unblock or escalate the real decision",
        done_contract="blocker cleared or Owner asked once",
        idempotency_key="cos:launch-block",
        objective="launch blocker",
    )
    replay = json.loads(capsys.readouterr().out)
    assert replay["created"] is False

    _run("work-complete", work_id=work_id, result="Owner decision recorded", quiet=False)
    completed = json.loads(capsys.readouterr().out)
    assert completed["state"] == "completed"

    _run("metrics")
    metrics = json.loads(capsys.readouterr().out)
    assert metrics["metrics"]["useful_autonomous_completions"] >= 1


def test_event_and_tick_claim_cli(home, capsys):
    _run("event-claim", key="jira:BWM-802:blocked")
    first = json.loads(capsys.readouterr().out)
    _run("event-claim", key="jira:BWM-802:blocked")
    second = json.loads(capsys.readouterr().out)
    assert first["claimed"] is True
    assert second["duplicate"] is True

    _run("tick-claim", job_id="autonomy-observe", scheduled_at="2026-09-04T08")
    first_tick = json.loads(capsys.readouterr().out)
    assert first_tick["claimed"] is True and first_tick["duplicate"] is False
    _run("tick-claim", job_id="autonomy-observe", scheduled_at="2026-09-04T08")
    tick = json.loads(capsys.readouterr().out)
    assert tick["duplicate"] is True


def test_noop_prints_silent(home, capsys):
    _run("noop")
    assert capsys.readouterr().out.strip() == "[SILENT]"
