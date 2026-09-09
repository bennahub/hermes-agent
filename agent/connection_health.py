"""Canonical Connection fault taxonomy — one owner-facing verdict for every credential failure.

Detection of *what* failed lives in :mod:`agent.error_classifier` (``FailoverReason.auth`` /
``auth_permanent``); this module answers the two questions every surface then asks:

    * which canonical Connection row broke, and
    * what — if anything — only the Owner can do about it.

The :class:`ConnectionBlock` rides the turn result exactly like ``billing_block`` does, so the
agent transcript, Connections UI, Needs You and the task record all render one structured signal
instead of re-parsing provider prose.

Two hard invariants, both owner-visible defects when broken:

1. **No raw provider text ever leaves here.** ``HTTP 401: OAuth access token has been revoked.``,
   provider JSON, refresh exceptions and stack traces are diagnostics — they belong in the log,
   never in an agent's final response. Only ``provider_status`` (the integer) and a normalized
   ``reason_code`` cross the boundary.
2. **The reason code is structural, not textual.** Clients localize from ``reason_code`` +
   ``owner_action``; they must never string-match a message to decide what button to show.
"""

from __future__ import annotations

import contextlib
import threading
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

# ── Reason codes (structural; clients localize from these, never from ``message``) ──

REASON_REVOKED = "credential_revoked"          # Provider says the grant is gone. Only the Owner can re-consent.
REASON_EXPIRED = "credential_expired"          # Access token expired and no usable refresh remains.
REASON_REFRESH_FAILED = "refresh_failed"       # A refresh was attempted and the provider rejected it.
REASON_MISSING = "credential_missing"          # Nothing is configured for this Connection at all.
REASON_INVALID = "credential_invalid"          # A credential is present but the provider rejects it.
REASON_FORBIDDEN = "credential_forbidden"      # Authenticated, but the grant lacks the required scope/entitlement.

# ── Owner actions (what the Connections UI must offer) ──

ACTION_REAUTHORIZE = "reauthorize"   # Re-run the provider's consent flow. Owner-only; Hermes cannot do this.
ACTION_CONFIGURE = "configure"       # Supply a credential in Connections (an API key, a client JSON).
ACTION_NONE = "none"                 # Nothing for the Owner to do; Hermes recovers on its own.

# Reason → (owner action, whether a bare retry can plausibly succeed).
# ``retryable`` is deliberately False for every Owner-gated reason: showing a Retry button that
# cannot work is how a broken Connection gets mistaken for a flaky one.
_REASON_POLICY: Dict[str, tuple] = {
    REASON_REVOKED: (ACTION_REAUTHORIZE, False),
    REASON_EXPIRED: (ACTION_REAUTHORIZE, False),
    REASON_REFRESH_FAILED: (ACTION_REAUTHORIZE, False),
    REASON_MISSING: (ACTION_CONFIGURE, False),
    REASON_INVALID: (ACTION_CONFIGURE, False),
    REASON_FORBIDDEN: (ACTION_REAUTHORIZE, False),
}

# Provider phrasing → reason. Matched against the LOWERCASED provider message; the matched text is
# never propagated. Ordered: the most specific verdict must win over the generic ones.
_REASON_PATTERNS: tuple = (
    (REASON_REVOKED, (
        "has been revoked", "token revoked", "token has been revoked", "grant was revoked",
        "authorization revoked", "consent revoked", "access_denied",
    )),
    (REASON_REFRESH_FAILED, ("invalid_grant", "refresh token", "refresh_token is invalid")),
    (REASON_EXPIRED, ("token expired", "token has expired", "expired_token", "jwt expired")),
    (REASON_MISSING, (
        "no credentials", "no api key", "missing api key", "missing credentials",
        "authorization header", "no authorization",
    )),
    (REASON_FORBIDDEN, ("insufficient scope", "missing scope", "not authorized", "forbidden")),
)


def classify_reason(message: str, status_code: Optional[int] = None) -> str:
    """Structural reason for an auth-classified failure.

    Falls back to ``credential_invalid`` for a 401 with unrecognized prose (a present credential the
    provider rejects) and ``credential_forbidden`` for a 403 — never to a text passthrough.
    """
    text = str(message or "").lower()
    for reason, needles in _REASON_PATTERNS:
        if any(needle in text for needle in needles):
            return reason
    return REASON_FORBIDDEN if status_code == 403 else REASON_INVALID


