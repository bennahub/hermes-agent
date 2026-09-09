"""Due Owner work enters the native claim/dispatch path on the current tick."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from agent.autonomy import owner_continuity as continuity
from cron import jobs, scheduler
from hermes_state import SessionDB


@pytest.fixture
def native_tick(autonomy_home, monkeypatch):
    instant = datetime.now(timezone.utc).replace(microsecond=0)

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    monkeypatch.setattr(continuity, "datetime", FixedClock)
    monkeypatch.setattr(jobs, "_hermes_now", lambda: instant)
    monkeypatch.setattr(scheduler, "_hermes_now", lambda: instant)
    # Keep native locking, reconciliation, due selection, execution recording,
    # in-flight guard and fire claims; exclude unrelated host maintenance.
    for name in ("_maybe_reap_dead_owners", "_maybe_run_worktree_maintenance", "_sweep_mcp_orphans"):
        monkeypatch.setattr(scheduler, name, lambda: None)
    monkeypatch.setattr(scheduler, "_should_yield_tick_to_fresh_gateway", lambda: None)
    db = SessionDB(autonomy_home / "state.db")
    db.create_session("owner-timer", source="desktop")
    mid = db.append_message("owner-timer", "user", "After two minutes check the QA artifact then report the result")
    work = continuity.register_owner_request(db, "owner-timer", mid, hermes_home=autonomy_home)
    continuity.wait(work["id"], until=(instant - timedelta(seconds=2)).isoformat(), hermes_home=autonomy_home)
    dispatched = []

    def fake_effect(job, **kwargs):
        # Native _process_due_job already acquired the actual durable fire claim.
        assert job.get("fire_claim")
        dispatched.append(job["id"])
        assert jobs.mark_job_run(job["id"], success=True)
        scheduler.finish_execution(job["execution_id"], success=True)
        return True

    monkeypatch.setattr(scheduler, "run_one_job", fake_effect)
    with ThreadPoolExecutor(max_workers=1) as pool, jobs.use_cron_store(autonomy_home):
        monkeypatch.setattr(scheduler, "_get_parallel_pool", lambda _: pool)
        try:
            yield dispatched
        finally:
            db.close()


def test_due_owner_work_dispatches_once_in_reconciling_tick(native_tick):
    assert scheduler.tick(verbose=False) == 1
    assert len(native_tick) == 1
    assert scheduler.tick(verbose=False) == 0
    assert len(native_tick) == 1


def test_drain_preserves_due_owner_work_for_next_allowed_tick(native_tick):
    assert scheduler.tick(verbose=False, can_dispatch=lambda: False) == 0
    assert jobs.list_jobs(include_disabled=True) == []
    assert native_tick == []
    assert scheduler.tick(verbose=False, can_dispatch=lambda: True) == 1
    assert len(native_tick) == 1


def test_stopped_owner_work_never_dispatches(native_tick, autonomy_home):
    continuity.cancel_owner_session("owner-timer", hermes_home=autonomy_home)
    assert scheduler.tick(verbose=False) == 0
    assert native_tick == []
    assert jobs.list_jobs(include_disabled=True) == []
