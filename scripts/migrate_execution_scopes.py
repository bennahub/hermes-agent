"""Prepare history-free scheduled policies; apply backup-backed additive authority metadata."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cron.execution_scope import assignment
from cron.jobs import _jobs_lock, _normalize_job_record, is_job_runnable, use_cron_store


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def read_jobs(path):
    raw = path.read_bytes()
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise ValueError("Migration requires canonical native jobs JSON")
    ids = [job["id"] for job in data["jobs"]]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate native job identity")
    return raw, data


def configured_subject(home: Path, job: dict) -> dict:
    """Only the stored assignment and exact explicitly configured script bytes."""
    scripts = {}
    for field in ("script", "monitor_script"):
        name = job.get(field)
        if not name:
            continue
        path = Path(name).expanduser()
        if not path.is_absolute():
            path = home / "scripts" / path
        path = path.resolve(strict=True)
        path.relative_to((home / "scripts").resolve())
        raw = path.read_bytes()
        scripts[field] = {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                          "program": raw.decode("utf-8")}
    return {"configured_assignment": assignment(job), "configured_scripts": scripts}


def reverse_bindings(home: Path) -> dict:
    path = home / "autonomy" / "work.db"
    result = {}
    if not path.is_file():
        return result
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        for work_id, raw in db.execute("SELECT id,refs_json FROM work"):
            refs = json.loads(raw or "{}")
            job = refs.get("resume_job") or {}
            if job.get("id") and job.get("generation") == refs.get("resume_generation"):
                result.setdefault(job["id"], []).append(
                    {"work_id": work_id, "generation": job["generation"], "hermes_home": str(home)})
    return result


def prepare(root: Path, *, compiler=None) -> dict:
    from agent.execution_scope_policy import derive_scope

    root = root.resolve()
    compiler = compiler or derive_scope
    homes = [root, *sorted(p for p in (root / "profiles").iterdir()
                          if p.is_dir() and not p.name.startswith("."))]
    manifest = {"version": 1, "migration_id": uuid.uuid4().hex, "root": str(root), "homes": []}
    for home in homes:
        path = home / "cron" / "jobs.json"
        if not path.is_file():
            continue
        raw, data = read_jobs(path)
        reverse = reverse_bindings(home)
        operations = []
        for stored in data["jobs"]:
            job = _normalize_job_record(stored)
            if not is_job_runnable(job) or job.get("state") in {"completed", "error"}:
                continue
            if job.get("execution_scope") or job.get("native_continuation"):
                continue
            bindings = reverse.get(job["id"], [])
            if len(bindings) > 1:
                raise ValueError("Ambiguous native continuation binding")
            op = {"job_id": job["id"], "job_digest": digest(stored)}
            if bindings:
                op["native_continuation"] = bindings[0]
            else:
                subject = configured_subject(home, job)
                key = "cron:" + job["id"] + ":" + uuid.uuid4().hex
                op.update(
                    source_key=key,
                    source={"kind": "scheduled", "assignment_kind": "cron", "assignment_id": key,
                            "job_id": job["id"], "job_home": str(home),
                            "instruction": assignment(job), "migration_id": manifest["migration_id"]},
                    subject=subject, policy=compiler(subject),
                )
            operations.append(op)
        if operations:
            manifest["homes"].append({"home": str(home.relative_to(root)),
                "file_sha256": hashlib.sha256(raw).hexdigest(), "document_digest": digest(data),
                "operations": operations})
    return manifest


def home_path(root: Path, entry: dict) -> Path:
    home = (root / entry["home"]).resolve()
    if home != root and not (home.parent == root / "profiles" and not home.name.startswith(".")):
        raise ValueError("Migration home is outside native profile roots")
    return home


def validate_home(root: Path, entry: dict) -> tuple[Path, bytes, dict]:
    home = home_path(root, entry)
    raw, data = read_jobs(home / "cron" / "jobs.json")
    original = deepcopy(data)
    current = {job["id"]: job for job in data["jobs"]}
    unadorned = {job["id"]: job for job in original["jobs"]}
    changed = False
    reverse = reverse_bindings(home)
    for op in entry["operations"]:
        job = current.get(op["job_id"])
        if job is None:
            raise ValueError("Migration job disappeared")
        field = "native_continuation" if "native_continuation" in op else "execution_scope"
        if field in job:
            changed = True
            unadorned[op["job_id"]].pop(field)
            if field == "native_continuation" and job[field] != op[field]:
                raise ValueError("Native continuation metadata conflicts")
            if field == "execution_scope" and (
                    job[field].get("assignment_id") != op["source_key"]
                    or Path(job[field].get("db_path", "")).resolve() != home / "state.db"):
                raise ValueError("Existing scope locator conflicts")
        if digest(unadorned[op["job_id"]]) != op["job_digest"]:
            raise ValueError("Migration job configuration drifted")
        if field == "native_continuation":
            if reverse.get(op["job_id"]) != [op[field]]:
                raise ValueError("Native continuation reverse binding drifted")
        elif configured_subject(home, _normalize_job_record(job)) != op["subject"]:
            raise ValueError("Configured assignment or script bytes drifted")
    if digest(original) != entry["document_digest"]:
        raise ValueError("Unselected job store content drifted")
    if not changed and hashlib.sha256(raw).hexdigest() != entry["file_sha256"]:
        raise ValueError("Job store file bytes drifted")
    return home, raw, data


def atomic_json(path: Path, value: dict) -> None:
    previous = path.stat() if path.exists() else None
    temp = path.with_name(path.name + ".migration-" + uuid.uuid4().hex)
    try:
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        if previous is not None:
            os.chmod(temp, previous.st_mode & 0o777)
            os.chown(temp, previous.st_uid, previous.st_gid)
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temp.unlink(missing_ok=True)


def apply(manifest: dict, backup_dir: Path, *, after_scope=None) -> dict:
    """Caller owns cold drain. This function never invokes a policy model."""
    from hermes_state import SessionDB

    root = Path(manifest["root"]).resolve()
    if manifest.get("version") != 1:
        raise ValueError("Unsupported migration manifest")
    # All jobs and script inputs must match before backups or schema writes.
    checked = [validate_home(root, entry) for entry in manifest["homes"]]
    backup_dir = backup_dir.resolve()
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt_path = backup_dir / "backup-receipt.json"
    manifest_digest = digest(manifest)
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text())
        if receipt["manifest_digest"] != manifest_digest:
            raise ValueError("Backup belongs to a different migration")
        for file, expected in receipt["files"].items():
            if hashlib.sha256((backup_dir / file).read_bytes()).hexdigest() != expected:
                raise ValueError("Migration backup is missing or altered")
    else:
        if any("execution_scope" in j or "native_continuation" in j
               for _, _, data in checked for j in data["jobs"]
               if any(j["id"] == op["job_id"] for e in manifest["homes"] for op in e["operations"])):
            raise ValueError("Applied metadata requires the original backup receipt")
        files, absent = {}, []
        for entry, (home, raw, _) in zip(manifest["homes"], checked):
            destination = backup_dir / entry["home"]
            destination.mkdir(parents=True, exist_ok=True)
            jobs_copy = destination / "jobs.json"
            jobs_copy.write_bytes(raw)
            files[str(jobs_copy.relative_to(backup_dir))] = hashlib.sha256(raw).hexdigest()
            db_path = home / "state.db"
            backup = destination / "state.db"
            if db_path.is_file():
                backup.unlink(missing_ok=True)
                with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as source:
                    with sqlite3.connect(backup) as target:
                        source.backup(target)
                files[str(backup.relative_to(backup_dir))] = hashlib.sha256(backup.read_bytes()).hexdigest()
            else:
                absent.append(str(db_path.relative_to(root)))
        atomic_json(receipt_path, {"manifest_digest": manifest_digest, "files": files, "absent_databases": absent})
    result = []
    for entry in manifest["homes"]:
        home = home_path(root, entry)
        with use_cron_store(home), _jobs_lock(required=True):
            _, _, data = validate_home(root, entry)
            jobs = {job["id"]: job for job in data["jobs"]}
            changed = []
            with SessionDB(home / "state.db") as db:
                for op in entry["operations"]:
                    job = jobs[op["job_id"]]
                    if "native_continuation" in op:
                        desired, field = op["native_continuation"], "native_continuation"
                    else:
                        record = db.create_or_get_scope(op["source_key"], op["source"], op["policy"])
                        if record["state"] != "active":
                            raise ValueError("Migration scope was closed")
                        desired = {"db_path": str(db.db_path), "scope_id": record["scope_id"],
                                   "assignment_id": op["source_key"]}
                        field = "execution_scope"
                        if after_scope:
                            after_scope(record)
                    if field in job and job[field] != desired:
                        raise ValueError("Existing migration locator conflicts")
                    if job.get(field) != desired:
                        job[field] = desired
                        changed.append(job["id"])
            if changed:
                atomic_json(home / "cron" / "jobs.json", data)
            result.append({"home": entry["home"], "changed_ids": changed})
    return {"migration_id": manifest["migration_id"], "homes": result}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path)
    args = parser.parse_args()
    if args.prepare:
        if not args.root or args.manifest.exists():
            parser.error("--prepare requires --root and a new manifest path")
        atomic_json(args.manifest, prepare(args.root))
        print(json.dumps({"prepared": str(args.manifest)}))
    else:
        if not args.backup_dir:
            parser.error("--apply requires --backup-dir")
        print(json.dumps(apply(json.loads(args.manifest.read_text()), args.backup_dir), indent=2))
