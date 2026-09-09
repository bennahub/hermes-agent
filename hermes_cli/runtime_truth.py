"""Canonical hosted Hermes runtime facts.

Agents must not infer Hermes health from the machine they happen to be on.
This module reads the hosted release under the Hermes root
(``releases/current``) plus on-disk gateway/cron markers. Local git, Agent
Computer checkouts, and ``systemctl --user`` bus failures are not truth.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

_SNAPSHOT_NAME = "canonical_runtime.json"
_TICKER_STALE_S = 200
_UNIT_STATES = {"active", "inactive", "failed", "activating", "deactivating"}
_SECRET_KEY_MARKERS = ("KEY", "TOKEN", "SECRET")


def hosted_release_dir(root: Path | None = None) -> Optional[Path]:
    """Return ``<root>/releases/current`` when a hosted release is installed."""
    base = Path(root) if root is not None else _hermes_root()
    current = base / "releases" / "current"
    try:
        resolved = current.resolve()
    except OSError:
        return None
    if not resolved.is_dir():
        return None
    if any((resolved / name).is_file() for name in ("RUNTIME_IDENTITY", ".hermes_build_sha")):
        return resolved
    if (resolved / "hermes_cli").is_dir():
        return resolved
    return None


def snapshot_path(root: Path | None = None) -> Path:
    return (Path(root) if root is not None else _hermes_root()) / _SNAPSHOT_NAME


def collect_canonical_runtime(*, persist: bool = False, root: Path | None = None) -> dict[str, Any]:
    """Return the hosted runtime snapshot. Never uses cwd git as Hermes SHA."""
    hermes_root = Path(root) if root is not None else _hermes_root()
    release = hosted_release_dir(hermes_root)
    if release is None:
        payload = {
            "canonical": False,
            "source": "not_canonical",
            "hermes_root": str(hermes_root),
            "release_path": None,
            "sha": None,
            "short_sha": None,
            "gateway": "unknown",
            "serve": "unknown",
            "profile_schema": {},
            "profile_schema_version": None,
            "profiles_consistent": False,
            "providers_ready": False,
            "scheduler": "unknown",
        }
        return payload
    sha, sha_source = _release_sha(release)
    profiles = _profile_schema_versions(hermes_root)
    numeric = [v for v in profiles.values() if isinstance(v, int)]
    expected = _expected_schema_version()
    if expected is None and numeric:
        expected = max(numeric)
    consistent = bool(numeric) and expected is not None and all(v == expected for v in numeric)
    payload = {
        "canonical": True,
        "source": "hosted-release",
        "hermes_root": str(hermes_root),
        "release_path": str(release),
        "sha": sha,
        "short_sha": sha[:8] if sha else None,
        "sha_source": sha_source,
        "gateway": _service_status("hermes-gateway", hermes_root / "gateway_state.json"),
        "serve": _service_status("hermes-serve", None),
        "profile_schema": profiles,
        "profile_schema_version": expected,
        "profiles_consistent": consistent,
        "providers_ready": _providers_ready(hermes_root),
        "scheduler": _scheduler_health(hermes_root),
    }
    if persist:
        write_snapshot(payload, hermes_root)
    return payload


def write_snapshot(payload: dict[str, Any], root: Path | None = None) -> Path:
    path = snapshot_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def render_runtime(payload: dict[str, Any]) -> str:
    """Short human summary. Agents should prefer the JSON form."""
    if not payload.get("canonical"):
        return (
            "This process is not the hosted Hermes runtime. "
            "Ask the canonical VPS with `hermes runtime --json`."
        )
    schema = payload.get("profile_schema") or {}
    n = len(schema)
    version = payload.get("profile_schema_version")
    ok = "consistent" if payload.get("profiles_consistent") else "mixed"
    lines = [
        "Hermes runtime (canonical VPS)",
        f"  SHA:        {payload.get('sha') or 'unknown'}",
        f"  Release:    {payload.get('release_path') or 'unknown'}",
        f"  Gateway:    {payload.get('gateway')}",
        f"  Serve:      {payload.get('serve')}",
        f"  Schema:     {version} ({n} profiles, {ok})",
        f"  Providers:  {'ready' if payload.get('providers_ready') else 'not ready'}",
        f"  Scheduler:  {payload.get('scheduler')}",
    ]
    return "\n".join(lines)


def _hermes_root() -> Path:
    from hermes_constants import get_default_hermes_root

    return Path(get_default_hermes_root())


def _release_sha(release: Path) -> tuple[Optional[str], str]:
    identity = release / "RUNTIME_IDENTITY"
    sha = _parse_sha_file(identity)
    if sha:
        return sha, "runtime-identity"
    sha = _parse_sha_file(release / ".hermes_build_sha")
    if sha:
        return sha, "build-file"
    return None, "unknown"


def _parse_sha_file(path: Path) -> Optional[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text:
        return None
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        if isinstance(data, dict):
            return _sha_or_none(str(data.get("sha") or data.get("code_sha") or ""))
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("sha="):
            return _sha_or_none(line.split("=", 1)[1].strip())
        got = _sha_or_none(line)
        if got:
            return got
    return None


def _sha_or_none(value: str) -> Optional[str]:
    value = value.strip()
    return value if len(value) == 40 and all(c in "0123456789abcdefABCDEF" for c in value) else None


def _read_config_version(path: Path) -> Optional[int]:
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("_config_version"):
            _, _, raw = stripped.partition(":")
            try:
                return int(str(raw).strip().split()[0])
            except (TypeError, ValueError, IndexError):
                return None
    return None


def _profile_schema_versions(root: Path) -> dict[str, Optional[int]]:
    versions: dict[str, Optional[int]] = {"default": _read_config_version(root / "config.yaml")}
    profiles = root / "profiles"
    if not profiles.is_dir():
        return versions
    try:
        entries = sorted(profiles.iterdir())
    except OSError:
        return versions
    for entry in entries:
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        versions[entry.name] = _read_config_version(entry / "config.yaml")
    return versions


def _expected_schema_version() -> Optional[int]:
    try:
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        return int(DEFAULT_CONFIG["_config_version"])
    except Exception:
        return None


def _providers_ready(root: Path) -> bool:
    env_path = root / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, val = stripped.partition("=")
        value = val.strip().strip("'").strip('"')
        if value and any(marker in key.upper() for marker in _SECRET_KEY_MARKERS):
            return True
    cfg = root / "config.yaml"
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "model:" in text and ("default:" in text or "provider:" in text)


def _scheduler_health(root: Path) -> str:
    ages: list[float] = []
    candidates = [root / "cron" / "ticker_heartbeat"]
    profiles = root / "profiles"
    if profiles.is_dir():
        try:
            candidates.extend(profiles.glob("*/cron/ticker_heartbeat"))
        except OSError:
            pass
    now = time.time()
    for path in candidates:
        try:
            ages.append(max(0.0, now - float(path.read_text(encoding="utf-8").strip())))
        except (OSError, ValueError):
            continue
    if not ages:
        return "unknown"
    return "healthy" if min(ages) <= _TICKER_STALE_S else "stale"


def _service_status(unit: str, state_path: Optional[Path]) -> str:
    unit_state = _unit_state(unit)
    pid_alive = _state_pid_alive(state_path) if state_path is not None else False
    if unit_state == "active" or pid_alive:
        return "active"
    if unit_state == "failed":
        return "failed"
    if unit_state == "stopped":
        return "stopped"
    return "unknown"


def _unit_state(unit: str) -> Optional[str]:
    """Read-only systemd probe. User-bus failures are ignored, not 'stopped'."""
    for extra in ([], ["--user"]):
        try:
            result = subprocess.run(
                ["systemctl", *extra, "is-active", unit],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except Exception:
            continue
        combined = f"{result.stdout or ''}{result.stderr or ''}"
        if "Failed to connect to bus" in combined or "No medium found" in combined:
            continue
        state = (result.stdout or "").strip().splitlines()
        name = state[0] if state else ""
        if name == "active":
            return "active"
        if name == "failed":
            return "failed"
        if name in _UNIT_STATES:
            return "stopped"
    return None


def _state_pid_alive(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    pid = payload.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    return _pid_alive(pid)


def _pid_alive(pid: int) -> bool:
    proc = Path("/proc") / str(pid)
    if proc.exists():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