# ── Canonical Connection identity ──
#
# The runtime resolves an Anthropic token from several stores in priority order
# (:func:`agent.anthropic_credentials.resolve_anthropic_token`). Connections must name the row the
# runtime is ACTUALLY using, or the two projections disagree — which is exactly how a revoked
# borrowed Claude Code grant surfaced as an unrelated "Anthropic API Key: sign-in required" row.

_ENV_BACKED_ANTHROPIC = ("ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")


def _anthropic_connection_id() -> str:
    """``claude-code`` when the borrowed Claude Code singleton is what the resolver would use.

    Mirrors the resolver's own precedence: an explicit env credential wins, EXCEPT that a static env
    OAuth token defers to the refreshable Claude Code file (``_prefer_refreshable_claude_code_token``).
    Fail-soft: any error resolves to the plain provider row.
    """
    try:
        from agent.anthropic_credentials import (
            _getenv, _is_oauth_token, read_claude_code_credentials,
        )
    except Exception:
        return "anthropic"
    try:
        creds = read_claude_code_credentials() or {}
        borrowed = bool(creds.get("accessToken"))
        for name in _ENV_BACKED_ANTHROPIC:
            value = (_getenv(name) or "").strip()
            if not value:
                continue
            # A static env OAuth token defers to the refreshable file when one exists.
            if borrowed and creds.get("refreshToken") and _is_oauth_token(value):
                return "claude-code"
            return "anthropic"
        return "claude-code" if borrowed else "anthropic"
    except Exception:
        return "anthropic"


def connection_id_for(provider: str, base_url: str = "") -> str:
    """Canonical Connections row id backing *provider* right now (the id the client deep-links to)."""
    slug = (provider or "").strip().lower()
    if slug == "anthropic":
        return _anthropic_connection_id()
    return slug or "unknown"


# ── The descriptor ──


@dataclass
class ConnectionBlock:
    """Structured Connection-fault descriptor shared across every surface.

    ``connection_id`` is the canonical Connections row id, so a client can deep-link straight into
    the reconnect flow instead of dumping the Owner at the top of a settings list.
    """

    provider: str
    provider_label: str
    connection_id: str
    scope: str
    reason_code: str
    owner_action: str
    retryable: bool
    provider_status: Optional[int]
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


def owner_safe_message(provider_label: str, reason_code: str) -> str:
    """Neutral, provider-body-free English fallback.

    Localized clients render from ``reason_code`` + ``owner_action``; this string exists only for
    surfaces with no localization (the CLI, logs, plain-text transports).
    """
    if reason_code == REASON_MISSING:
        return f"{provider_label} is not connected yet. Add it in Connections to continue."
    if reason_code == REASON_INVALID:
        return f"The saved {provider_label} credential was rejected. Update it in Connections."
    if reason_code == REASON_FORBIDDEN:
        return (f"The {provider_label} connection is missing a permission this action needs. "
                "Reconnect it in Connections to grant it.")
    return f"The {provider_label} connection was disconnected and needs to be reconnected."


def build_connection_block(
    *, provider: str, base_url: str = "", reason_code: str = "", message: str = "",
    status_code: Optional[int] = None, scope: str = "", connection_id: str = "",
    provider_label: str = "",
) -> ConnectionBlock:
    """Connection descriptor for an auth-classified failure.

    *message* is the RAW provider text and is used only to classify — it is never stored on the
    block. Pass ``reason_code`` explicitly when the caller already knows the verdict (a failed
    refresh, an empty credential store); otherwise it is derived from the provider text.

    ``scope`` defaults to the scope that OWNS the credential (:func:`owning_scope`), which for a
    globally shared grant is ``default`` no matter which profile observed the failure — never to the
    observing profile, which is how the fault used to become invisible to Connections.

    ``connection_id`` / ``provider_label`` are for Connections rows whose id is NOT a provider slug.
    An MCP server row is keyed by its configured name verbatim (``build_inventory`` writes
    ``row(name, "mcp", name, ...)``), and lower-casing it here would address a row nothing draws —
    the same invisible-fault failure mode, one kind over.
    """
    slug = (provider or "").strip().lower()
    label = (provider_label or "").strip()
    if not label:
        try:
            from agent.billing_links import provider_display_label
            label = provider_display_label(slug, base_url)
        except Exception:
            label = slug.replace("_", " ").replace("-", " ").strip().title() or "your provider"
    reason = reason_code or classify_reason(message, status_code)
    owner_action, retryable = _REASON_POLICY.get(reason, (ACTION_REAUTHORIZE, False))
    connection_id = str(connection_id or "").strip() or connection_id_for(slug, base_url)
    return ConnectionBlock(
        provider=slug, provider_label=label, connection_id=connection_id,
        scope=scope or owning_scope(connection_id), reason_code=reason, owner_action=owner_action,
        retryable=bool(retryable), provider_status=status_code,
        message=owner_safe_message(label, reason),
    )


