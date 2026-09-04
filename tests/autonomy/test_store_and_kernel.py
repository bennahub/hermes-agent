import io
import json
from contextlib import redirect_stdout


def test_event_and_tick_replay_are_idempotent(autonomy_home):
    from agent.autonomy import kernel

    first = kernel.claim_event("evt-1")
    second = kernel.claim_event("evt-1")
    assert first["claimed"] is True
    assert second["duplicate"] is True
    tick = kernel.claim_tick("autonomy-observe", "2026-09-04T12")
    again = kernel.claim_tick("autonomy-observe", "2026-09-04T12")
    assert tick["claimed"] is True
    assert again["duplicate"] is True


def test_same_key_does_not_start_work_twice(autonomy_home):
    from agent.autonomy import kernel

    first = kernel.start_work(
        why="CI red",
        outcome="green build",
        done_contract="main CI green",
        objective="fix checkout CI",
        idempotency_key="ci:checkout:1",
    )
    second = kernel.start_work(
        why="CI red again",
        outcome="green build",
        done_contract="main CI green",
        objective="fix checkout CI",
        idempotency_key="ci:checkout:1",
    )
    assert first["created"] is True
    assert second["duplicate"] is True
    assert first["work"]["id"] == second["work"]["id"]


def test_already_working_blocks_unrelated_expansion(autonomy_home):
    from agent.autonomy import kernel

    first = kernel.start_work(
        why="latency",
        outcome="p95 ok",
        done_contract="p95 < 2s",
        objective="sales latency",
        idempotency_key="erp:lat",
    )
    blocked = kernel.start_work(
        why="unrelated lint",
        outcome="none",
        done_contract="n/a",
        objective="lint",
        idempotency_key="lint:1",
    )
    assert first["created"] is True
    assert blocked["blocked"] is True
    assert blocked["reason"] == "already_working"
    secondary = kernel.start_work(
        why="record separately",
        outcome="later",
        done_contract="own ticket",
        objective="secondary finding",
        idempotency_key="other:1",
        parent_id=first["work"]["id"],
    )
    assert secondary["created"] is True
    assert secondary["work"]["state"] == "observed"
    assert secondary["work"]["parent_id"] == first["work"]["id"]


def test_complete_and_drop_metrics(autonomy_home):
    from agent.autonomy import kernel

    started = kernel.start_work(
        why="502",
        outcome="fix 502",
        done_contract="200",
        objective="fix 502",
        idempotency_key="svc:502",
    )
    kernel.complete_work(started["work"]["id"], "verified 200")
    noise = kernel.start_work(
        why="lint warning",
        outcome="none",
        done_contract="n/a",
        objective="ignore lint",
        idempotency_key="lint:1",
    )
    kernel.drop_work(noise["work"]["id"], "not material")
    metrics = kernel.status_snapshot()["metrics"]
    assert metrics["useful_autonomous_completions"] == 1
    assert metrics["dropped_as_not_worth_doing"] == 1


def test_delegate_rejects_duplicate_ping_pong_and_fanout(autonomy_home):
    from agent.autonomy import kernel, store

    work = kernel.start_work(
        why="ERP latency",
        outcome="cause + fix",
        done_contract="p95 ok",
        objective="latency",
        idempotency_key="erp:lat",
    )
    wid = work["work"]["id"]
    first = kernel.record_delegate(
        work_id=wid,
        to_profile="sami",
        goal="Diagnose host CPU",
        deliverable="CPU/IO evidence",
        evidence="sar + journal excerpt",
    )
    dup = kernel.record_delegate(
        work_id=wid,
        to_profile="sami",
        goal="Diagnose host CPU",
        deliverable="CPU/IO evidence",
        evidence="sar + journal excerpt",
    )
    assert first["allowed"] is True
    assert dup["reason"] == "duplicate_delegation"

    from agent.autonomy.store import _now, profile_slug, transaction

    with transaction() as conn:
        conn.execute(
            """INSERT INTO collab (
                 from_work_id, from_profile, to_profile, goal_hash, created_at
               ) VALUES (?, ?, ?, ?, ?)""",
            ("other", "badr", profile_slug(), first["goal_hash"], _now()),
        )
    ping = kernel.record_delegate(
        work_id=wid,
        to_profile="badr",
        goal="Diagnose host CPU",
        deliverable="bounce",
        evidence="none",
    )
    assert ping["reason"] == "ping_pong"

    kernel.record_delegate(work_id=wid, to_profile="nasser", goal="A", deliverable="a", evidence="a")
    kernel.record_delegate(work_id=wid, to_profile="mishari", goal="B", deliverable="b", evidence="b")
    exploded = kernel.record_delegate(
        work_id=wid, to_profile="fahad", goal="C", deliverable="c", evidence="c"
    )
    assert exploded["reason"] == "fanout_cap"
    assert store.count_collab(wid) <= store.MAX_FANOUT


