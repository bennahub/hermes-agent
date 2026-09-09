"""Initiative kernel: events, cooldown, already-working, quiet projection."""

from __future__ import annotations

import pytest

from agent.autonomy import SILENT_TOKEN
from agent.autonomy import kernel, store


@pytest.fixture
def home(tmp_path, monkeypatch):
    hermes_home = tmp_path / "profiles" / "sami"
    hermes_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return hermes_home


def test_github_event_key_is_stable():
    payload = {"action": "completed", "workflow_run": {"id": 77, "conclusion": "failure"}}
    assert kernel.event_key(payload) == "gh:run:77:completed"
    assert kernel.event_key(payload) == kernel.event_key(dict(payload))


def test_webhook_filter_ignores_duplicate_delivery(home):
    payload = {"action": "completed", "workflow_run": {"id": 12}}
    first = kernel.webhook_filter(payload, hermes_home=home)
    second = kernel.webhook_filter(payload, hermes_home=home)
    assert first["ignore"] is False
    assert "Standing Mission" in first["autonomy"]["prompt"]
    assert second["ignore"] is True
    assert second["reason"] == "duplicate_event"


def test_begin_work_blocks_second_initiative(home):
    first = kernel.begin_work(
        why="service 5xx spike",
        outcome="restore error rate",
        done_contract="5xx back to baseline",
        idempotency_key="ops:5xx",
        hermes_home=home,
    )
    second = kernel.begin_work(
        why="unrelated disk warning",
        outcome="clean disk",
        done_contract="disk < 80%",
        idempotency_key="ops:disk",
        hermes_home=home,
    )
    assert first["created"] is True
    assert second["created"] is False
    assert second.get("blocked") is True
    assert second["reason"] == "already_working"


def test_begin_work_same_key_is_duplicate(home):
    kernel.begin_work(
        why="a", outcome="b", done_contract="c",
        idempotency_key="same", hermes_home=home,
    )
    work = store.get_work_by_key("same", home)
    store.complete_work(work["id"], "done", hermes_home=home)
    replay = kernel.begin_work(
        why="a", outcome="b", done_contract="c",
        idempotency_key="same", hermes_home=home,
    )
    assert replay["duplicate"] is True
    assert replay["work"]["state"] == "completed"


def test_noop_sets_cooldown_and_is_silent(home):
    kernel.mark_noop(home)
    ctx = kernel.observe_context(home)
    assert ctx["cooldown_active"] is True
    prompt = kernel.format_observe_prompt(ctx)
    assert SILENT_TOKEN in prompt


def test_monitor_fingerprint_stable_when_idle(home):
    first = kernel.monitor_fingerprint(home)
    second = kernel.monitor_fingerprint(home)
    assert first == second
    assert first.endswith("\n")


def test_monitor_fingerprint_changes_when_work_starts(home):
    before = kernel.monitor_fingerprint(home)
    kernel.begin_work(
        why="outage",
        outcome="restore service",
        done_contract="health checks pass",
        idempotency_key="ops:outage",
        hermes_home=home,
    )
    after = kernel.monitor_fingerprint(home)
    assert before != after


def test_owner_line_is_product_language():
    line = kernel.owner_line("badr", "investigating", "a checkout regression")
    assert line == "Badr is investigating a checkout regression"
    need = kernel.owner_line("abu-saud", "needs_owner", "launch timing decision")
    assert "needs you" in need.lower()


def test_is_silent_accepts_only_the_token():
    assert kernel.is_silent("[SILENT]")
    assert kernel.is_silent("  [SILENT]  ")
    assert not kernel.is_silent("Use [SILENT] when nothing changed")


def test_tick_claim_helper(home):
    first = kernel.claim_tick("autonomy-observe", "2026-09-04T11", hermes_home=home)
    second = kernel.claim_tick("autonomy-observe", "2026-09-04T11", hermes_home=home)
    assert first["claimed"] is True
    assert second["duplicate"] is True