def connection_block_dict(
    *, provider: str, base_url: str = "", reason_code: str = "", message: str = "",
    status_code: Optional[int] = None, scope: str = "", connection_id: str = "",
    provider_label: str = "",
) -> Optional[dict]:
    """Best-effort :func:`build_connection_block` as a dict (None if the descriptor cannot be built).

    Fail-soft by design: a missing descriptor degrades the UI to a generic connection notice, but it
    must never turn a handled auth failure back into an unhandled crash.
    """
    try:
        return build_connection_block(
            provider=provider, base_url=str(base_url or ""), reason_code=reason_code,
            message=message, status_code=status_code, scope=scope,
            connection_id=connection_id, provider_label=provider_label,
        ).to_dict()
    except Exception:
        return None


# ── Scope: which Connections row a fault belongs to ──
#
# A credential's OWNER is not "whoever happened to observe the failure". On this deployment 16 named
# profiles borrow ONE Claude Code grant from ``~/.claude/.credentials.json`` — a single file every
# profile reads verbatim. A fault observed while running as profile ``badr`` is therefore a fault for
# every profile, and above all for the ``default`` row the Connections screen actually renders.

GLOBAL_SCOPE = "default"

#: Connections whose credential is a process-global singleton file rather than a per-profile
#: ``auth.json`` row. ``claude-code`` is ``~/.claude/.credentials.json``
#: (:func:`agent.anthropic_credentials.claude_code_credentials_path` — "every profile reads/writes
#: this same path"). ``hermes_cli.connections.singleton_account_state`` projects exactly this set, so
#: the two sides cannot drift.
SINGLETON_BACKED_CONNECTIONS = frozenset({"claude-code"})


def current_profile_scope() -> str:
    """Connections scope the runtime is serving right now: a profile name, or ``default``.

    The effective Hermes home (the process ``HERMES_HOME``, or a ``scope_context()`` override when
    one is installed) measured against the Hermes ROOT, which never moves. Both readings are the
    profile whose credentials this turn is actually using, which is what ownership turns on.
    """
    try:
        from pathlib import Path
        from hermes_constants import get_hermes_home, get_default_hermes_root
        home = Path(get_hermes_home()).expanduser().resolve(strict=False)
        root = Path(get_default_hermes_root()).expanduser().resolve(strict=False)
        if home == root or home.parent != root / "profiles":
            return GLOBAL_SCOPE
        return home.name or GLOBAL_SCOPE
    except Exception:
        return GLOBAL_SCOPE


