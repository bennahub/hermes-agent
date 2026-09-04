"""Real storage regressions found during independent program revalidation."""

from datetime import datetime, timedelta, timezone

from agent.autonomy import kernel, store

import pytest


def test_idle_observation_gets_another_discovery_turn(tmp_path, monkeypatch):
    now = datetime(2026, 9, 5, 8, tzinfo=timezone.utc)
    monkeypatch.setattr(kernel, "_hermes_now", lambda: now)
    first = kernel.monitor_fingerprint(tmp_path)
    kernel.mark_noop(tmp_path)
    now += timedelta(hours=3)
    assert kernel.monitor_fingerprint(tmp_path) != first


def test_distinct_jira_updates_do_not_collapse_into_one_event(tmp_path):
    first = {"webhookEvent": "jira:issue_updated", "issue": {"key": "BWM-802"}, "timestamp": 100}
    second = {**first, "timestamp": 200}
    assert kernel.webhook_filter(first, hermes_home=tmp_path)["ignore"] is False
    assert kernel.webhook_filter(second, hermes_home=tmp_path)["ignore"] is False
    assert kernel.webhook_filter(second, hermes_home=tmp_path)["ignore"] is True


def test_github_delivery_identity_survives_repeated_run_action():
    first = {"action": "completed", "workflow_run": {"id": 77}, "headers": {"x-github-delivery": "delivery-one"}}
    second = {**first, "headers": {"x-github-delivery": "delivery-two"}}
    assert kernel.event_key(first) != kernel.event_key(second)


def test_admission_rechecks_competing_work_inside_storage(tmp_path, monkeypatch):
    real_start = store.start_work
    def race(**kwargs):
        real_start(why="concurrent event", outcome="bounded", done_contract="done", idempotency_key="competitor", hermes_home=tmp_path)
        return real_start(**kwargs)
    monkeypatch.setattr(store, "start_work", race)
    result = kernel.begin_work(why="observation", outcome="bounded", done_contract="done", idempotency_key="candidate", hermes_home=tmp_path)
    assert result["created"] is False
    assert result["reason"] == "already_working"
    assert store.get_work_by_key("candidate", tmp_path) is None


def test_resume_prompt_includes_durable_completion_and_waiting_context(tmp_path):
    work = store.start_work(why="service issue", outcome="restore", done_contract="p95 below 2 seconds", idempotency_key="latency", hermes_home=tmp_path)["work"]
    store.update_work(work["id"], state="waiting", waiting_reason="awaiting measured replica lag from Sami", refs={"jira": "BWM-805"}, hermes_home=tmp_path)
    prompt = kernel.format_observe_prompt(kernel.observe_context(tmp_path))
    assert "p95 below 2 seconds" in prompt
    assert "awaiting measured replica lag from Sami" in prompt
    assert "BWM-805" in prompt


@pytest.mark.parametrize("target", ["../hamad", "../../escape"])
def test_delegate_rejects_path_targets_before_mirroring_state(tmp_path, target):
    home = tmp_path / "profiles/badr"
    home.mkdir(parents=True)
    result = kernel.delegate(target=target, goal="bounded task", hermes_home=home, send=False)
    assert result["ok"] is False
    assert result["error"] == "invalid_target"
    assert not (tmp_path / "hamad/autonomy").exists()
    assert not (tmp_path / "escape/autonomy").exists()
