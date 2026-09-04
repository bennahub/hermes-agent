"""Durable work, idempotency, restart, and collaboration-loop contracts."""

from __future__ import annotations

import pytest

from agent.autonomy import store


@pytest.fixture
def home(tmp_path, monkeypatch):
    hermes_home = tmp_path / "profiles" / "badr"
    hermes_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return hermes_home


def _start(home, key="ci:checkout", **kwargs):
    extra = {k: v for k, v in kwargs.items() if k in {"state", "objective", "refs"}}
    return store.start_work(
        why=kwargs.get("why", "Checkout regression is failing CI"),
        outcome=kwargs.get("outcome", "Identify cause and restore green checkout"),
        done_contract=kwargs.get("done", "CI checkout job is green or root cause documented"),
        idempotency_key=key,
        hermes_home=home,
        **extra,
    )


def test_start_work_is_idempotent(home):
    first = _start(home)
    second = _start(home)
    assert first["created"] is True
    assert second["created"] is False
    assert first["work"]["id"] == second["work"]["id"]


def test_restart_reloads_same_work(home):
    created = _start(home)
    work_id = created["work"]["id"]
    reloaded = store.get_work(work_id, home)
    assert reloaded is not None
    assert reloaded["objective"]
    assert reloaded["state"] == "working"


def test_event_claim_replay_does_not_win_twice(home):
    first = store.claim_signal("event", "gh:run:99:failure", hermes_home=home)
    second = store.claim_signal("event", "gh:run:99:failure", hermes_home=home)
    assert first["claimed"] is True
    assert second["claimed"] is False
    assert second["duplicate"] is True


def test_tick_claim_replay_is_duplicate(home):
    assert store.claim_signal("tick", "autonomy-observe:2026-09-04T14", hermes_home=home)["claimed"]
    assert store.claim_signal("tick", "autonomy-observe:2026-09-04T14", hermes_home=home)["duplicate"]


def test_completed_work_does_not_reopen(home):
    work = _start(home)["work"]
    store.complete_work(work["id"], "Verified green", hermes_home=home)
    reopened = store.update_work(work["id"], state="working", hermes_home=home)
    assert reopened["state"] == "completed"


def test_collaboration_ping_pong_rejected(home):
    first = store.record_collab(
        work_id="w1",
        from_agent="nasser",
        to_agent="sami",
        evidence_hash="e1",
        goal_hash="g1",
        hermes_home=home,
    )
    bounce = store.record_collab(
        work_id="w1",
        from_agent="sami",
        to_agent="nasser",
        evidence_hash="e1",
        goal_hash="g2",
        hermes_home=home,
    )
    assert first["allowed"] is True
    assert bounce["allowed"] is False
    assert bounce["reason"] == "ping_pong"


def test_collaboration_duplicate_and_fanout(home):
    assert store.record_collab(
        work_id="w2", from_agent="abu-saud", to_agent="badr",
        evidence_hash="a", goal_hash="g-badr", hermes_home=home,
    )["allowed"]
    replay = store.record_collab(
        work_id="w2", from_agent="abu-saud", to_agent="badr",
        evidence_hash="b", goal_hash="g-badr", hermes_home=home,
    )
    assert replay["reason"] == "duplicate_delegation"
    store.record_collab(
        work_id="w2", from_agent="abu-saud", to_agent="sami",
        evidence_hash="c", goal_hash="g-sami", hermes_home=home,
    )
    store.record_collab(
        work_id="w2", from_agent="abu-saud", to_agent="faisal",
        evidence_hash="d", goal_hash="g-faisal", hermes_home=home,
    )
    fourth = store.record_collab(
        work_id="w2", from_agent="abu-saud", to_agent="joud",
        evidence_hash="e", goal_hash="g-joud", hermes_home=home,
    )
    assert fourth["allowed"] is False
    assert fourth["reason"] == "fanout_cap"


def test_open_work_cap_drops_instead_of_expanding(home):
    _start(home, key="one")
    _start(home, key="two")
    _start(home, key="three")
    extra = _start(home, key="four")
    assert extra["created"] is True
    assert extra["work"]["state"] == "dropped"


def test_profiles_do_not_share_work_state(tmp_path, monkeypatch):
    badr = tmp_path / "profiles" / "badr"
    sami = tmp_path / "profiles" / "sami"
    badr.mkdir(parents=True)
    sami.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(badr))
    store.start_work(
        why="badr only",
        outcome="badr outcome",
        done_contract="done",
        idempotency_key="shared-looking-key",
        hermes_home=badr,
    )
    assert store.list_work(sami) == []
    assert store.list_work(badr)