def _profile_owns_credential(scope: str, connection_id: str) -> bool:
    """True only when profile *scope*'s OWN ``auth.json`` supplies this provider's credential.

    Mirrors ``build_inventory``'s profile loop exactly: a profile with no local credential renders no
    account row of its own (it falls through to the global row), so a fault recorded against the
    profile there would address a row nothing draws — invisible, which is the whole defect.
    Presence only; no credential value is read, compared or logged.
    """
    try:
        import json
        from pathlib import Path
        from hermes_cli.connections import credential_state
        from hermes_cli.profiles import get_profile_dir
        raw = json.loads((Path(get_profile_dir(scope)) / "auth.json").read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return False
        pools, providers = raw.get("credential_pool") or {}, raw.get("providers") or {}
        if not isinstance(pools, dict) or not isinstance(providers, dict):
            return False
        return bool(credential_state(pools.get(connection_id) or [], providers.get(connection_id))[1])
    except Exception:
        return False


def owning_scope(connection_id: str) -> str:
    """The Connections scope that owns *connection_id*'s credential right now.

    ``default`` (the global row) for a singleton-backed connection and whenever the runtime is the
    default profile. A named profile owns the row only when its own store supplies the credential.

    Every uncertainty resolves to ``default`` on purpose: the global row is the one that is always
    rendered, so an over-visible fault degrades to a reconnect prompt the Owner can dismiss, whereas
    an invisible one is exactly the 44-hour outage this module exists to prevent.
    """
    key = str(connection_id or "").strip()
    if not key or key in SINGLETON_BACKED_CONNECTIONS:
        return GLOBAL_SCOPE
    scope = current_profile_scope()
    if scope == GLOBAL_SCOPE:
        return GLOBAL_SCOPE
    return scope if _profile_owns_credential(scope, key) else GLOBAL_SCOPE


# ── The canonical health store ──
#
# The runtime learns a Connection is broken by *using* it; the Connections screen learns by reading
# local metadata. Before this store the two never met: the runtime kept reporting a revoked grant
# while Connections showed a permanently static "external gate" row with no reconnect action.
# Whoever observes a fault records it here, and every projection reads it — one canonical state.
#
# ONE FILE, at the Hermes ROOT. ``get_default_hermes_root()`` reads the process ``HERMES_HOME`` only
# and deliberately ignores the context-local override ``scope_context()`` installs, so ``<root>``,
# ``<root>/profiles/badr`` and a request scoped into any profile all resolve the identical path —
# and it is the path ``build_inventory`` reads, because that runs inside ``scope_context()``
# (default), whose home IS the root. Anchoring to ``get_hermes_home()`` instead put a fault observed
# by profile ``badr`` in ``<root>/profiles/badr/state/`` where the Connections projection never looks:
# the two projections still could not agree, and Connections would have shown that row healthy for
# the whole outage.
#
# Entries are keyed ``<owning scope>/<connection id>`` so the global/profile distinction survives in
# one file: a fault on a genuinely profile-local credential still cannot make another profile's row
# look broken.
#
# Secret-free by construction: only ids, reason codes and timestamps are persisted.

_HEALTH_STORE_VERSION = 2
_HEALTH_STORE_COMMENT = (
    "Canonical Connection health observed by the Hermes runtime. Secret-free: connection ids, "
    "structural reason codes and timestamps only. One file at the Hermes root, keyed "
    "'<scope>/<connection id>', so every profile and every projection reads the same state and the "
    "UI and the agent runtime never disagree about whether a connection works."
)
_HEALTH_FIELDS = (
    "provider", "provider_label", "connection_id", "scope", "reason_code",
    "owner_action", "retryable", "provider_status",
)


def health_store_path():
    """``<hermes root>/state/connection_health.json`` — the one path every scope resolves identically.

    NOT ``<HERMES_HOME>/state/...``: the credential behind this store is global (one Claude Code
    grant shared by every profile), so its health must be global too. See the block comment above.
    """
    from hermes_constants import get_default_hermes_root
    from pathlib import Path
    return Path(get_default_hermes_root()) / "state" / "connection_health.json"


def _store_key(connection_id: str, scope: str) -> str:
    return f"{str(scope or GLOBAL_SCOPE).strip() or GLOBAL_SCOPE}/{str(connection_id).strip()}"


def _read_health_store() -> Dict[str, Any]:
    import json
    try:
        raw = json.loads(health_store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    faults = raw.get("faults") if isinstance(raw, dict) else None
    if not isinstance(faults, dict):
        return {}
    # ``"/" in k`` also drops any v1 entry keyed by a bare connection id: those predate the
    # scope-keyed store, can never match a lookup now, and dropping them on read means the next
    # write garbage-collects them instead of carrying them forever.
    return {k: v for k, v in faults.items() if isinstance(k, str) and "/" in k and isinstance(v, dict)}


# ── Orphaned rows: a fault whose scope names a profile that no longer exists ──
#
# ``record_healthy`` pops exactly ONE key, and MCP faults are recorded under
# ``current_profile_scope()`` unconditionally, so a per-profile MCP fault is keyed
# ``<profile>/<id>``. Delete that profile and the key becomes unreachable: ``owning_scope`` falls
# back to ``default`` once the profile's ``auth.json`` is gone, the only clearer for an MCP row is a
# successful call *running as that profile*, and there is no such profile any more. The row is then
# permanent, invisible to ``observed_fault`` (which reads the ``default`` key), and — because the
# respawn guard is scope-agnostic and answers ``bool(faults)`` for an unstamped task — it stops
# autonomous dispatch for every task, forever, with no Owner-visible cause and no in-product remedy.
#
# So an orphan is not merely stale: it is a fault no Owner can act on. The Connections screen cannot
# draw a row for a profile that is gone, so there is nothing to press. Treating it as open is the
# permanent stall; treating it as closed loses nothing, because nothing could ever close it.
#
# Deliberately fail-SAFE in the *keep* direction. Every uncertainty — an unreadable profiles root, a
# name that will not normalise, an import that fails — answers "live" and keeps the row. An
# over-visible fault degrades to a reconnect prompt the Owner can dismiss; a wrongly-dropped one is
# a broken connection reading healthy, which is the outage this module exists to prevent.


def _scope_is_live(scope: str) -> bool:
    """True unless *scope* names a profile that no longer exists."""
    name = str(scope or "").strip()
    if not name or name == GLOBAL_SCOPE:
        return True  # the global row is always rendered and can never be orphaned
    try:
        from hermes_cli.profiles import profile_exists
        return bool(profile_exists(name))
    except Exception:
        return True


def _live_faults(faults: Dict[str, Any]) -> Dict[str, Any]:
    """*faults* without the rows whose scope no longer names a live profile.

    One ``profile_exists`` probe per distinct scope, not per row: this deployment runs 16 profiles
    against one store, and the healthy path never reaches here at all (``_known_empty``'s ``stat``
    short-circuits it).
    """
    live: Dict[str, Any] = {}
    known: Dict[str, bool] = {}
    for key, entry in faults.items():
        scope = key.split("/", 1)[0]
        if scope not in known:
            known[scope] = _scope_is_live(scope)
        if known[scope]:
            live[key] = entry
    return live


def open_faults() -> Dict[str, Any]:
    """Every recorded fault an Owner can still act on — the store minus its orphans.

    The reading that anything *gating work* on connection health must use. Reading the raw store
    there is what lets a deleted profile hold the dispatch queue open permanently.
    """
    return _live_faults(_read_health_store())


def forget_scope(scope: str) -> int:
    """Drop every fault recorded under *scope*; returns how many rows went. Fail-soft.

    Called when a profile is deleted, so the store does not accumulate rows addressed to agents that
    are gone. This is hygiene, not the safety net — ``_live_faults`` is, because a profile can also
    be removed by something that never runs this (a hand ``rm -rf``, a restore from a backup taken
    before it existed, a ``delete_profile`` that got as far as the tombstone and then failed).
    """
    name = str(scope or "").strip()
    if not name or name == GLOBAL_SCOPE:
        return 0
    try:
        faults = _read_health_store()
        keep = {k: v for k, v in faults.items() if k.split("/", 1)[0] != name}
        dropped = len(faults) - len(keep)
        if dropped:
            _write_health_store(keep)
            _invalidate_no_faults()
        return dropped
    except Exception:
        return 0


def _write_health_store(faults: Dict[str, Any]) -> None:
    import json
    import os
    import stat
    path = health_store_path()
    payload = json.dumps(
        {"version": _HEALTH_STORE_VERSION, "comment": _HEALTH_STORE_COMMENT, "faults": faults}, indent=2
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    # Per-writer temp name. The store is now shared by every profile on the host, so a fixed
    # ``.tmp`` would let two concurrent writers interleave into one file and ``os.replace`` a
    # half-written store into place. O_EXCL + a random suffix gives each writer its own inode; the
    # 0600 mode is set at open() so the file is never briefly umask-readable, and survives the
    # rename onto an existing store.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{os.urandom(6).hex()}.tmp")
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(str(tmp))
        raise


def record_fault(block: Optional[Dict[str, Any]], *, scope: str = "") -> None:
    """Persist an observed Connection fault. Fail-soft — never breaks the turn that reported it.

    The owning scope comes from *scope*, else the block's own ``scope``, else :func:`owning_scope`.
    """
    if not isinstance(block, dict) or not block.get("connection_id"):
        return
    import time
    try:
        connection_id = str(block["connection_id"])
        key = _store_key(connection_id, scope or block.get("scope") or owning_scope(connection_id))
        faults = _read_health_store()
        entry = {k: block.get(k) for k in _HEALTH_FIELDS}
        entry["scope"] = key.split("/", 1)[0]
        previous = faults.get(key)
        # Preserve first_seen_at across repeats so the UI can say how long this has been broken.
        entry["first_seen_at"] = (
            previous.get("first_seen_at") if isinstance(previous, dict) and previous.get("reason_code") == entry["reason_code"]
            else time.time()
        ) or time.time()
        entry["last_seen_at"] = time.time()
        faults[key] = entry
        _write_health_store(faults)
        _invalidate_no_faults()
    except Exception:
        pass


def record_healthy(connection_id: str, scope: str = "") -> None:
    """Clear a recorded fault after the connection is observed working again.

    This is the recovery half of the contract, and it is not optional: without it a reconnected
    account stays marked broken forever, which is the same divergence as before with the signs
    reversed. Called on every successful turn, so the common case is short-circuited by a ``stat``
    of the store rather than a read-and-parse.

    Resolves the owning scope exactly as :func:`record_fault` does, so a fault can never be written
    under one key and looked for under another.

    It also sweeps out orphaned rows on its way past — a fault keyed to a profile that no longer
    exists, which this one-key pop can by construction never reach. That sweep is what makes the
    recovery contract total: every route back to healthy runs through here, so an orphan is cleared
    by the next good turn on any connection rather than sitting in the store forever. A LIVE
    profile's fault is untouched by it — only a scope that no longer resolves to a profile is
    swept, so ``badr``'s own row still needs ``badr``'s own recovery exactly as it does today.
    """
    key = str(connection_id or "").strip()
    if not key or _known_empty():
        return
    try:
        faults = _read_health_store()
        live = _live_faults(faults)
        popped = live.pop(_store_key(key, scope or owning_scope(key)), None) is not None
        if popped or len(live) != len(faults):
            _write_health_store(live)
        if not live:
            _mark_empty()
    except Exception:
        pass


# Short-circuit for the healthy path: remembers the store's identity (inode/size/mtime) at the
# moment it was last seen empty, so a per-turn ``record_healthy`` costs one ``stat`` instead of a
# read-and-parse. Deliberately NOT a cache of the faults themselves — a stale *fault* would mislead
# the UI. Validating against the file's own stat (rather than trusting a flag forever) is what makes
# it safe now that 16 profiles share one store: another process's write changes the stat, so this
# process re-reads instead of believing an emptiness that another writer has already undone.
_NO_FAULTS_LOCK = threading.Lock()
_NO_FAULTS: Dict[str, Any] = {}


def _store_stamp():
    """``(st_ino, st_size, st_mtime_ns)`` of the store, or None when it does not exist."""
    try:
        st = health_store_path().stat()
    except (OSError, ValueError):
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns)


def _known_empty() -> bool:
    stamp = _store_stamp()
    if stamp is None:
        return True  # No store on disk at all: nothing can be recorded against it.
    with _NO_FAULTS_LOCK:
        return _NO_FAULTS.get("stamp") == stamp


def _mark_empty() -> None:
    stamp = _store_stamp()
    with _NO_FAULTS_LOCK:
        if stamp is None:
            _NO_FAULTS.pop("stamp", None)
        else:
            _NO_FAULTS["stamp"] = stamp


def _invalidate_no_faults() -> None:
    with _NO_FAULTS_LOCK:
        _NO_FAULTS.pop("stamp", None)


def note_connection_healthy(provider: str, base_url: str = "") -> None:
    """Convenience for the turn pipeline: clear whichever row *provider* resolves to right now."""
    try:
        record_healthy(connection_id_for(provider, base_url))
    except Exception:
        pass


def observed_fault(connection_id: str, scope: str = "") -> Optional[Dict[str, Any]]:
    """The runtime's recorded fault for a Connections row, or None when it is believed healthy.

    A ``default``-scope read also answers for a fault recorded by any profile against a global
    credential, because that fault was recorded under ``default`` — that is the point of
    :func:`owning_scope`. Pass *scope* to ask about one profile's own row.
    """
    key = str(connection_id or "").strip()
    if not key:
        return None
    try:
        fault = _read_health_store().get(_store_key(key, scope or GLOBAL_SCOPE))
    except Exception:
        return None
    return fault if isinstance(fault, dict) and fault.get("reason_code") else None


def needs_owner_summary(block: Optional[Dict[str, Any]]) -> str:
    """One-line, secret-free NEEDS_OWNER reason for the autonomy/task record."""
    if not isinstance(block, dict):
        return "A connection this task depends on is disconnected and needs the Owner to reconnect it."
    label = str(block.get("provider_label") or block.get("provider") or "A connection")
    if block.get("owner_action") == ACTION_CONFIGURE:
        return f"{label} is not configured. The Owner must add it in Connections before this task can continue."
    return f"{label} is disconnected. The Owner must reconnect it in Connections before this task can continue."
