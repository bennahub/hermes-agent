"""Production migration seams: native jobs, SessionDB and exact precompiled identities."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

import pytest

from cron.jobs import create_job, use_cron_store
from hermes_state import SessionDB
from scripts import migrate_execution_scopes as migration


@pytest.fixture()
def population(tmp_path, monkeypatch):
    (tmp_path / "profiles").mkdir()
    home = tmp_path / "profiles" / "example"
    home.mkdir()
    (home / "scripts").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("agent.execution_scope.capture_binding", lambda: None)
    with SessionDB(home / "state.db") as db:
        db.create_session("keep-session", "cli")
        db.append_message("keep-session", "user", "Preserve existing conversation")
    with use_cron_store(home):
        script = home / "scripts" / "configured.py"
        script.write_text("print('configured report')")
        first = create_job("Report configured results", "every 1h", script=str(script))
        second = create_job("Report configured results", "every 1h", script=str(script))
        inactive = create_job("Leave paused job unchanged", "every 1h")
        from cron.jobs import pause_job
        pause_job(inactive["id"])
    return tmp_path, home, first, second


def compile_exact(subject):
    return {"version": 1, "original_instruction": deepcopy(subject),
            "policy": {"objective": "Run exactly the configured report",
                       "permitted": ["Configured reporting"], "excluded": ["Unrelated work"]}}


def test_apply_adds_only_locators_and_rerun_is_noop(population, tmp_path):
    root, home, first, second = population
    original = json.loads((home / "cron" / "jobs.json").read_text())
    manifest = migration.prepare(root, compiler=compile_exact)
    ops = manifest["homes"][0]["operations"]
    assert len(ops) == 2
    assert ops[0]["source_key"] != ops[1]["source_key"]
    assert ops[0]["subject"]["configured_scripts"]["script"]["program"] == "print('configured report')"
    assert ops[0]["source"]["instruction"] == ops[0]["subject"]["configured_assignment"]
    backup = tmp_path / "backup"
    result = migration.apply(manifest, backup)
    assert len(result["homes"][0]["changed_ids"]) == 2
    updated = json.loads((home / "cron" / "jobs.json").read_text())
    with SessionDB(home / "state.db") as db:
        assert db.get_messages("keep-session")[0]["content"] == "Preserve existing conversation"
        for job in updated["jobs"]:
            locator = job.pop("execution_scope", None)
            if locator:
                scope = db.get_scope(locator["scope_id"])
                assert scope["source"]["assignment_id"] == locator["assignment_id"]
                assert scope["state"] == "active"
    assert updated == original
    assert migration.apply(manifest, backup)["homes"][0]["changed_ids"] == []
    assert json.loads((backup / "profiles" / "example" / "jobs.json").read_text()) == original


def test_interruption_after_scope_insert_reuses_same_scope(population, tmp_path):
    root, home, *_ = population
    manifest = migration.prepare(root, compiler=compile_exact)
    inserted = []

    def interrupt(record):
        inserted.append(record["scope_id"])
        raise RuntimeError("Simulated stop after durable scope insert")

    with pytest.raises(RuntimeError, match="Simulated stop"):
        migration.apply(manifest, tmp_path / "backup", after_scope=interrupt)
    with SessionDB(home / "state.db") as db:
        before = db.get_scope_for_source(manifest["homes"][0]["operations"][0]["source_key"])
        assert before["scope_id"] == inserted[0]
    migration.apply(manifest, tmp_path / "backup")
    with SessionDB(home / "state.db") as db:
        after = db.get_scope_for_source(manifest["homes"][0]["operations"][0]["source_key"])
        assert after["scope_id"] == inserted[0]
    assert migration.apply(manifest, tmp_path / "backup")["homes"][0]["changed_ids"] == []


@pytest.mark.parametrize("drift", ["job", "script", "unselected"])
def test_drift_aborts_before_any_scope_or_backup(population, tmp_path, drift):
    root, home, *_ = population
    manifest = migration.prepare(root, compiler=compile_exact)
    if drift == "script":
        (home / "scripts" / "configured.py").write_text("print('different action')")
    else:
        path = home / "cron" / "jobs.json"
        jobs = json.loads(path.read_text())
        jobs["jobs"][0 if drift == "job" else -1]["prompt"] = "Changed after policy preparation"
        path.write_text(json.dumps(jobs))
    with pytest.raises(ValueError, match="drifted"):
        migration.apply(manifest, tmp_path / "backup")
    assert not (tmp_path / "backup").exists()
    with SessionDB(home / "state.db") as db:
        assert db.get_scope_for_source(manifest["homes"][0]["operations"][0]["source_key"]) is None


def test_native_marker_requires_exact_reverse_binding(population, tmp_path):
    from agent.autonomy import store

    root, home, first, _ = population
    work = store.start_work(
        why="Native pending continuation", outcome="Resume the existing assignment",
        done_contract="Durable result", idempotency_key="native-test",
        refs={"resume_generation": 3, "resume_job": {"id": first["id"], "generation": 3}},
        hermes_home=home,
    )["work"]
    manifest = migration.prepare(root, compiler=compile_exact)
    operation = next(op for op in manifest["homes"][0]["operations"] if op["job_id"] == first["id"])
    assert operation["native_continuation"]["work_id"] == work["id"]
    assert "policy" not in operation
    with sqlite3.connect(home / "autonomy" / "work.db") as db:
        before = db.execute("SELECT * FROM work").fetchall()
    migration.apply(manifest, tmp_path / "backup")
    with sqlite3.connect(home / "autonomy" / "work.db") as db:
        assert db.execute("SELECT * FROM work").fetchall() == before
    assert migration.apply(manifest, tmp_path / "backup")["homes"][0]["changed_ids"] == []



def test_interruption_between_homes_preserves_prior_backup_and_completed_home(population, tmp_path):
    root, home, *_ = population
    other = root / "profiles" / "other"
    other.mkdir()
    with use_cron_store(other):
        create_job("A distinct second-profile task", "every 2h")
    manifest = migration.prepare(root, compiler=compile_exact)
    seen = []

    def interrupt(record):
        seen.append(record)
        if len(seen) == 3:
            raise RuntimeError("Stop in second home")

    backup = tmp_path / "backup"
    with pytest.raises(RuntimeError, match="second home"):
        migration.apply(manifest, backup, after_scope=interrupt)
    receipt_before = (backup / "backup-receipt.json").read_bytes()
    result = migration.apply(manifest, backup)
    assert result["homes"][0]["changed_ids"] == []
    assert len(result["homes"][1]["changed_ids"]) == 1
    assert (backup / "backup-receipt.json").read_bytes() == receipt_before
    assert all(not row["changed_ids"] for row in migration.apply(manifest, backup)["homes"])


def test_backup_tampering_rejected_on_idempotent_apply(population, tmp_path):
    root, *_ = population
    manifest = migration.prepare(root, compiler=compile_exact)
    backup = tmp_path / "backup"
    migration.apply(manifest, backup)
    (backup / "profiles" / "example" / "jobs.json").write_text("altered")
    with pytest.raises(ValueError, match="backup is missing or altered"):
        migration.apply(manifest, backup)
