"""A native child-event turn owns its source while cron is only a fallback."""

from types import SimpleNamespace
import time

import pytest

from hermes_state import SessionDB
from agent.autonomy import owner_continuity as continuity, store


def waiting_child(home, *, signal=True):
    db = SessionDB(home / "state.db")
    db.create_session("owner-source", source="desktop")
    mid = db.append_message(
        "owner-source",
        "user",
        "Complete the entire QA task and return the verified final result without another Owner prompt.",
    )
    work = continuity.register_owner_request(db, "owner-source", mid, hermes_home=home)
    continuity.wait(
        work["id"],
        child="native-child",
        deadline="2099-01-01T00:00:00Z" if signal else "2020-01-01T00:00:00Z",
        hermes_home=home,
    )
    if signal:
        continuity.signal_event(work["id"], "native-child", hermes_home=home)
    return db, store.get_work(work["id"], home)


@pytest.mark.parametrize(
    "lease_case",
    ["active", "compressed", "foreign", "expired", "dead", "active_dead_dispatch"],
)
def test_reconcile_defers_due_child_while_native_source_turn_is_active(
    autonomy_home, lease_case
):
    db, work = waiting_child(autonomy_home)
    try:
        sid = "owner-source"
        holder = "native-event-worker"
        if lease_case == "compressed":
            db.end_session(sid, "compression")
            db.create_session("compressed-tip", source="desktop", parent_session_id=sid)
            sid = "compressed-tip"
        elif lease_case == "foreign":
            sid = "foreign-source"
            db.create_session(sid, source="desktop")
        elif lease_case == "dead":
            holder = "pid=999999999:turn=dead:platform=desktop"
        assert db.try_acquire_session_turn_lease(
            sid, holder, ttl_seconds=300, patience_s=0
        )
        if lease_case == "expired":
            with db._conn:
                db._conn.execute(
                    "UPDATE session_turn_leases SET expires_at=?", (time.time() - 1,)
                )
        busy = lease_case in {"active", "compressed", "active_dead_dispatch"}
        if lease_case == "active_dead_dispatch":
            store.update_work(
                work["id"],
                state="working",
                refs={
                    "heartbeat_at": time.time() - 1000,
                    "dispatch": {"pid": 999999999, "process_started_at": 1,
                                 "generation": 1, "nonce": "dead-attempt"},
                },
                hermes_home=autonomy_home,
            )
        assert continuity.reconcile(autonomy_home) == (0 if busy else 1)
        current = store.get_work(work["id"], autonomy_home)
        assert current["state"] == (
            "working" if lease_case == "active_dead_dispatch" else "waiting"
        )
        assert bool(current["refs"].get("resume_job")) is (not busy)
        assert not current["refs"].get("pending_owner_result")
        if busy:
            if lease_case == "active_dead_dispatch":
                continuity._recover_orphan(current, autonomy_home)
                assert not store.get_work(work["id"], autonomy_home)["refs"].get(
                    "pending_owner_result"
                )
            db.release_session_turn_lease(sid, holder)
            if lease_case == "active_dead_dispatch":
                continuity._recover_orphan(current, autonomy_home)
                recovered = store.get_work(work["id"], autonomy_home)
                assert recovered["state"] == "waiting"
                assert not recovered["refs"].get("pending_owner_result")
            else:
                assert continuity.reconcile(autonomy_home) == 1
                assert store.get_work(work["id"], autonomy_home)["refs"]["resume_job"]
    finally:
        db.release_session_turn_lease("owner-source", "native-event-worker")
        db.close()


