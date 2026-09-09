"""Files an agent has explicitly published for the owner.

An agent could create a file and then only tell the owner where it was:

    /home/hermes/bennahub-cmo/BennaHub-Marketing-Plan-90d-2026-09-02.docx

Asked to send the file itself, it reported that it could not. That is the
defect this module closes. The owner should never have to read a VPS
filesystem path to receive something an agent made for them.

The obvious fix — let the client download whatever path the agent mentions —
is the wrong one, and is already most of the way to being a hole:
``/api/files/download`` resolves an arbitrary absolute path, and its
containment check is skipped entirely when no managed root is configured
(``_managed_files_policy``), which is the case on a default deployment. So
"widen the client until it accepts /home/hermes/anything" would have turned an
accepted-but-bounded risk into the product's supported contract.

Instead a file becomes owner-downloadable only by being **published**, and
publication does three things that matter:

1. **It copies the bytes into a managed store.** The registry does not
   reference the agent's path — it owns a copy under
   ``<profile>/artifacts/published/``. That is what lets the download route
   read from exactly one directory, so the client never needs a path and the
   broad path-based route can be locked down without breaking this one. It
   also means the attachment still resolves after the agent moves, rewrites or
   deletes its working copy, which a reference could not promise.

2. **It mints the identity.** A 32-hex id the server chose, never a filename
   and never a path. The filename is metadata carried alongside; it is what
   the owner sees, and it never participates in resolving anything on disk.

3. **It records the file.** Size, MIME, SHA-256 and provenance, durably, so a
   history reload months later still renders a real file card rather than
   degrading to prose.

**Where the index lives.** Its own SQLite file beside the bytes,
``<profile>/artifacts/published/index.db``, rather than a table in the
profile's ``state.db``. The shared state schema carries a version and a
migration discipline that every profile database moves through together, and
a new attachment concept does not justify that blast radius. Keeping index and
bytes in one directory also means the store backs up, moves and is reasoned
about as a unit. The cost is that a publication is not in the same transaction
as the message that references it, which is acceptable: the message carries an
id, and the id resolves — or honestly does not — on its own.

This is modelled on ``gateway/browser_control_artifacts.py``, which already got
server-minted ids, hashing and MIME handling right. It deliberately does **not**
inherit that module's TTL, its consume-on-read, or its in-memory index: those
are correct for a one-shot browser hand-off and wrong for a file in a
conversation the owner may reopen next year.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import threading as _threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from gateway.path_authorization import (
    SENSITIVE_DIR_NAMES,
    SENSITIVE_FILE_BASENAMES,
    SENSITIVE_NAME_PREFIXES,
    is_sensitive_path,
    is_within_allowed_roots,
)


# --- policy ---------------------------------------------------------------

#: Matches ``KANBAN_ATTACHMENT_MAX_BYTES`` and the session attach cap, so a
#: file that can be attached one way is not silently refused another.
MAX_ARTIFACT_BYTES = 25 * 1024 * 1024

#: Server-minted, and the only thing a client ever sends back.
ARTIFACT_ID_RE = re.compile(r"^[0-9a-f]{32}$")

#: Names that must never become an owner-downloadable artifact, whatever the
#: agent asks for.
#:
#: These used to be a *copy* of the managed-files read denylist, and a copy
#: drifts: this module's list omitted ``hosts.yml`` (the GitHub CLI token store
#: at ``~/.config/gh/hosts.yml`` on this deployment) and ``id_ecdsa``, both of
#: which the read side carried — so ``send_file`` published a live token store
#: that ``/api/files/read`` refused. They are now aliases of the one list in
#: ``gateway.path_authorization``; the names are kept so existing callers and
#: tests that reach for them keep resolving.
_SENSITIVE_NAMES = SENSITIVE_FILE_BASENAMES

#: Any path with one of these as a component is refused outright.
_SENSITIVE_DIRS = SENSITIVE_DIR_NAMES

_SENSITIVE_PREFIXES = SENSITIVE_NAME_PREFIXES

_DEFAULT_MIME = "application/octet-stream"


class PublishRefused(Exception):
    """A file was not published, with a reason the caller can act on.

    ``reason`` is a stable token for programmatic handling; ``str(exc)`` is a
    sentence an agent can read. Neither carries the resolved path — an error
    that echoes the filesystem back is its own small disclosure.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class PublishedArtifact:
    artifact_id: str
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    profile: str
    session_id: str
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        """The shape a message reference and an API response both use.

        Note what is absent: there is no path. A client that could name a path
        would be a client that could ask for one.
        """
        return {
            "artifact_id": self.artifact_id,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "profile": self.profile,
            "session_id": self.session_id,
            "created_at": self.created_at,
        }


