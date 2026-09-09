"""Read the existing multi-account Google store. No second credential vault.

The VPS already keeps one admin OAuth client and one token directory per
Google account under ``~/.secrets/google/``. This module only folds that
store into plugin accounts and revokes one alias at a time.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

ALIAS_RE = re.compile(r"^google_account_[1-9][0-9]{0,3}$")
WORKSPACE_SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
)
_SCOPE_IMPLIES = {
    "https://www.googleapis.com/auth/calendar.readonly": (
        "https://www.googleapis.com/auth/calendar",
    ),
    "https://www.googleapis.com/auth/drive.readonly": (
        "https://www.googleapis.com/auth/drive",
    ),
    "https://www.googleapis.com/auth/gmail.readonly": (
        "https://mail.google.com/",
        "https://www.googleapis.com/auth/gmail.modify",
    ),
    "https://www.googleapis.com/auth/gmail.modify": ("https://mail.google.com/",),
    "https://www.googleapis.com/auth/gmail.settings.basic": ("https://mail.google.com/",),
}
_GMAIL_SCOPES = frozenset({
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.settings.basic",
})
_PROBE_TTL = 45.0
_probe_cache: dict[tuple, tuple[float, str]] = {}


def default_secrets_root():
    root = Path.home() / ".secrets" / "google"
    if (root / "ACL.json").is_file() and (root / "accounts").is_dir():
        return root
    return None


def validate_alias(alias):
    if not isinstance(alias, str) or not ALIAS_RE.fullmatch(alias):
        raise ValueError("invalid_google_request")
    return alias


def extra_account_scopes(gmail_scopes):
    return list(dict.fromkeys([*gmail_scopes, *WORKSPACE_SCOPES]))


def scopes_cover(granted, required, *, service=None):
    have = {str(s) for s in (granted or [])}
    if service == "gmail":
        return bool(have & _GMAIL_SCOPES)
    for need in required or ():
        if need in have:
            continue
        if have.intersection(_SCOPE_IMPLIES.get(need, ())):
            continue
        return False
    return True


def badge_for(email):
    if not isinstance(email, str) or "@" not in email:
        return email or ""
    domain = email.rsplit("@", 1)[-1].casefold()
    if domain in {"bennahub.com"}:
        return "bennahub"
    if domain.endswith("mraia.com.sa") or domain == "mraia.com":
        return "mraia"
    if domain in {"gmail.com", "googlemail.com"}:
        return "default"
    return email.split("@", 1)[0]


def _read_json(path, *, limit=65536):
    path = Path(path)
    if not path.is_file() or path.is_symlink() or path.stat().st_size > limit:
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _token_scopes(payload):
    scopes = (payload or {}).get("scopes") or (payload or {}).get("scope") or []
    if isinstance(scopes, str):
        scopes = scopes.split()
    return [str(s) for s in scopes] if isinstance(scopes, (list, tuple)) else []


def _probe_refresh(path):
    """Classify a stored grant. Transient Google failures stay usable."""
    path = Path(path)
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return "missing"
    cached = _probe_cache.get(key)
    now = time.monotonic()
    if cached and now - cached[0] < _PROBE_TTL:
        return cached[1]
    payload = _read_json(path)
    if not payload or not payload.get("refresh_token"):
        verdict = "reauth"
    else:
        try:
            from google.auth.exceptions import RefreshError
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            creds = Credentials.from_authorized_user_info(payload)
            if creds.valid and not creds.expired:
                verdict = "ok"
            else:
                creds.refresh(Request())
                verdict = "ok"
        except RefreshError as exc:
            text = str(exc).casefold()
            verdict = "reauth" if "invalid_grant" in text or "revoked" in text else "transient"
        except Exception:
            verdict = "transient"
    _probe_cache[key] = (now, verdict)
    return verdict


def _acl_accounts(root):
    acl = _read_json(root / "ACL.json")
    accounts = (acl or {}).get("accounts")
    if isinstance(accounts, dict):
        return accounts
    return {}


def _identity_email(account_dir, meta):
    identity = _read_json(account_dir / "identity.json") or {}
    for candidate in (identity.get("email"), (meta or {}).get("email")):
        if isinstance(candidate, str) and "@" in candidate and len(candidate) < 256:
            return candidate
    return None


def _home_token(home):
    home = Path(home)
    fallback = home / "google_token.json"
    try:
        from hermes_cli.google_workspace_onboarding import credential_module
        from hermes_constants import get_default_hermes_root
        store = credential_module()
        path = store.token_path(home, get_default_hermes_root())
        return path, _read_json(path)
    except Exception:
        return fallback, _read_json(fallback)


def _same_refresh(left, right):
    a, b = (left or {}).get("refresh_token"), (right or {}).get("refresh_token")
    return isinstance(a, str) and isinstance(b, str) and a and a == b


def is_primary_alias(alias, home=None, secrets_root=None):
    if alias == "google_account_1":
        return True
    if home is None or secrets_root is None:
        return False
    _, home_payload = _home_token(home)
    other = _read_json(Path(secrets_root) / "accounts" / alias / "token.json")
    return _same_refresh(home_payload, other)


def load_google_accounts(home, *, secrets_root=None, probe=True):
    """Owner-facing Google accounts. Never returns token material."""
    home = Path(home)
    secrets_root = Path(secrets_root) if secrets_root is not None else default_secrets_root()
    home_path, home_payload = _home_token(home)
    rows = []
    seen_refresh = set()

    def add(alias, email, scopes, verdict, *, primary=False):
        refresh = None
        token_path = home_path if primary else (secrets_root / "accounts" / alias / "token.json" if secrets_root else home_path)
        payload = home_payload if primary else _read_json(token_path)
        refresh = (payload or {}).get("refresh_token")
        if isinstance(refresh, str) and refresh:
            if refresh in seen_refresh:
                return
            seen_refresh.add(refresh)
        if verdict == "ok" or (verdict == "transient" and payload and payload.get("refresh_token")):
            status = "CONNECTED"
        elif verdict in ("reauth", "missing") and payload:
            status = "RECONNECT_REQUIRED"
        elif payload:
            status = "RECONNECT_REQUIRED"
        else:
            return
        rows.append({
            "id": alias,
            "display": email or alias,
            "badge": badge_for(email or ""),
            "status": status,
            "scopes": list(scopes),
            "primary": primary,
            "reconnect": status == "RECONNECT_REQUIRED",
        })

    if secrets_root is not None:
        meta_map = _acl_accounts(secrets_root)
        aliases = [alias for alias in meta_map if ALIAS_RE.fullmatch(alias)]
        if not aliases:
            aliases = sorted(
                path.name for path in (secrets_root / "accounts").iterdir()
                if path.is_dir() and ALIAS_RE.fullmatch(path.name)
            ) if (secrets_root / "accounts").is_dir() else []
        for alias in aliases:
            account_dir = secrets_root / "accounts" / alias
            payload = _read_json(account_dir / "token.json")
            email = _identity_email(account_dir, meta_map.get(alias) if isinstance(meta_map.get(alias), dict) else None)
            if is_primary_alias(alias, home, secrets_root) and home_payload:
                verdict = _probe_refresh(home_path) if probe else "ok"
                add(alias, email or _token_account(home_payload), _token_scopes(home_payload),
                    verdict, primary=True)
                continue
            if not payload:
                if email:
                    rows.append({
                        "id": alias, "display": email, "badge": badge_for(email),
                        "status": "NEEDS_SIGN_IN", "scopes": [], "primary": False,
                        "reconnect": False,
                    })
                continue
            verdict = _probe_refresh(account_dir / "token.json") if probe else "ok"
            add(alias, email, _token_scopes(payload), verdict)

    if home_payload and not any(row.get("primary") for row in rows):
        email = _token_account(home_payload)
        verdict = _probe_refresh(home_path) if probe else "ok"
        add("google_account_1", email, _token_scopes(home_payload), verdict, primary=True)
    return rows


def _token_account(payload):
    for candidate in (
        payload.get("account"),
        (payload.get("_hermes_gmail_verification") or {}).get("account"),
    ):
        if isinstance(candidate, str) and "@" in candidate and len(candidate) < 256:
            return candidate
    return None


def next_alias(secrets_root):
    root = Path(secrets_root) / "accounts"
    used = {path.name for path in root.iterdir() if path.is_dir()} if root.is_dir() else set()
    used.update(alias for alias in _acl_accounts(Path(secrets_root)) if ALIAS_RE.fullmatch(alias))
    index = 1
    while f"google_account_{index}" in used:
        index += 1
    if index > 99:
        raise ValueError("invalid_google_request")
    return f"google_account_{index}"


def retarget_native(native, alias, *, home, secrets_root=None, gmail_scopes=()):
    """Point one native operation at a named account without touching the others."""
    alias = validate_alias(alias)
    secrets_root = Path(secrets_root) if secrets_root is not None else default_secrets_root()
    if is_primary_alias(alias, home, secrets_root):
        return alias
    if secrets_root is None:
        raise ValueError("invalid_google_request")
    account_dir = secrets_root / "accounts" / alias
    account_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(secrets_root / "accounts", 0o700)
    os.chmod(account_dir, 0o700)
    client = secrets_root / "client_secret.json"
    if not client.is_file():
        client = Path(home) / "google_client_secret.json"
    native.TOKEN_PATH = account_dir / "token.json"
    native.CLIENT_SECRET_PATH = client
    native.PENDING_AUTH_PATH = account_dir / "google_oauth_pending.json"
    native.SCOPES = extra_account_scopes(gmail_scopes)
    return alias


def revoke_named_account(alias, *, home, secrets_root=None):
    """Revoke one alias. Shared primary grant is removed from both copies."""
    alias = validate_alias(alias)
    secrets_root = Path(secrets_root) if secrets_root is not None else default_secrets_root()
    home = Path(home)
    home_path, home_payload = _home_token(home)
    if is_primary_alias(alias, home, secrets_root):
        if secrets_root is not None:
            twin = secrets_root / "accounts" / alias / "token.json"
            if twin.is_file() and not twin.is_symlink():
                twin.unlink()
        return "primary"
    if secrets_root is None:
        raise ValueError("invalid_google_request")
    token = secrets_root / "accounts" / alias / "token.json"
    payload = _read_json(token)
    _revoke_file(token)
    if _same_refresh(home_payload, payload):
        _revoke_file(home_path)
    return "named"


def _revoke_file(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        path.unlink(missing_ok=True)
        return
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        creds = Credentials.from_authorized_user_file(str(path))
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        import urllib.request
        urllib.request.urlopen(
            urllib.request.Request(
                "https://oauth2.googleapis.com/revoke?token=" + creds.token,
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ),
            timeout=15,
        )
    except Exception:
        pass
    path.unlink(missing_ok=True)