@pytest.mark.parametrize(
    "race",
    [
        "held",
        "wake_signal_during_defer",
        "after_claim",
        "before_cli_failure",
        "failure_commit_race",
        "new_wait",
        "stop",
        "verified_final",
        "admitted_failure",
    ],
)
def test_already_queued_cron_cannot_poison_active_native_event_work(
    autonomy_home, monkeypatch, race
):
    db, work = waiting_child(autonomy_home, signal=race != "wake_signal_during_defer")
    calls = []
    try:
        # Native fallback existed before the event worker acquired its real lease.
        assert continuity.reconcile(autonomy_home) == 1

        def acquire():
            assert db.try_acquire_session_turn_lease(
                "owner-source", "native-event-worker", ttl_seconds=300, patience_s=0
            )

        if race in {"held", "wake_signal_during_defer"}:
            acquire()
            if race == "wake_signal_during_defer":
                update = store.update_work
                signalled = []

                def signal_during_cas(*args, **kwargs):
                    if not signalled and kwargs.get("refs", {}).get(
                        "resume_generation"
                    ):
                        signalled.append(True)
                        continuity.signal_event(
                            work["id"],
                            "native-child",
                            payload={"native": "arrived"},
                            hermes_home=autonomy_home,
                        )
                    return update(*args, **kwargs)

                monkeypatch.setattr(store, "update_work", signal_during_cas)
        elif race == "after_claim":
            update = store.update_work

            def claim_then_event(*args, **kwargs):
                result = update(*args, **kwargs)
                if kwargs.get("state") == "working" and kwargs.get("refs", {}).get(
                    "dispatch"
                ):
                    acquire()
                return result

            monkeypatch.setattr(store, "update_work", claim_then_event)
        before = store.get_work(work["id"], autonomy_home)

        def rejected_cli(*args, **kwargs):
            calls.append(True)
            if race == "failure_commit_race":
                original_acquire = SessionDB.try_acquire_session_turn_lease

                def event_wins(db_arg, sid, holder, **options):
                    if holder.startswith("owner-resume-failure:"):
                        assert original_acquire(
                            db,
                            "owner-source",
                            "native-event-worker",
                            ttl_seconds=300,
                            patience_s=0,
                        )
                    return original_acquire(db_arg, sid, holder, **options)

                monkeypatch.setattr(
                    SessionDB, "try_acquire_session_turn_lease", event_wins
                )
            else:
                acquire()
            if race == "new_wait":
                continuity.wait(
                    work["id"],
                    event="later-authorized-event",
                    deadline="2099-01-01T00:00:00Z",
                    hermes_home=autonomy_home,
                )
            elif race == "stop":
                continuity.cancel_owner_session(
                    "owner-source", hermes_home=autonomy_home
                )
            elif race == "verified_final":
                artifact = autonomy_home / "qa.txt"
                artifact.write_text("verified")
                continuity.verify(
                    work["id"], file=str(artifact), hermes_home=autonomy_home
                )
                continuity.request_finish(
                    work["id"], "Verified QA final", hermes_home=autonomy_home
                )
            elif race == "admitted_failure":
                # Exercise the actual source binding after native turn admission.
                with monkeypatch.context() as scoped:
                    scoped.setenv("HERMES_OWNER_CONTINUATION_ID", work["id"])
                    scoped.setenv(
                        "HERMES_OWNER_CONTINUATION_NONCE",
                        kwargs["env"]["HERMES_OWNER_CONTINUATION_NONCE"],
                    )
                    agent = SimpleNamespace(
                        _session_db=db,
                        session_id="owner-source",
                        _active_session_turn_lease_holder="native-event-worker",
                    )
                    continuity.bind_turn(agent, {})
                assert store.get_work(work["id"], autonomy_home)["refs"]["dispatch"][
                    "admitted"
                ]
            return SimpleNamespace(returncode=1)

        result = continuity.run_resume(
            work["id"],
            before["refs"]["resume_generation"],
            hermes_home=autonomy_home,
            runner=rejected_cli,
        )
        after = store.get_work(work["id"], autonomy_home)
        pending = after["refs"].get("pending_owner_result")
        if race in {
            "held",
            "wake_signal_during_defer",
            "after_claim",
            "before_cli_failure",
            "failure_commit_race",
        }:
            assert not pending, (result, calls, pending)
            assert (
                len(calls)
                == (1 if race in {"before_cli_failure", "failure_commit_race"} else 0)
                and result == 0
            )
            assert after["state"] == before["state"] == "waiting"
            assert after["refs"].get("dispatch") == before["refs"].get("dispatch")
            assert after["refs"].get("resume_attempts") == before["refs"].get(
                "resume_attempts"
            )
            assert (
                after["refs"]["resume_generation"]
                == before["refs"]["resume_generation"] + 1
            )
            assert continuity.resume_due(after)
            if race == "wake_signal_during_defer":
                assert after["refs"]["resume_event"]["payload"] == {"native": "arrived"}
            db.release_session_turn_lease("owner-source", "native-event-worker")
            # A skipped native one-shot can complete; new generation preserves fallback.
            from cron.jobs import use_cron_store, update_job

            with use_cron_store(autonomy_home):
                update_job(
                    before["refs"]["resume_job"]["id"],
                    {"state": "completed", "enabled": False},
                )
            assert continuity.reconcile(autonomy_home) == 1
            fresh = store.get_work(work["id"], autonomy_home)
            assert (
                fresh["refs"]["resume_job"]["id"] != before["refs"]["resume_job"]["id"]
            )
            assert not fresh["refs"].get("pending_owner_result")
        elif race == "new_wait":
            assert (
                result == 0
                and not pending
                and after["refs"]["resume"]["id"] == "later-authorized-event"
            )
        elif race == "stop":
            assert (
                result == 0
                and pending["state"] == "cancelled"
                and after["refs"]["owner_stop"]
            )
        elif race == "verified_final":
            assert (
                result == 0
                and pending["state"] == "completed"
                and pending["text"] == "Verified QA final"
            )
        else:
            assert result == 1 and pending["state"] == "needs_owner"
    finally:
        db.release_session_turn_lease("owner-source", "native-event-worker")
        db.close()


