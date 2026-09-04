"""Observation cron install is native and idempotent."""

from __future__ import annotations

import pytest

from agent.autonomy import OBSERVE_JOB_NAME
from agent.autonomy.cron_setup import disable_observation, enable_observation, stagger_schedule
from agent.autonomy.missions import load_state
from agent.autonomy.paths import scripts_dir


@pytest.fixture
def home(tmp_path, monkeypatch):
    hermes_home = tmp_path / "profiles" / "badr"
    hermes_home.mkdir(parents=True)
    (hermes_home / "SOUL.md").write_text("# Badr\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return hermes_home


def test_stagger_is_stable_and_distinct():
    assert stagger_schedule("badr") == stagger_schedule("badr")
    assert stagger_schedule("badr") != stagger_schedule("sami")


def test_enable_creates_single_observe_job(home):
    first = enable_observation(home, schedule="every 3h")
    second = enable_observation(home, schedule="every 3h")
    assert first["job"]["name"] == OBSERVE_JOB_NAME
    assert first["job"]["id"] == second["job"]["id"]
    assert first["state"]["enabled"] is True
    assert (scripts_dir(home) / "autonomy_observe_context.py").is_file()
    assert (scripts_dir(home) / "autonomy_monitor.py").is_file()

    from cron.jobs import list_jobs, use_cron_store

    with use_cron_store(home):
        jobs = [job for job in list_jobs(include_disabled=True) if job.get("name") == OBSERVE_JOB_NAME]
    assert len(jobs) == 1
    assert jobs[0].get("monitor_script")
    assert jobs[0].get("script")


def test_disable_pauses_without_deleting_mission(home):
    enable_observation(home, schedule="every 3h")
    disable_observation(home)
    state = load_state(home)
    assert state["enabled"] is False
    assert (home / "autonomy" / "mission.md").is_file()


def _observe_job(home):
    from cron.jobs import list_jobs, use_cron_store

    with use_cron_store(home):
        jobs = [job for job in list_jobs(include_disabled=True) if job.get("name") == OBSERVE_JOB_NAME]
    assert len(jobs) == 1
    return jobs[0]


def test_codex_profile_default_pins_anthropic(home, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"model": {"provider": "openai-codex", "default": "gpt-5.6-terra"}},
    )
    enable_observation(home, schedule="every 3h")
    job = _observe_job(home)
    assert job.get("provider") == "anthropic"
    assert "codex" not in str(job.get("provider") or "").lower()


def test_enable_omits_model_and_provider_pins(home):
    enable_observation(home, schedule="every 3h")
    job = _observe_job(home)
    assert job.get("model") in (None, "")
    assert job.get("provider") in (None, "")
    assert job.get("reasoning_effort") == "low"


def test_reenable_clears_existing_inference_pins(home):
    created = enable_observation(home, schedule="every 3h")
    from cron.jobs import update_job, use_cron_store

    with use_cron_store(home):
        update_job(created["job"]["id"], {"model": "gpt-5.4", "provider": "openai-codex"})
    assert _observe_job(home).get("provider") == "openai-codex"

    enable_observation(home, schedule="every 3h")
    job = _observe_job(home)
    assert job.get("model") in (None, "")
    assert job.get("provider") in (None, "")
    assert "codex" not in str(job.get("provider") or "").lower()


def test_no_soul_leaves_existing_soul_untouched(home):
    original = (home / "SOUL.md").read_text(encoding="utf-8")
    enable_observation(home, schedule="every 3h", no_soul=True)
    assert (home / "SOUL.md").read_text(encoding="utf-8") == original
    assert (home / "autonomy" / "mission.md").is_file()
