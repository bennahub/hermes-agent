"""Explicit read-only Gmail verification in the canonical scoped token store.

Called only by the owner-authenticated Google worker under its existing lock.
Inventory reads a bounded cached receipt; it never refreshes or probes Google.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

SCOPES = ["https://mail.google.com/", "https://www.googleapis.com/auth/gmail.settings.basic"]
META = "_hermes_gmail_verification"
MAX_AGE = 86400


def required_granted(scopes):
    if isinstance(scopes, str): scopes=scopes.split()
    return isinstance(scopes, (list, tuple, set)) and set(SCOPES).issubset(scopes)


def fingerprint(payload):
    return hashlib.sha256(json.dumps({k:v for k,v in payload.items() if k != META},
                                    sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def receipt(path, now=None):
    now = time.time() if now is None else now
    path = Path(path)
    try:
        if path.is_symlink() or path.stat().st_size > 65536:
            return None
        payload = json.loads(path.read_text())
        if not isinstance(payload, dict) or not required_granted(payload.get("scopes")): return None
        proof = payload.get(META, {})
        if not isinstance(proof, dict): return None
        if (proof.get("credential_fingerprint") != fingerprint(payload)
                or not 0 <= now - float(proof["verified_at"]) <= MAX_AGE
                or not isinstance(proof.get("account"), str) or "@" not in proof["account"]):
            return None
        return {"account":proof["account"], "verified_at":proof["verified_at"]}
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _write(path, payload):
    fd, name = tempfile.mkstemp(prefix=".google-token-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(payload, stream); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def verify(path):
    """Force official refresh, then GET identity and labels; never email content or writes."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request, AuthorizedSession
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
        raise ValueError("google_credential_required")
    payload = json.loads(path.read_text())
    payload.pop(META, None)
    _write(path, payload)  # A failed recheck must not retain a stale Connected receipt.
    if not required_granted(payload.get("scopes")):
        raise ValueError("google_verification_failed")
    credentials = Credentials.from_authorized_user_info(payload)
    if not credentials.refresh_token:
        raise ValueError("google_refresh_required")
    transport = Request()
    credentials.refresh(lambda *a, **kw: transport(*a, **{**kw, "timeout":15}))
    if not required_granted(credentials.scopes) or (credentials.granted_scopes is not None
            and not required_granted(credentials.granted_scopes)):
        raise ValueError("google_verification_failed")
    with AuthorizedSession(credentials) as session:
        response = session.get("https://gmail.googleapis.com/gmail/v1/users/me/profile", timeout=15)
        response.raise_for_status()
        profile = response.json()
        account = profile.get("emailAddress")
        if not isinstance(account, str) or "@" not in account:
            raise ValueError("google_verification_failed")
        response = session.get("https://gmail.googleapis.com/gmail/v1/users/me/labels", timeout=15)
        response.raise_for_status()
        labels = response.json().get("labels")
        if not isinstance(labels, list):
            raise ValueError("google_verification_failed")
    saved = json.loads(credentials.to_json())
    if credentials.granted_scopes is not None:
        saved["scopes"] = list(credentials.granted_scopes)
    saved.setdefault("type", "authorized_user")
    saved[META] = {"account":account, "verified_at":time.time(),
                   "credential_fingerprint":fingerprint(saved)}
    _write(path, saved)
    return {"ok":True, "detail_code":"google_verified", "account":account,
            "refresh_verified":True, "gmail_verified":True, "labels_count":len(labels)}
