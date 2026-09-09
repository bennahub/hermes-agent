"""Attempt identity and bounded recovery through production continuation seams."""
import os
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agent.autonomy import owner_continuity as oc, store
from cron.jobs import list_jobs, update_job, use_cron_store
from hermes_state import SessionDB


def due_work(home):
    db = SessionDB(home / "state.db")
    db.create_session("attempt-source", source="desktop")
    mid = db.append_message("attempt-source", "user", "Deploy and report the verified result.")
    work = oc.register_owner_request(db, "attempt-source", mid, hermes_home=home)
    work = oc.wait(work["id"], until="2020-01-01T00:00:00Z", hermes_home=home)
    return db, work


def jobs_for(home, work):
    with use_cron_store(home):
        return [j for j in list_jobs(include_disabled=True)
                if j.get("name", "").startswith("owner-continuation:" + work["id"] + ":")]


def test_dead_unadmitted_attempt_retries_without_owner_obligation(autonomy_home, monkeypatch):
    db, work = due_work(autonomy_home)
    try:
        def killed(*args, **kwargs):
            raise KeyboardInterrupt("process stopped before admission")
        with pytest.raises(KeyboardInterrupt):
            oc.run_resume(work["id"], 1, hermes_home=autonomy_home, runner=killed)
        crashed = store.get_work(work["id"], autonomy_home)
        nonce = crashed["refs"]["dispatch"]["nonce"]
        assert crashed["refs"]["resume_attempts"] == 1
        store.update_work(work["id"], refs={"heartbeat_at": time.time() - 1000},
                          hermes_home=autonomy_home)
        monkeypatch.setattr("gateway.status._pid_exists", lambda pid: False)
        oc.reconcile(autonomy_home)
        recovered = store.get_work(work["id"], autonomy_home)
        assert recovered["state"] == "waiting"
        assert not recovered["refs"].get("pending_owner_result")
        assert not recovered["refs"].get("owner_obligation")
        assert recovered["refs"]["resume_generation"] == 2
        assert recovered["refs"]["resume_attempts"] == 1
        assert recovered["refs"].get("dispatch") is None
        assert recovered["refs"]["lifecycle"]["attempts"][nonce]["admitted"] is False
        assert oc.reconcile(autonomy_home) == 1
        assert oc.reconcile(autonomy_home) == 0
        assert len(jobs_for(autonomy_home, work)) == 1
    finally:
        db.close()


def test_concurrent_busy_deferral_has_one_successor(autonomy_home):
    db, work = due_work(autonomy_home)
    try:
        # Every contender holds the same production preflight snapshot.
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: oc._defer_busy_resume(work, autonomy_home), range(8)))
        assert sum(result is not None for result in results) == 1
        after = store.get_work(work["id"], autonomy_home)
        assert after["refs"]["resume_generation"] == 2
        assert int(after["refs"].get("resume_attempts") or 0) == 0
        assert not after["refs"].get("owner_obligation")
        assert oc.reconcile(autonomy_home) == 1
        assert oc.reconcile(autonomy_home) == 0
        assert len(jobs_for(autonomy_home, work)) == 1
    finally:
        db.close()


def test_rearm_keeps_old_admission_and_creates_one_fresh_job(autonomy_home):
    db, work = due_work(autonomy_home)
    try:
        store.update_work(work["id"], refs={"dispatch": {
            "nonce": "prior-admitted", "generation": 1, "admitted": True,
            "pid": os.getpid()}, "ever_admitted": True}, hermes_home=autonomy_home)
        proof = autonomy_home / "verified-artifact.txt"
        proof.write_text("A verified prior result.")
        verified = oc.verify(work["id"], file=str(proof), hermes_home=autonomy_home)
        prior_proof = verified["refs"]["verification"]
        assert oc.reconcile(autonomy_home) == 1
        job = jobs_for(autonomy_home, work)[0]
        with use_cron_store(autonomy_home):
            update_job(job["id"], {"enabled": False, "state": "error"})
        assert oc.reconcile(autonomy_home) == 0
        after = store.get_work(work["id"], autonomy_home)
        assert after["refs"]["resume_generation"] == 2
        assert after["refs"].get("dispatch") is None
        assert after["refs"]["lifecycle"]["attempts"]["prior-admitted"]["admitted"] is True
        assert after["refs"].get("verification") is None
        assert after["refs"]["lifecycle"]["verifications"][prior_proof["id"]] == prior_proof
        assert oc.reconcile(autonomy_home) == 1
        for _ in range(3):
            assert oc.reconcile(autonomy_home) == 0
        jobs = jobs_for(autonomy_home, work)
        assert len(jobs) == 2
        assert sum(j["name"].endswith(":2") for j in jobs) == 1
        assert sum(j.get("enabled") is not False for j in jobs) == 1
    finally:
        db.close()


