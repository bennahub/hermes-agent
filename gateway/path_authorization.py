"""One decision about which filesystem paths may leave this machine.

Two surfaces hand an operator-chosen file to a remote client: the managed-files
read endpoints in ``hermes_cli.web_server`` (``/api/files/read``,
``/api/files/download``) and artifact publication in
``gateway.published_artifacts`` (the ``send_file`` tool). They were written
weeks apart and grew *two* opinions about what a secret looks like — which is
how BWM-794 happened:

* the read side gained a positive allowlist (``_read_allowed_roots`` /
  ``_enforce_read_allowlist``) whose own test asserts ``/etc/hosts`` is
  refused, while the publish side had **no containment at all** — only a
  denylist — so ``send_file('/etc/passwd')`` copied the file into the profile
  store and handed the owner a download id for it;
* the read side's basename list carried ``hosts.yml`` (the GitHub CLI token
  store at ``~/.config/gh/hosts.yml`` on this deployment) and ``id_ecdsa``;
  the publish side's did not. On the live host, ``send_file`` on that token
  store succeeded.

A second denylist is a denylist that drifts, and this one already had. So the
lists and the containment rule live here, once, and both surfaces call in.
Neither keeps a private copy.

Deliberately dependency-free: no FastAPI, no ``hermes_cli`` import, nothing
that a tool handler running inside an agent turn would have to drag in. It
lives under ``gateway/`` because that is the direction imports already run
(``hermes_cli.web_server`` imports ``gateway.*`` at module scope; nothing in
``gateway`` imports ``hermes_cli``).

The two surfaces still differ in how they *say no* — one raises
``HTTPException(403)``, the other ``PublishRefused`` — and that is left alone.
The shared part is the judgement, not the refusal.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Iterable

#: The container/hosted layout's data root. Defined here so the read endpoints
#: and the allowlist cannot disagree about it.
HOSTED_MANAGED_FILES_ROOT = Path("/opt/data")


# --- what a secret is -----------------------------------------------------

#: Basenames that are credential material wherever they sit. The union of the
#: two lists that existed before: ``hermes_cli.web_server``'s
#: ``_SENSITIVE_MANAGED_FILE_BASENAMES`` (itself mirroring
#: ``agent.file_safety.get_read_block_error`` and
#: ``gateway.platforms.base._ROOT_CREDENTIAL_FILES``) and
#: ``gateway.published_artifacts``'s ``_SENSITIVE_NAMES``. Each list knew
#: something the other did not; keeping them apart is what let ``send_file``
#: publish ``.config/gh/hosts.yml``.
SENSITIVE_FILE_BASENAMES = frozenset({
    # Hermes' own credential stores
    "auth.json",
    "auth_pool.json",
    "auth.lock",
    "credentials",
    "config.yaml",
    ".anthropic_oauth.json",
    "google_token.json",
    "google_client_secret.json",
    "google_oauth.json",
    "google_oauth_pending.json",
    "webhook_subscriptions.json",
    "bws_cache.json",
    "bws_cache.enc.json",
    # git's credential-store helper cache
    ".git-credentials",
    # generic UNIX credential files
    ".netrc",
    # private key material. The interesting keys are rarely named `id_rsa`,
    # which is why SENSITIVE_DIR_NAMES denies the whole `.ssh` tree as well.
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    # the GitHub CLI token store: `~/.config/gh/hosts.yml`. The read side
    # carried this and the publish side did not — BWM-794's live case.
    "hosts.yml",
})

#: Directory names whose entire subtree is credential material, matched on any
#: path component so they are denied wherever they appear.
SENSITIVE_DIR_NAMES = frozenset({
    "mcp-tokens",
    "pairing",
    ".ssh",
    ".gnupg",
})

#: Basename *prefixes*. ``.env`` covers ``.envrc`` and ``.env.local``;
#: ``google_oauth`` and ``bws_cache`` cover the suffixed variants Hermes
#: writes.
SENSITIVE_NAME_PREFIXES = (".env", "google_oauth", "bws_cache")

#: Separators an operator's backup copy puts after the original name.
#: ``auth.json.bak-codex-fix`` and ``config.yaml.bak-precopilot-removal`` both
#: exist on this deployment, beside the live files they copy. Matching the
#: exact name only would refuse the original and release its byte-identical
#: twin — and publication *launders* it, because the stored blob is named by a
#: 32-hex id that matches no denylist anywhere.
#:
#: Kept to the two suffixes that are unambiguously a copy of the file they
#: follow — ``auth.json.bak-…`` and ``auth.json~``. ``-`` and ``_`` are not
#: here on purpose: they would refuse ``credentials-2026.csv``, a name an agent
#: may legitimately produce, for no gain against a real credential store.
_BACKUP_SEPARATORS = (".", "~")


def is_sensitive_filename(name: str) -> bool:
    """Whether this basename is credential material.

    Case-insensitive, so ``Auth.JSON`` on a case-insensitive mount cannot slip
    past, and prefix-aware for backup copies (see ``_BACKUP_SEPARATORS``).
    """
    lowered = str(name).lower()
    if not lowered:
        return False
    for blocked in SENSITIVE_FILE_BASENAMES:
        if lowered == blocked:
            return True
        if any(lowered.startswith(blocked + sep) for sep in _BACKUP_SEPARATORS):
            return True
    return any(lowered.startswith(prefix) for prefix in SENSITIVE_NAME_PREFIXES)


def is_sensitive_path(path: Path | str) -> bool:
    """Whether this path is credential material, by name or by tree."""
    candidate = Path(path)
    if is_sensitive_filename(candidate.name):
        return True
    return any(part.lower() in SENSITIVE_DIR_NAMES for part in candidate.parts)


# --- where a file may come from -------------------------------------------


def path_is_under(root: Path, target: Path) -> bool:
    """Containment, on already-resolved paths.

    Both callers resolve first; a check made on an unresolved path is a check a
    symlink walks straight through.
    """
    return target == root or root in target.parents


def read_allowed_roots() -> tuple[Path, ...]:
    """Trees a file may be served or published from when no root is configured.

    With no ``HERMES_DASHBOARD_FILES_ROOT`` and a non-``/opt/data`` layout —
    the live deployment — ``_managed_files_policy`` returns ``locked_root=None``
    and containment is skipped entirely, so every absolute path the runtime
    user can read was reachable, bounded only by a denylist and a size cap.

    Locking the *policy* root would have been the obvious fix and is wrong: it
    also locks browsing, and it breaks Desktop, which builds download URLs from
    arbitrary gateway-local paths (its own test uses ``/tmp/a b.png``). So this
    is an allowlist covering every consumer actually enumerated:

    * the runtime user's home — Asera's ``profiles/<name>/…``, and the agent
      workspaces deliverables are written into (``/home/hermes/bennahub-cmo/…``,
      the case ``send_file`` exists for);
    * the system temp directory — Desktop's media previews, and the scratch
      space an agent legitimately generates a report in;
    * ``/opt/data`` where that hosted layout exists.

    What it removes is everything else: ``/etc``, ``/root``, ``/var``, another
    user's home.

    Resolved at call time, not import time: the tests monkeypatch
    ``Path.home``, and a gateway that changes ``HOME`` between turns must not
    be judged against a root captured at import.
    """
    roots: list[Path] = []
    seen: set[Path] = set()
    # `/tmp` is listed beside `gettempdir()` deliberately. On macOS the latter
    # is `$TMPDIR` (`/var/folders/…/T`), so a Mac-hosted gateway would refuse
    # the very path this design cites as the case it must not break. On Linux
    # the two coincide and the duplicate is dropped.
    for candidate in (
        Path.home(),
        Path(tempfile.gettempdir()),
        Path("/tmp"),
        HOSTED_MANAGED_FILES_ROOT,
    ):
        try:
            resolved = candidate.expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if resolved in seen:
            continue
        if resolved.exists():
            seen.add(resolved)
            roots.append(resolved)
    return tuple(roots)


def is_within_allowed_roots(
    target: Path, *, extra_roots: Iterable[Path | str] = ()
) -> bool:
    """Whether an already-resolved path sits inside an allowed tree.

    ``extra_roots`` is for a caller that owns a tree the shared list cannot
    know about — ``published_artifacts`` passes the profile home, which is
    under ``HOME`` on this deployment but need not be on another.

    Returns ``True`` when nothing resolvable can be compared against, which is
    the pre-existing fail-open the read side already chose: a host with no
    home, no temp directory and no ``/opt/data`` is not a host where this
    check is the thing standing between the owner and their files.
    """
    roots = list(read_allowed_roots())
    for extra in extra_roots:
        try:
            resolved = Path(extra).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if resolved.exists():
            roots.append(resolved)
    if not roots:
        return True
    return any(path_is_under(root, target) for root in roots)