# --- store layout ---------------------------------------------------------


def store_dir(profile_home: Path | str) -> Path:
    """The one directory published bytes live in, for this profile."""
    return Path(profile_home) / "artifacts" / "published"


def _index_path(profile_home: Path | str) -> Path:
    return store_dir(profile_home) / "index.db"


def _blob_path(profile_home: Path | str, artifact_id: str) -> Path:
    # The id *is* the filename. The owner-facing name is metadata and never
    # touches the filesystem, so a name like "../../etc/passwd" or one with a
    # NUL in it can be recorded and displayed without ever being resolved.
    return store_dir(profile_home) / artifact_id


_SCHEMA = """
CREATE TABLE IF NOT EXISTS published_artifacts (
    artifact_id TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,
    mime_type   TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    profile     TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    source_path TEXT NOT NULL,
    created_at  REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'available'
);
CREATE INDEX IF NOT EXISTS published_artifacts_session
    ON published_artifacts(session_id, created_at);
"""


def _connect(profile_home: Path | str) -> sqlite3.Connection:
    directory = store_dir(profile_home)
    directory.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_index_path(profile_home))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


# --- validation -----------------------------------------------------------


def _is_sensitive(path: Path) -> bool:
    """Whether this file is one the owner must never receive as an attachment.

    Delegates to the shared judgement in ``gateway.path_authorization``. Names
    are matched as **prefixes** there, not exactly, because this deployment
    carries ``auth.json.bak-codex-fix`` beside every profile's ``auth.json``
    and ``config.yaml.bak-precopilot-removal`` beside every ``config.yaml`` —
    the backups a careful operator makes before editing a credential file.
    Under an exact match the original was refused and its byte-identical twin
    was publishable, which also *launders* it: publication copies the bytes to
    an artifact named by a 32-hex id, and that name matches no denylist
    anywhere.
    """
    return is_sensitive_path(path)


def _resolve_source(
    source_path: Any, *, profile_home: Path | str | None = None
) -> Path:
    """Canonicalise the agent's path, or refuse with a reason.

    Symlinks are resolved *before* every other check, so a link pointing at
    ``auth.json`` is judged as ``auth.json`` and a link pointing outside a
    permitted tree cannot smuggle its target past a check made on the link.

    Then — BWM-794 — the resolved path must be **inside a tree an agent may
    send from**. A denylist alone was the whole defence here, and a denylist
    alone cannot be the defence: it answers "is this file's *name* one I know
    about", and the answer for ``/etc/passwd``, ``/root/anything`` and a second
    user's home is no. The allowlist is the same one
    ``/api/files/read`` enforces (``path_authorization.read_allowed_roots``),
    so the two surfaces cannot drift apart again, plus the profile's own home —
    which is under ``HOME`` on this deployment but need not be on another.

    Both refusals are deliberately path-free. An error that echoes the
    filesystem back is its own small disclosure, and it is also an oracle: a
    caller that can tell "outside the permitted area" from "no such file" can
    map the disk without ever downloading anything. The two are separate
    ``reason`` tokens because the agent's next move differs — move the file
    versus check the name — but neither sentence quotes a path.
    """
    if not isinstance(source_path, str) or not source_path.strip():
        raise PublishRefused("invalid_path", "No file path was given.")
    raw = source_path.strip()
    if "\x00" in raw:
        raise PublishRefused("invalid_path", "That file path is not valid.")

    candidate = Path(os.path.expanduser(raw))
    if not candidate.is_absolute():
        raise PublishRefused(
            "invalid_path", "The file path must be absolute."
        )
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise PublishRefused("not_found", "That file could not be found.") from None

    # `resolve(strict=True)` already collapsed `..`; re-checking the resolved
    # form is what makes traversal a non-question rather than a regex.
    if not resolved.is_file():
        raise PublishRefused(
            "not_a_file", "That path is not a file."
        )
    extra_roots = (profile_home,) if profile_home is not None else ()
    if not is_within_allowed_roots(resolved, extra_roots=extra_roots):
        raise PublishRefused(
            "not_permitted",
            "That file is outside the area an agent can send files from.",
        )
    if _is_sensitive(resolved):
        raise PublishRefused(
            "refused", "That file cannot be sent — it holds credentials or configuration."
        )
    return resolved


