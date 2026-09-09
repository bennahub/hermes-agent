"""Filename search over the attachments Hermes staged for a profile.

Why this module exists
----------------------
Nothing in ``state.db`` has an attachments table, and nothing needs one: the
attach RPCs already persist everything a filename search needs.

* ``image.attach_bytes`` / ``image.attach`` / ``pdf.attach`` write the bytes
  into ``<profile_home>/images/`` (``tui_gateway.server._queue_attached_image``)
  and ``prompt.submit`` persists the turn through
  ``tui_gateway.server._build_persist_message_with_image_refs``, which appends
  one ``@image:<absolute path>`` directive per attachment to the *user*
  message's ``content``.
* ``file.attach`` copies/uploads the bytes into ``<profile_home>/attachments/``
  (``tui_gateway.server._stage_session_file_attachment``) and hands the client
  an ``@file:<ref>`` directive that lands in the same ``content`` column.

``messages.content`` is column 1 of the ``messages_fts`` FTS5 index
(``hermes_state_common.FTS_SQL``), so those filenames are *already* indexed and
already searchable — the existing ``/api/sessions/search`` simply reports the
hit as a session rather than as a file. This module turns already-matched user
rows into file rows.

Security contract
-----------------
A result may only ever name a file that Hermes' own attach pipeline staged
into the profile's attachment store. :func:`resolve_attachment_path` accepts a
persisted directive value **only** when it is absolute or anchored to the native
session cwd and, once fully
resolved (symlinks included), lies inside ``<profile_home>/attachments`` or
``<profile_home>/images``. Everything else is dropped: relative refs, ``..``
segments that escape the profile store, absolute paths elsewhere, and symlinks escaping the
roots. Relative refs without a persisted cwd are refused. Nothing here lists, globs or walks a directory — the candidate set comes
entirely from strings already stored in ``state.db``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from agent.context_references import (
    _parse_file_reference_value,
    _strip_reference_wrappers,
    _strip_trailing_punctuation,
)

#: The two attachment directive forms Hermes persists. Mirrors the value
#: grammar of ``agent.context_references.REFERENCE_PATTERN`` (which handles
#: ``@file:`` but not ``@image:`` — that prefix is written by the gateway and
#: read by the desktop renderer, never by the built-in reference parser).
_ATTACHMENT_DIRECTIVE_RE = re.compile(
    r"(?<![\w/])@(?P<kind>image|file):"
    r"(?P<value>`[^`\n]+`|\"[^\"\n]+\"|'[^'\n]+'|\S+)"
)

#: Profile-home sub-directories that hold owner-visible attachments. These are
#: exactly the dirs ``_desktop_attachment_dir`` and ``_session_images_dir``
#: stage into.
ATTACHMENT_ROOT_NAMES: Tuple[str, ...] = ("attachments", "images")

#: FTS5 operators/wildcards stripped before a term is matched against a
#: filename. Filename matching is plain case-insensitive substring — the FTS
#: pass has already done the indexed work.
_QUERY_NOISE = '"*()`\''
_QUERY_OPERATORS = frozenset({"AND", "OR", "NOT", "NEAR"})


def attachment_roots(profile_home: Any) -> Tuple[Path, ...]:
    """Resolved ``<profile_home>/{attachments,images}``.

    Resolution is best-effort per root so a profile that has never received an
    attachment (no such directory yet) still yields a usable, empty search.
    """
    base = Path(profile_home).resolve()
    roots: List[Path] = []
    for name in ATTACHMENT_ROOT_NAMES:
        try:
            lexical = base / name
            # The canonical stores are directories, not aliases. In default
            # HOME, another profile is nested under the same base: mere base
            # containment would accept a root symlink into that profile.
            if lexical.is_symlink():
                continue
            root = lexical.resolve()
            if root == lexical:
                roots.append(root)
        except OSError:  # pragma: no cover - unreadable parent
            continue
    return tuple(roots)


def _is_inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def resolve_attachment_path(target: str, roots: Sequence[Path], workspace: Optional[str] = None) -> Optional[Path]:
    """Resolve a persisted directive value to a file under *roots*, else None.

    Relative refs require the native session cwd. The fully resolved path is
    contained by one of the profile's attachment roots. A relative ref without
    a native cwd, traversal outside the roots, or an absolute path outside the roots
    (``/etc/passwd``) and a symlink pointing out of them all resolve to None,
    so no such path can ever reach a response.
    """
    raw = (target or "").strip()
    if not raw or not roots:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        if not workspace or not Path(workspace).is_absolute():
            return None
        candidate = Path(workspace) / candidate
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        return None
    for root in roots:
        if _is_inside(root, resolved):
            return resolved
    return None


def iter_attachment_directives(text: str) -> Iterator[Tuple[str, str]]:
    """Yield ``(kind, target)`` for each ``@image:``/``@file:`` in *text*."""
    if not text:
        return
    for match in _ATTACHMENT_DIRECTIVE_RE.finditer(text):
        kind = match.group("kind")
        value = _strip_trailing_punctuation(match.group("value") or "")
        if kind == "file":
            target, _start, _end = _parse_file_reference_value(value)
        else:
            target = _strip_reference_wrappers(value)
        if target:
            yield kind, target


def query_terms(query: str) -> List[str]:
    """Lowercased substring terms a filename must contain to be returned."""
    terms: List[str] = []
    for raw in re.findall(r'"[^"]*"|\S+', query or ""):
        if raw.upper() in _QUERY_OPERATORS:
            continue
        term = raw.strip(_QUERY_NOISE).strip().lower()
        if term:
            terms.append(term)
    return terms


def name_matches(name: str, terms: Sequence[str]) -> bool:
    """True when every term appears in *name* (case-insensitive substring)."""
    if not terms:
        return False
    lowered = (name or "").lower()
    return all(term in lowered for term in terms)


def _stat(path: Path) -> Tuple[Optional[int], bool]:
    """Size/existence of an already-policy-approved path. Never raises.

    Called only for a path :func:`resolve_attachment_path` has already
    confined to the profile's attachment roots, so this cannot be used to
    probe arbitrary locations.
    """
    try:
        return path.stat().st_size, True
    except OSError:
        return None, False


def build_file_results(
    matches: Iterable[Dict[str, Any]],
    texts: Dict[int, str],
    *,
    roots: Sequence[Path],
    terms: Sequence[str],
    is_sensitive: Any = None,
    workspaces: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Turn matched user-message rows into deduped, policy-filtered file rows.

    ``matches`` are rows from ``SessionDB.search_messages`` (already profile-,
    source- and role-scoped); ``texts`` maps message id to that row's plain
    text. Ordering is newest-attached first, then by name, so paging over the
    result is stable.
    """
    rows: List[Dict[str, Any]] = []
    seen: set = set()
    for match in matches:
        message_id = match.get("id")
        text = texts.get(message_id)
        if not text:
            continue
        for kind, target in iter_attachment_directives(text):
            resolved = resolve_attachment_path(target, roots, (workspaces or {}).get(match.get("session_id")))
            if resolved is None:
                continue
            if is_sensitive is not None and is_sensitive(resolved):
                continue
            if not name_matches(resolved.name, terms):
                continue
            key = str(resolved)
            if key in seen:
                continue
            seen.add(key)
            size, exists = _stat(resolved)
            rows.append(
                {
                    "kind": "file",
                    "name": resolved.name,
                    "path": key,
                    "attachment_kind": kind,
                    "session_id": match.get("session_id"),
                    "message_id": message_id,
                    "attached_at": match.get("timestamp"),
                    "source": match.get("source"),
                    "model": match.get("model"),
                    "session_started": match.get("session_started"),
                    "size": size,
                    "exists": exists,
                }
            )
    rows.sort(key=lambda row: (-(row["attached_at"] or 0), row["name"], row["path"]))
    return rows