@pytest.mark.parametrize("operation", ["success", "orphan"])
@pytest.mark.parametrize("change", ["new_wait", "stop", "verified_final"])
@pytest.mark.parametrize("release_before_commit", [False, True])
def test_idle_settlement_preserves_new_native_state(
    autonomy_home, monkeypatch, operation, change, release_before_commit
):
    db, work = waiting_child(autonomy_home)
    probe = continuity._source_has_active_turn
    runner_done = []
    won = {}

    def event_after_probe(old, home, **kwargs):
        active = probe(old, home, **kwargs)
        eligible = operation == "orphan" or bool(runner_done)
        if eligible and not won:
            assert not active
            assert db.try_acquire_session_turn_lease(
                "owner-source", "new-native-event", ttl_seconds=300, patience_s=0
            )
            if change == "new_wait":
                continuity.wait(
                    work["id"],
                    child="next-child",
                    deadline="2099-01-01T00:00:00Z",
                    hermes_home=home,
                )
            elif change == "stop":
                continuity.cancel_owner_session("owner-source", hermes_home=home)
            else:
                artifact = home / "result.txt"
                artifact.write_text("verified final")
                continuity.verify(work["id"], file=str(artifact), hermes_home=home)
                continuity.request_finish(
                    work["id"], "Verified final", hermes_home=home
                )
            won.update(store.get_work(work["id"], home))
            if release_before_commit:
                db.release_session_turn_lease("owner-source", "new-native-event")
        return active

    monkeypatch.setattr(continuity, "_source_has_active_turn", event_after_probe)
    try:
        if operation == "success":

            def runner(*args, **kwargs):
                runner_done.append(True)
                return SimpleNamespace(returncode=0)

            continuity.run_resume(
                work["id"],
                work["refs"]["resume_generation"],
                hermes_home=autonomy_home,
                runner=runner,
            )
        else:
            old = store.update_work(
                work["id"],
                state="working",
                refs={
                    "heartbeat_at": time.time() - 1000,
                    "dispatch": {"pid": 999999999, "process_started_at": 1,
                                 "generation": 1, "nonce": "dead-attempt"},
                },
                hermes_home=autonomy_home,
            )
            continuity._recover_orphan(old, autonomy_home)
        assert won
        after = store.get_work(work["id"], autonomy_home)
        assert after["state"] == won["state"]
        assert after["refs"] == won["refs"]
    finally:
        db.release_session_turn_lease("owner-source", "new-native-event")
        db.close()


@pytest.mark.parametrize("interrupted", [False, True])
@pytest.mark.parametrize("own_holder", [False, True])
def test_finish_hook_preserves_native_lease_lifecycle(
    autonomy_home, interrupted, own_holder
):
    db, work = waiting_child(autonomy_home)
    try:
        store.update_work(work["id"], state="working", hermes_home=autonomy_home)
        assert db.try_acquire_session_turn_lease(
            "owner-source", "actual-native-holder", ttl_seconds=300, patience_s=0
        )
        before = tuple(
            db._conn.execute(
                "SELECT holder,expires_at FROM session_turn_leases"
            ).fetchone()
        )
        agent = SimpleNamespace(
            _owner_continuity_work_id=work["id"],
            _session_db=db,
            _active_session_turn_lease_holder="actual-native-holder"
            if own_holder
            else "stale-holder",
        )
        continuity.finish_turn(agent, {"interrupted": interrupted})
        after = store.get_work(work["id"], autonomy_home)
        assert (
            tuple(
                db._conn.execute(
                    "SELECT holder,expires_at FROM session_turn_leases"
                ).fetchone()
            )
            == before
        )
        if not own_holder:
            assert after["state"] == "working" and not after["refs"].get(
                "pending_owner_result"
            )
        elif interrupted:
            assert after["refs"]["pending_owner_result"]["state"] == "needs_owner"
        else:
            assert (
                after["state"] == "waiting"
                and after["refs"]["resume"]["kind"] == "until"
            )
    finally:
        db.release_session_turn_lease("owner-source", "actual-native-holder")
        db.close()