def test_admitted_failure_requires_inspection_and_is_not_replayed(autonomy_home, monkeypatch):
    db, work = due_work(autonomy_home)
    calls = []
    try:
        def admitted_then_failed(*args, **kwargs):
            calls.append(kwargs["env"]["HERMES_OWNER_CONTINUATION_NONCE"])
            holder = "attempt-regression:" + str(os.getpid())
            assert db.try_acquire_session_turn_lease("attempt-source", holder, ttl_seconds=30, patience_s=0)
            try:
                with monkeypatch.context() as env:
                    env.setenv("HERMES_OWNER_CONTINUATION_ID", work["id"])
                    env.setenv("HERMES_OWNER_CONTINUATION_NONCE", calls[-1])
                    agent = SimpleNamespace(_session_db=db, session_id="attempt-source")
                    oc.bind_turn(agent, {})
            finally:
                db.release_session_turn_lease("attempt-source", holder)
            raise RuntimeError("admitted execution outcome is uncertain")
        assert oc.run_resume(work["id"], 1, hermes_home=autonomy_home,
                             runner=admitted_then_failed) == 1
        failed = store.get_work(work["id"], autonomy_home)
        assert failed["refs"]["pending_owner_result"]["state"] == "needs_owner"
        assert failed["refs"]["lifecycle"]["attempts"][calls[0]]["admitted"] is True
        assert oc.deliver_pending(failed, autonomy_home)
        for _ in range(3):
            oc.reconcile(autonomy_home)
            assert oc.run_resume(work["id"], 1, hermes_home=autonomy_home,
                                 runner=admitted_then_failed) == 0
        final = store.get_work(work["id"], autonomy_home)
        assert final["state"] == "needs_owner"
        assert final["refs"]["owner_obligation"]["status"] == "unresolved"
        assert len(calls) == 1
        assert not jobs_for(autonomy_home, work)
    finally:
        db.close()


def test_three_unadmitted_failures_create_one_explicit_restart_obligation(autonomy_home):
    db, work = due_work(autonomy_home)
    calls = []
    try:
        def failed_before_admission(*args, **kwargs):
            calls.append(kwargs["env"]["HERMES_OWNER_CONTINUATION_NONCE"])
            raise RuntimeError("transport never admitted this attempt")
        for generation in (1, 2, 3):
            oc.run_resume(work["id"], generation, hermes_home=autonomy_home,
                          runner=failed_before_admission)
            current = store.get_work(work["id"], autonomy_home)
            if generation < 3:
                assert current["state"] == "waiting"
                assert not current["refs"].get("pending_owner_result")
                assert not current["refs"].get("owner_obligation")
        pending = current["refs"]["pending_owner_result"]
        assert pending["state"] == "needs_owner"
        assert "Reply to continue" in pending["text"]
        assert "Existing effects" not in pending["text"]
        assert "interrupted" not in pending["text"].lower()
        assert oc.deliver_pending(current, autonomy_home)
        delivered = store.get_work(work["id"], autonomy_home)
        obligation = delivered["refs"]["owner_obligation"]
        assert obligation["status"] == "unresolved"
        for _ in range(3):
            oc.reconcile(autonomy_home)
            oc.run_resume(work["id"], 3, hermes_home=autonomy_home,
                          runner=failed_before_admission)
            assert not oc.deliver_pending(store.get_work(work["id"], autonomy_home), autonomy_home)
        final = store.get_work(work["id"], autonomy_home)
        assert final["refs"]["owner_obligation"] == obligation
        assert len(calls) == len(set(calls)) == 3
        assert len(final["refs"]["lifecycle"]["obligations"]) == 1
        assert all(not attempt["admitted"] for attempt in
                   final["refs"]["lifecycle"]["attempts"].values())
        rows = db.get_messages("attempt-source", include_inactive=True)
        assert len([row for row in rows if row["role"] == "assistant"]) == 1
        assert not jobs_for(autonomy_home, work)
    finally:
        db.close()


@pytest.mark.parametrize("prior_admitted", [False, True])
def test_dead_schedule_ceiling_is_transport_failure_not_uncertain_execution(autonomy_home, prior_admitted):
    from tui_gateway.activity import work_state

    db, work = due_work(autonomy_home)
    try:
        if prior_admitted:
            store.update_work(work["id"], refs={"dispatch": {
                "nonce": "historical-admission", "generation": 1, "admitted": True},
                "ever_admitted": True}, hermes_home=autonomy_home)
            oc.wait(work["id"], until="2020-01-01T00:00:00Z", hermes_home=autonomy_home)
        for _ in range(5):
            oc.reconcile(autonomy_home)
            pending = store.get_work(work["id"], autonomy_home)
            if pending["refs"].get("pending_owner_result"):
                break
            with use_cron_store(autonomy_home):
                for job in jobs_for(autonomy_home, work):
                    if job.get("enabled") is not False:
                        update_job(job["id"], {"enabled": False, "state": "error"})
            oc.reconcile(autonomy_home)
            pending = store.get_work(work["id"], autonomy_home)
            if pending["refs"].get("pending_owner_result"):
                break
            assert work_state(autonomy_home) != "needs_owner"
        assert pending["refs"]["outcome_kind"] == "transport_exhausted"
        assert not pending["refs"].get("dispatch")
        assert oc.deliver_pending(pending, autonomy_home)
        current = store.get_work(work["id"], autonomy_home)
        assert current["refs"]["owner_obligation"]["kind"] == "transport_exhausted"
        assert work_state(autonomy_home) == "needs_owner"
        assert oc.reconcile(autonomy_home) == 0
        assert not oc.deliver_pending(current, autonomy_home)
    finally:
        db.close()