def search_profile_files(db, query: str, *, limit: int, offset: int, include_sources, exclude_sources):
    """Project bounded native FTS matches without listing directories or changing state."""
    from gateway.path_authorization import is_sensitive_path

    terms = query_terms(query)
    envelope = {"results": [], "kind": "file", "limit": limit, "offset": offset, "has_more": False}
    if not terms:
        return envelope
    window = min(max((offset + limit) * 5, 100), 1000)
    prefix_query = " ".join(
        token if token.startswith('"') or token.endswith("*") else token + "*"
        for token in re.findall(r'"[^"]*"|\S+', query.strip()))
    matches = db.search_messages(
        query=prefix_query, source_filter=include_sources, exclude_sources=exclude_sources or None,
        role_filter=["user"], limit=window, sort="newest",
        fields=("id", "session_id", "timestamp", "source", "model", "session_started"))
    texts = db.get_message_texts([row["id"] for row in matches], roles=("user",))
    # file.attach returns a workspace-relative ref when possible. Resolve it
    # only against the authoritative persisted session cwd, never process cwd.
    workspaces = {}
    for sid in {row["session_id"] for row in matches}:
        session = db.get_session(sid) or {}
        workspaces[sid] = session.get("cwd")
    rows = build_file_results(matches, texts, roots=attachment_roots(Path(db.db_path).parent),
                              terms=terms, is_sensitive=is_sensitive_path, workspaces=workspaces)
    return dict(envelope, results=rows[offset:offset + limit], has_more=len(rows) > offset + limit,
                candidate_window_truncated=len(matches) == window)
