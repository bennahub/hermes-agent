"""A quiet/error tick must not erase a routine's last useful state."""

from unittest.mock import patch

import pytest


def test_real_scheduler_retains_meaningful_context_through_quiet_restart(tmp_path, monkeypatch):
    from cron.jobs import create_job, use_cron_store
    from cron.scheduler import _build_job_prompt, _run_one_job_body

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with use_cron_store(tmp_path):
        job = create_job(prompt="Report only material changes", schedule="every 3h", context_from="self", deliver="local")
        results = [
            (True, "# Full execution trace", "Disk is 72%; alert already reported", None),
            (True, "# Cron run\n**Status:** no_change (agent run suppressed)", "[SILENT]", None),
            (False, "# Failed execution", "", "temporary provider outage"),
        ]
        for result in results:
            with patch("cron.scheduler.run_job", return_value=result), patch("cron.scheduler._deliver_result", return_value=None):
                assert _run_one_job_body(job)
        # Re-read through the real store/prompt path, as the next process does.
        from cron.jobs import get_job
        prompt = _build_job_prompt(get_job(job["id"]))
        assert "Disk is 72%; alert already reported" in prompt
        assert "Full execution trace" not in prompt
        assert "Failed execution" not in prompt


@pytest.mark.parametrize("response", ["Already reported: stable at 72%", "x" * 10000], ids=["short", "large"])
def test_duplicate_unchanged_response_is_not_delivered_twice(tmp_path, monkeypatch, response):
    from cron.jobs import create_job, use_cron_store, read_job_continuity, CONTINUITY_MAX_CHARS
    from cron.scheduler import _run_one_job_body
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with use_cron_store(tmp_path):
        job = create_job(prompt="Monitor", schedule="every 1h", context_from="self", deliver="local")
        with patch("cron.scheduler.run_job", return_value=(True, "# trace", response, None)), patch("cron.scheduler._deliver_result", return_value=None) as delivery:
            assert _run_one_job_body(job)
            assert _run_one_job_body(job)
        assert delivery.call_count == 1
        assert len(read_job_continuity(job["id"])) <= CONTINUITY_MAX_CHARS
        assert len(list((tmp_path / "cron/output" / job["id"]).glob("continuity_*.json"))) == 1


def test_failed_delivery_remains_retryable(tmp_path, monkeypatch):
    from cron.jobs import create_job, use_cron_store, read_job_continuity
    from cron.scheduler import _run_one_job_body
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with use_cron_store(tmp_path):
        job = create_job(prompt="Monitor", schedule="every 1h", context_from="self", deliver="local")
        with patch("cron.scheduler.run_job", return_value=(True, "# trace", "New material finding", None)), patch("cron.scheduler._deliver_result", side_effect=["transport down", None]) as delivery:
            assert _run_one_job_body(job)
            assert read_job_continuity(job["id"]) == ""
            assert _run_one_job_body(job)
        assert delivery.call_count == 2
        assert read_job_continuity(job["id"]) == "New material finding"


def test_continuity_snapshot_survives_history_retention(tmp_path, monkeypatch):
    from cron.jobs import create_job, use_cron_store, save_job_continuity, _prune_job_output
    from cron.scheduler import _build_job_prompt
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with use_cron_store(tmp_path):
        job = create_job(prompt="Monitor", schedule="every 1h", context_from="self")
        save_job_continuity(job["id"], "Last meaningful state remains")
        directory = tmp_path / "cron/output" / job["id"]
        for index in range(60):
            (directory / f"2026-01-{index:02}.md").write_text("[SILENT]")
        assert _prune_job_output(directory, keep=2) == 58
        assert "Last meaningful state remains" in _build_job_prompt(job)


def test_continuity_snapshot_does_not_cross_profile_stores(tmp_path):
    from cron.jobs import use_cron_store, save_job_continuity, read_job_continuity
    (tmp_path / "profiles/first").mkdir(parents=True)
    (tmp_path / "profiles/second").mkdir(parents=True)
    with use_cron_store(tmp_path / "profiles/first"):
        save_job_continuity("abcdef123456", "First profile private routine")
    with use_cron_store(tmp_path / "profiles/second"):
        assert read_job_continuity("abcdef123456") == ""


def test_existing_job_recovers_meaningful_response_from_legacy_history(tmp_path, monkeypatch):
    import os
    from cron.jobs import create_job, use_cron_store
    from cron.scheduler import _build_job_prompt
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with use_cron_store(tmp_path):
        job = create_job(prompt="Monitor", schedule="every 1h", context_from="self")
        directory = tmp_path / "cron/output" / job["id"]
        directory.mkdir()
        old = directory / "old.md"
        old.write_text("# Cron Job: test\n\n## Prompt\n\nDo not recursively retain this instruction.\n\n## Response\n\nLast meaningful legacy response")
        os.utime(old, (1, 1))
        (directory / "quiet.md").write_text("# Cron Job: test\n\n## Response\n\n[SILENT]")
        prompt = _build_job_prompt(job)
        assert "Last meaningful legacy response" in prompt
        assert "Do not recursively retain this instruction" not in prompt
