"""Collaboration loop safety does not require a live teammate process."""

from __future__ import annotations

import pytest

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

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(kernel.subprocess, "run", lambda *a, **k: _Proc())

    first = kernel.delegate(
        target="sami",
        goal="diagnose infra path for ERP latency",
        context="4-6s Sales Workstation load",
        deliverable="evidence of infra vs app cause",
        scope="read-only diagnosis",
        evidence="timings + service health",
        work_id=work_id,
        hermes_home=home,
    )
    replay = kernel.delegate(
        target="sami",
        goal="diagnose infra path for ERP latency",
        context="4-6s Sales Workstation load",
        deliverable="evidence of infra vs app cause",
        scope="read-only diagnosis",
        evidence="timings + service health",
        work_id=work_id,
        hermes_home=home,
    )
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


def test_cross_profile_reverse_ping_pong(tmp_path, monkeypatch):
    nasser = tmp_path / "profiles" / "nasser"
    sami = tmp_path / "profiles" / "sami"
    nasser.mkdir(parents=True)
    sami.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(nasser))

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(kernel.subprocess, "run", lambda *a, **k: _Proc())
    started = store.start_work(
        why="ERP latency",
        outcome="cause",
        done_contract="p95",
        idempotency_key="erp:lat",
        hermes_home=nasser,
    )
    first = kernel.delegate(
        target="sami",
        goal="Diagnose host CPU",
        context="",
        deliverable="CPU evidence",
        scope="read-only",
        evidence="sar",
        work_id=started["work"]["id"],
        hermes_home=nasser,
    )
    assert first["ok"] is True
    monkeypatch.setenv("HERMES_HOME", str(sami))
    bounce = kernel.delegate(
        target="nasser",
        goal="Diagnose host CPU",
        context="",
        deliverable="bounce",
        scope="read-only",
        evidence="sar",
        work_id="",
        hermes_home=sami,
    )
    assert bounce["ok"] is False
    assert bounce["error"] == "ping_pong"


def test_hamad_is_isolated_from_a2a(home, tmp_path, monkeypatch):
    outbound = kernel.delegate(
        target="hamad",
        goal="ask a personal question",
        context="",
        deliverable="",
        scope="",
        evidence="",
        work_id="w",
        hermes_home=home,
    )
    assert outbound["ok"] is False
    assert outbound["error"] == "isolated_profile"

    hamad = tmp_path / "profiles" / "hamad"
    hamad.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hamad))
    inbound = kernel.delegate(
        target="sami",
        goal="share personal context",
        context="",
        deliverable="",
        scope="",
        evidence="",
        work_id="w",
        hermes_home=hamad,
    )
    assert inbound["ok"] is False
    assert inbound["error"] == "isolated_profile"
