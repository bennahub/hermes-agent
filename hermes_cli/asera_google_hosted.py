"""Hosted Google OAuth for Asera. Existing token files stay the store.

Desktop ``localhost:1`` remains in the native helper for Advanced/diagnostics.
The owner Plugins path uses ``https://auth.asera.dev`` only.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

from hermes_cli.asera_google_accounts import (
    ALIAS_RE,
    extra_account_scopes,
    is_primary_alias,
    next_alias,
    validate_alias,
)
from hermes_cli.google_gmail_verification import SCOPES as GMAIL_SCOPES
from hermes_cli.google_workspace_onboarding import validate_client

AUTH_HOST = "auth.asera.dev"
AUTH_ORIGIN = "https://auth.asera.dev"
REDIRECT_URI = "https://auth.asera.dev/oauth/google/callback"
COMPLETE_ORIGIN = "https://asera.dev"
COMPLETE_PATH = "/oauth/complete"
TX_TTL = 600
COMPLETION_TTL = 180
STORE_NAME = "google_oauth_hosted.json"
WEB_CLIENT_NAME = "web_client_secret.json"


def trusted_hosts():
    return frozenset({AUTH_HOST})


def web_client_path():
    root = Path.home() / ".secrets" / "google"
    path = root / WEB_CLIENT_NAME
    return path if path.is_file() and not path.is_symlink() else None


def hosted_ready():
    return web_client_path() is not None


def _store_path():
    from hermes_constants import get_default_hermes_root
    return Path(get_default_hermes_root()) / STORE_NAME


def _read_store():
    path = _store_path()
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 65536:
        return {"transactions": {}, "completions": {}}
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"transactions": {}, "completions": {}}
    if not isinstance(payload, dict):
        return {"transactions": {}, "completions": {}}
    payload.setdefault("transactions", {})
    payload.setdefault("completions", {})
    return payload


def _write_store(payload):
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = __import__("tempfile").mkstemp(prefix=".google-hosted-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _purge(payload, now):
    for bucket in ("transactions", "completions"):
        items = payload.get(bucket) or {}
        payload[bucket] = {k: v for k, v in items.items()
                           if isinstance(v, dict) and float(v.get("expires_at") or 0) > now
                           and not v.get("used")}
    return payload


def _client():
    path = web_client_path()
    if path is None:
        raise ValueError("google_hosted_client_required")
    return validate_client(path.read_text())


def _pkce():
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    import base64
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def begin(*, plugin, account, owner_id, scopes=None):
    """Authenticated owner click. Does not talk to Google and does not write tokens."""
    if not hosted_ready():
        raise ValueError("google_hosted_client_required")
    if account:
        account = validate_alias(account)
    elif plugin in {"google-calendar", "google-drive", "gmail"}:
        account = None
    now = time.time()
    verifier, challenge = _pkce()
    tx = secrets.token_urlsafe(24)
    state = secrets.token_urlsafe(24)
    requested = list(scopes or GMAIL_SCOPES)
    if plugin in {"google-calendar", "google-drive"} or (account and account != "google_account_1"):
        requested = extra_account_scopes(GMAIL_SCOPES)
    record = {
        "id": tx,
        "state": state,
        "verifier": verifier,
        "challenge": challenge,
        "plugin": plugin,
        "account": account,
        "owner": hashlib.sha256(str(owner_id).encode()).hexdigest(),
        "scopes": requested,
        "created_at": now,
        "expires_at": now + TX_TTL,
        "status": "pending",
    }
    payload = _purge(_read_store(), now)
    payload["transactions"][tx] = record
    _write_store(payload)
    start = f"{AUTH_ORIGIN}/oauth/google/start?t={tx}"
    return {
        "ok": True,
        "session_id": tx,
        "status": "pending",
        "verification_uri": start,
        "flow": "browser",
        "expires_in": TX_TTL,
        "requested_scopes": requested,
    }


def authorization_url(tx_id):
    now = time.time()
    payload = _purge(_read_store(), now)
    record = payload["transactions"].get(tx_id)
    if not isinstance(record, dict) or record.get("status") != "pending":
        raise ValueError("google_operation_not_found")
    if float(record.get("expires_at") or 0) <= now:
        raise ValueError("google_operation_expired")
    raw = _client()
    client = raw.get("web") or raw["installed"]
    query = urlencode({
        "client_id": client["client_id"],
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(record["scopes"]),
        "state": record["state"],
        "code_challenge": record["challenge"],
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    })
    return "https://accounts.google.com/o/oauth2/v2/auth?" + query


def complete(callback_url):
    """Exchange the hosted callback. Writes only the targeted account store."""
    if not isinstance(callback_url, str) or len(callback_url) > 16384:
        raise ValueError("invalid_google_callback")
    parts = urlsplit(callback_url)
    if parts.scheme != "https" or parts.hostname != AUTH_HOST or parts.username or parts.password:
        raise ValueError("invalid_google_callback")
    if parts.path.rstrip("/") != "/oauth/google/callback":
        raise ValueError("invalid_google_callback")
    from urllib.parse import parse_qs
    query = parse_qs(parts.query, keep_blank_values=True)
    state = (query.get("state") or [""])[0]
    code = (query.get("code") or [""])[0]
    if query.get("error"):
        raise ValueError("google_operation_cancelled")
    if not state or not code:
        raise ValueError("invalid_google_callback_state")
    now = time.time()
    payload = _purge(_read_store(), now)
    record = next((row for row in payload["transactions"].values()
                   if isinstance(row, dict) and secrets.compare_digest(str(row.get("state") or ""), state)), None)
    if record is None:
        raise ValueError("google_operation_not_found")
    if float(record.get("expires_at") or 0) <= now:
        raise ValueError("google_operation_expired")
    if record.get("status") != "pending":
        raise ValueError("google_operation_consumed")
    record["status"] = "exchanging"
    payload["transactions"][record["id"]] = record
    _write_store(payload)
    token = _exchange(code, record)
    account = record.get("account") or "google_account_1"
    _store_token(account, token)
    completion = secrets.token_urlsafe(24)
    payload = _purge(_read_store(), time.time())
    payload["transactions"][record["id"]] = {**record, "status": "completed", "verifier": None, "challenge": None}
    payload["completions"][completion] = {
        "id": completion,
        "plugin": record.get("plugin"),
        "account": account,
        "created_at": time.time(),
        "expires_at": time.time() + COMPLETION_TTL,
        "used": False,
    }
    _write_store(payload)
    return f"{COMPLETE_ORIGIN}{COMPLETE_PATH}?c={completion}"


def consume_completion(token):
    if not isinstance(token, str) or len(token) > 128:
        raise ValueError("invalid_google_request")
    now = time.time()
    payload = _purge(_read_store(), now)
    row = payload["completions"].get(token)
    if not isinstance(row, dict) or row.get("used") or float(row.get("expires_at") or 0) <= now:
        raise ValueError("google_operation_not_found")
    row["used"] = True
    payload["completions"][token] = row
    _write_store(payload)
    return {"ok": True, "plugin": row.get("plugin"), "account": row.get("account")}


def finish_url(*, error=None, completion=None):
    query = urlencode({"error": "could_not_connect"} if error else {"c": completion})
    return f"{COMPLETE_ORIGIN}{COMPLETE_PATH}?{query}"


def _exchange(code, record):
    from google_auth_oauthlib.flow import Flow
    raw = _client()
    client = raw.get("web") or raw["installed"]
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
    flow = Flow.from_client_config(
        raw,
        scopes=record["scopes"],
        redirect_uri=REDIRECT_URI,
        state=record["state"],
        code_verifier=record["verifier"],
    )
    flow.fetch_token(code=code)
    creds = flow.credentials
    token = json.loads(creds.to_json())
    token["type"] = "authorized_user"
    if getattr(creds, "granted_scopes", None):
        token["scopes"] = list(creds.granted_scopes)
    return token


def _store_token(account, token):
    """Write one alias only. Never replace a sibling grant."""
    from hermes_constants import get_default_hermes_root
    home = Path(get_default_hermes_root())
    secrets_root = Path.home() / ".secrets" / "google"
    alias = validate_alias(account) if ALIAS_RE.fullmatch(account or "") else "google_account_1"
    body = json.dumps(token)
    if is_primary_alias(alias, home, secrets_root if secrets_root.is_dir() else None):
        target = home / "google_token.json"
        _atomic_write(target, body)
        twin = secrets_root / "accounts" / "google_account_1" / "token.json"
        if secrets_root.is_dir():
            twin.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(twin, body)
        return
    target = secrets_root / "accounts" / alias / "token.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(target, body)


def _atomic_write(path, text):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("invalid_google_store")
    fd, name = __import__("tempfile").mkstemp(prefix=".google-token-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def allocate_account():
    root = Path.home() / ".secrets" / "google"
    if not (root / "accounts").is_dir():
        return "google_account_1"
    return next_alias(root)