# --- publication ----------------------------------------------------------


def publish(
    profile_home: Path | str,
    *,
    source_path: str,
    profile: str,
    session_id: str = "",
    filename: Optional[str] = None,
    max_bytes: int = MAX_ARTIFACT_BYTES,
) -> PublishedArtifact:
    """Register a file the agent can read as an owner-downloadable artifact.

    The bytes are copied into the profile's published store; the source is
    never read again. Returns the record the message should reference.

    "A file the agent can read" is narrower than it sounds: the source must
    resolve inside a tree an agent may send from — the shared allowlist plus
    this profile's own home — before its name is ever considered. Readable by
    the service account is not the same as publishable to the owner.

    Raises :class:`PublishRefused` for anything that must not be published,
    with a reason the agent can relay to the owner in plain words.
    """
    resolved = _resolve_source(source_path, profile_home=profile_home)

    size = resolved.stat().st_size
    if size > max_bytes:
        raise PublishRefused(
            "too_large",
            f"That file is too large to send ({size // (1024 * 1024)} MB; "
            f"the limit is {max_bytes // (1024 * 1024)} MB).",
        )

    display_name = (filename or resolved.name).strip() or resolved.name
    # The owner-facing name never resolves anything, but it does end up in a
    # Content-Disposition header and a file card, so strip separators and
    # control characters rather than trusting the source.
    display_name = re.sub(r"[\\/\x00-\x1f]", "_", display_name)[:255]

    artifact_id = secrets.token_hex(16)
    destination = _blob_path(profile_home, artifact_id)
    destination.parent.mkdir(parents=True, exist_ok=True)

    digest = hashlib.sha256()
    try:
        with resolved.open("rb") as src, destination.open("wb") as dst:
            copied = 0
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > max_bytes:
                    # The file grew between stat and read. Refuse rather than
                    # keep a truncated copy that would fail its own hash.
                    raise PublishRefused(
                        "too_large", "That file is too large to send."
                    )
                digest.update(chunk)
                dst.write(chunk)
    except PublishRefused:
        destination.unlink(missing_ok=True)
        raise
    except OSError:
        destination.unlink(missing_ok=True)
        raise PublishRefused("unreadable", "That file could not be read.") from None

    record = PublishedArtifact(
        artifact_id=artifact_id,
        filename=display_name,
        mime_type=mimetypes.guess_type(display_name)[0] or _DEFAULT_MIME,
        size_bytes=copied,
        sha256=digest.hexdigest(),
        profile=str(profile),
        session_id=str(session_id or ""),
        created_at=time.time(),
    )

    conn = _connect(profile_home)
    try:
        with conn:
            conn.execute(
                """INSERT INTO published_artifacts(
                       artifact_id, filename, mime_type, size_bytes, sha256,
                       profile, session_id, source_path, created_at, status
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'available')""",
                (
                    record.artifact_id, record.filename, record.mime_type,
                    record.size_bytes, record.sha256, record.profile,
                    record.session_id, str(resolved), record.created_at,
                ),
            )
    finally:
        conn.close()
    return record


