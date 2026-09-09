"""Baked-in build metadata for Hermes Agent.

Source installs report their git revision live via ``git rev-parse`` (``hermes_cli/dump.py``,
``hermes_cli/banner.py``). Inside the published Docker image ``.dockerignore`` excludes ``.git``,
so those callsites fall back to the ``.hermes_build_sha`` file written by the Dockerfile.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

# Resolved relative to this module so it works regardless of cwd (as banner._resolve_repo_dir).
_BUILD_SHA_FILE = Path(__file__).parent.parent / ".hermes_build_sha"
_RUNTIME_IDENTITY_FILE = Path(__file__).parent.parent / "RUNTIME_IDENTITY"
_code_identity_cache: Optional[dict] = None


def _read_stripped(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace").strip()


def _sha_or_none(value: str) -> Optional[str]:
    return value if len(value) == 40 else None


def _resolve_git_head_sha(project_root: Path) -> Optional[str]:
    """HEAD sha by reading .git directly (worktrees/submodules, loose refs, packed-refs).

    Deliberately NOT ``git rev-parse``: this runs in library paths (runtime-status writes, update
    receipts) where spawning is slow and hostile to tests that mock ``subprocess.run`` tightly.
    Returns None on any failure.
    """
    try:
        git_path = project_root / ".git"
        if git_path.is_file():
            # Worktree/submodule: ".git" is a "gitdir: <path>" pointer file.
            pointer = _read_stripped(git_path)
            if not pointer.startswith("gitdir:"):
                return None
            git_dir = Path(pointer[len("gitdir:"):].strip())
            if not git_dir.is_absolute():
                git_dir = (project_root / git_dir).resolve()
        elif git_path.is_dir():
            git_dir = git_path
        else:
            return None
        # Refs live in the COMMON git dir for worktrees.
        common_dir = git_dir
        commondir_file = git_dir / "commondir"
        if commondir_file.is_file():
            common = Path(_read_stripped(commondir_file))
            common_dir = common if common.is_absolute() else (git_dir / common).resolve()
        head = _read_stripped(git_dir / "HEAD")
        if not head.startswith("ref:"):
            return _sha_or_none(head)  # detached HEAD holds the sha itself
        ref_name = head[len("ref:"):].strip()
        loose = common_dir / ref_name
        if loose.is_file():
            return _sha_or_none(_read_stripped(loose))
        packed = common_dir / "packed-refs"
        if packed.is_file():
            for line in _read_stripped(packed).splitlines():
                line = line.strip()
                if not line or line.startswith(("#", "^")):
                    continue
                parts = line.split(" ", 1)
                if len(parts) == 2 and parts[1].strip() == ref_name:
                    return _sha_or_none(parts[0].strip())
    except Exception:
        return None
    return None


def _read_runtime_identity_sha(path: Path | None = None) -> Optional[str]:
    """SHA stamped by a hosted release. Copied ``.git`` dirs must not outrank this."""
    try:
        text = _read_stripped(path if path is not None else _RUNTIME_IDENTITY_FILE)
    except Exception:
        return None
    if not text:
        return None
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except Exception:
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


def get_code_identity(refresh: bool = False) -> dict:
    """``{sha, short_sha, version, source}`` for the running checkout; never raises.

    Hosted releases stamp ``RUNTIME_IDENTITY`` / ``.hermes_build_sha``; those win over a
    copied ``.git`` HEAD (which can lag the overlay). Source checkouts without stamps still
    use live git. Cached per process — code identity cannot change while a process runs.
    """
    global _code_identity_cache
    if _code_identity_cache is not None and not refresh:
        return dict(_code_identity_cache)
    project_root = Path(__file__).parent.parent
    source = "unknown"
    sha = _read_runtime_identity_sha()
    if sha:
        source = "runtime-identity"
    else:
        sha = get_build_sha(short=0)
        if sha:
            source = "build-file"
        else:
            sha = _resolve_git_head_sha(project_root)
            if sha:
                source = "git"
    version: Optional[str] = None
    try:
        import tomllib
        with open(project_root / "pyproject.toml", "rb") as fh:  # windows-footgun: ok — binary mode, tomllib requires bytes
            raw_version = tomllib.load(fh).get("project", {}).get("version")
        version = str(raw_version) if raw_version else None
    except Exception:
        version = None
    _code_identity_cache = {
        "sha": sha,
        "short_sha": sha[:8] if sha else None,
        "version": version,
        "source": source}
    return dict(_code_identity_cache)


def get_build_sha(short: int = 8) -> Optional[str]:
    """Baked-in build SHA (Dockerfile ``HERMES_GIT_SHA`` build-arg, full 40-char hash on one
    line), truncated to ``short`` chars (``short<=0`` = full), or None."""
    try:
        if not _BUILD_SHA_FILE.is_file():
            return None
        sha = _BUILD_SHA_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return None
    if not sha:
        return None
    return sha[:short] if short and short > 0 else sha