def test_failed_delegate_send_is_retryable(autonomy_home, monkeypatch):
    from agent.autonomy import kernel

    work = kernel.start_work(
        why="collab",
        outcome="evidence",
        done_contract="returned",
        objective="ask sami",
        idempotency_key="retry-send:1",
    )
    monkeypatch.setattr(kernel, "_send_bot_chat", lambda *_a, **_k: {"sent": False, "error": "timeout"})
    first = kernel.delegate(
        target="sami",
        goal="Need host evidence",
        deliverable="sar",
        evidence="p95",
        work_id=work["work"]["id"],
        send=True,
    )
    assert first["ok"] is False
    assert first["retryable"] is True
    second = kernel.record_delegate(
        work_id=work["work"]["id"],
        to_profile="sami",
        goal="Need host evidence",
        deliverable="sar",
        evidence="p95",
        send=False,
    )
    assert second["allowed"] is True


def test_webhook_filter_silences_duplicate_event(autonomy_home, monkeypatch):
    from agent.autonomy import kernel

    payload = json.dumps({"id": "evt-9", "action": "failed"})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    first = io.StringIO()
    with redirect_stdout(first):
        kernel.emit_webhook_filter()
    assert json.loads(first.getvalue())["ignore"] is False

    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    second = io.StringIO()
    with redirect_stdout(second):
        kernel.emit_webhook_filter()
    assert second.getvalue().strip() == "[SILENT]"


def test_noop_sets_cooldown_and_monitor_is_stable(autonomy_home):
    from agent.autonomy import kernel

    first = kernel.monitor_fingerprint()
    kernel.mark_noop()
    second = kernel.monitor_fingerprint()
    assert "cooldown" in second
    assert first != second
    third = kernel.monitor_fingerprint()
    assert third == second


def test_enable_writes_mission_soul_and_scripts(tmp_path, monkeypatch):
    from pathlib import Path

    from agent.autonomy import kernel
    from agent.autonomy.cron_setup import OBSERVE_SCRIPT

    home = tmp_path / "profiles" / "badr"
    home.mkdir(parents=True)
    (home / "SOUL.md").write_text("# Badr\n\nEngineering owner.\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    fake_job = {"id": "job-observe", "name": kernel.JOB_NAME}
    monkeypatch.setattr("agent.autonomy.cron_setup._find_observe_job", lambda: None)
    monkeypatch.setattr(
        "cron.jobs.create_job",
        lambda **kwargs: fake_job,
    )
    monkeypatch.setattr("cron.jobs.use_cron_store", lambda _home: _null_cm())
    result = kernel.enable_profile(profile="badr")
    assert result["soul_updated"] is True
    soul = (home / "SOUL.md").read_text(encoding="utf-8")
    assert kernel.SOUL_MARKER in soul
    assert "engineering" in (result["mission"] or "").lower()
    assert (home / "scripts" / OBSERVE_SCRIPT).exists()
    assert (home / "autonomy" / "mission.md").exists()
    again = kernel.enable_profile(profile="badr")
    assert again["soul_updated"] is False


def test_owner_lines_are_quiet_product_language(autonomy_home):
    from agent.autonomy import kernel

    line = kernel.owner_line("abu-saud", "needs_owner", "launch timing decision")
    assert "needs you" in line.lower()
    assert "pid" not in line
    assert "scheduler" not in line


def test_restart_resume_reads_same_work_row(autonomy_home):
    from agent.autonomy import kernel, store

    started = kernel.start_work(
        why="durable",
        outcome="survive restart",
        done_contract="row still present",
        objective="persist",
        idempotency_key="restart:1",
    )
    wid = started["work"]["id"]
    reloaded = store.get_work_by_id(wid)
    assert reloaded is not None
    assert reloaded["state"] == "working"
    again = kernel.start_work(
        why="durable",
        outcome="survive restart",
        done_contract="row still present",
        objective="persist",
        idempotency_key="restart:1",
    )
    assert again["duplicate"] is True
    assert again["work"]["id"] == wid


def test_two_profiles_do_not_share_work_rows(tmp_path, monkeypatch):
    from agent.autonomy import kernel, store

    badr = tmp_path / "profiles" / "badr"
    sami = tmp_path / "profiles" / "sami"
    badr.mkdir(parents=True)
    sami.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(badr))
    kernel.start_work(
        why="badr only",
        outcome="badr",
        done_contract="done",
        objective="badr work",
        idempotency_key="shared-looking-key",
    )
    monkeypatch.setenv("HERMES_HOME", str(sami))
    assert store.list_work() == []
    sami_work = kernel.start_work(
        why="sami only",
        outcome="sami",
        done_contract="done",
        objective="sami work",
        idempotency_key="shared-looking-key",
    )
    assert sami_work["created"] is True
    monkeypatch.setenv("HERMES_HOME", str(badr))
    badr_items = store.list_work()
    assert len(badr_items) == 1
    assert badr_items[0]["objective"] == "badr work"


def test_cli_parser_exposes_enable_and_work_start():
    import argparse

    from hermes_cli.subcommands.autonomy import build_autonomy_parser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    build_autonomy_parser(sub)
    args = parser.parse_args(
        [
            "autonomy",
            "work-start",
            "--key",
            "k",
            "--why",
            "w",
            "--outcome",
            "o",
            "--done",
            "d",
            "--objective",
            "obj",
        ]
    )
    assert args.autonomy_command == "work-start"
    assert args.idempotency_key == "k"
    complete = parser.parse_args(
        ["autonomy", "work-complete", "--work-id", "aw_1", "--result", "done"]
    )
    assert complete.work_id == "aw_1"
    assert complete.result == "done"


class _null_cm:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False