# --- retrieval ------------------------------------------------------------


def get(profile_home: Path | str, artifact_id: Any) -> Optional[PublishedArtifact]:
    """The record for an id, or ``None``.

    An id that does not match the minted shape is rejected without touching
    the database or the filesystem, so a client cannot probe either with it.
    """
    if not isinstance(artifact_id, str) or not ARTIFACT_ID_RE.match(artifact_id):
        return None
    if not _index_path(profile_home).exists():
        return None
    conn = _connect(profile_home)
    try:
        row = conn.execute(
            "SELECT * FROM published_artifacts WHERE artifact_id=? AND status='available'",
            (artifact_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return PublishedArtifact(
        artifact_id=row["artifact_id"],
        filename=row["filename"],
        mime_type=row["mime_type"],
        size_bytes=int(row["size_bytes"]),
        sha256=row["sha256"],
        profile=row["profile"],
        session_id=row["session_id"],
        created_at=float(row["created_at"]),
    )


def resolve_blob(
    profile_home: Path | str, artifact_id: Any
) -> Optional[tuple[Path, PublishedArtifact]]:
    """The bytes for an id, for the download route.

    Returns ``None`` when the id is unknown, malformed, or recorded but no
    longer on disk — the caller answers 404 for all three, so a probe cannot
    tell them apart.
    """
    record = get(profile_home, artifact_id)
    if record is None:
        return None
    blob = _blob_path(profile_home, record.artifact_id)
    if not blob.is_file():
        return None
    return blob, record


def list_for_session(
    profile_home: Path | str, session_id: str, limit: int = 100
) -> list[PublishedArtifact]:
    """Everything published in one conversation, newest first."""
    if not _index_path(profile_home).exists():
        return []
    conn = _connect(profile_home)
    try:
        rows = conn.execute(
            """SELECT * FROM published_artifacts
               WHERE session_id=? AND status='available'
               ORDER BY created_at DESC LIMIT ?""",
            (str(session_id), int(limit)),
        ).fetchall()
    finally:
        conn.close()
    return [
        PublishedArtifact(
            artifact_id=r["artifact_id"], filename=r["filename"],
            mime_type=r["mime_type"], size_bytes=int(r["size_bytes"]),
            sha256=r["sha256"], profile=r["profile"],
            session_id=r["session_id"], created_at=float(r["created_at"]),
        )
        for r in rows
    ]


# --- what this turn published ------------------------------------------------
#
# A tool cannot reach the agent object — handlers receive `session_id`,
# `task_id` and `user_task`, and nothing else — so `send_file` cannot stamp its
# own reference onto the message the turn is about to write. It leaves it here
# instead, and `finalize_turn` (the one place that runs exactly once per turn,
# for every turn source) collects it.
#
# Keyed by session, so two agents publishing at the same moment cannot collect
# each other's files. Drained on read: a reference belongs to one message.

_pending_lock = _threading.Lock()
_pending: dict[str, list[dict[str, Any]]] = {}


def note_pending(session_id: Any, reference: dict[str, Any]) -> None:
    """Record a publication for the message this turn is about to produce."""
    key = str(session_id or "")
    if not key:
        return
    with _pending_lock:
        _pending.setdefault(key, []).append(dict(reference))


def take_pending(session_id: Any) -> list[dict[str, Any]]:
    """Collect and clear what this turn published. Safe to call every turn."""
    key = str(session_id or "")
    if not key:
        return []
    with _pending_lock:
        return _pending.pop(key, [])


def message_reference(records: Iterable[PublishedArtifact]) -> list[dict[str, Any]]:
    """What a message carries in its ``display_metadata`` sidecar.

    Only what a file card needs to render and what a download needs to
    resolve. The same shape a group event will carry, so Scope 15 reuses this
    identity rather than growing a second one.
    """
    return [
        {
            "artifact_id": record.artifact_id,
            "filename": record.filename,
            "mime_type": record.mime_type,
            "size_bytes": record.size_bytes,
        }
        for record in records
    ]
