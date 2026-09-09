"""Ask the PROVIDER whether a Connection's credential still works.

``reconnect`` exists to answer one question — "is this connection fixed?" — and there is exactly one
authority on that answer, and it is not this host. Local metadata cannot see a revoked grant: the
token file keeps a perfectly valid shape, a future ``expiresAt`` and a refresh token long after the
provider has torn the grant down. That is precisely what happened in the 44-hour incident, where the
stored access token stayed locally valid until 06:20:07 while every call had been failing since
05:15:22.

So verification here means one read-only round-trip with the credential the runtime would actually
use, and a deliberately narrow reading of the result:

* ``VERIFIED``   — the provider answered 2xx (or 429: authenticated, merely throttled). Only this
                   clears a recorded fault.
* ``REJECTED``   — the provider answered 401/403. The credential is still bad; say so.
* ``UNVERIFIED`` — anything else: no network, a 5xx, an unexpected status, or no verifier for this
                   row. NOT success. The fault stands and the Owner is told it is unproven.

What this never does, by construction:

* **No consent.** OAuth consent is the Owner's alone and happens with the provider in a browser.
* **No refresh, no token exchange.** Anthropic refresh tokens are single-use; a POST that succeeds
  but whose write fails leaves a spent pair on disk that replays forever as ``invalid_grant``
  (:class:`agent.anthropic_credentials.CredentialPersistError`). A verify button must not be able to
  brick a working grant.
* **No credential ever leaves a header.** No token, prefix, length or fingerprint is returned,
  logged or rendered — only the structural verdict and the provider's HTTP status as an integer.
"""

from __future__ import annotations

from typing import Optional, Tuple

VERIFIED = "verified"
REJECTED = "rejected"
UNVERIFIED = "unverified"

#: Verdict + the provider's HTTP status (None when no call was made or it never answered).
Verdict = Tuple[str, Optional[int]]

_ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
_TIMEOUT = 10.0


def _from_status(status: Optional[int]) -> Verdict:
    """Map an HTTP status to a verdict. Unknown statuses are UNVERIFIED, never VERIFIED."""
    if status is None:
        return UNVERIFIED, None
    if status in (401, 403):
        return REJECTED, status
    # 429 = the credential authenticated and was throttled; that is proof it is live.
    if 200 <= status < 300 or status == 429:
        return VERIFIED, status
    return UNVERIFIED, status


def _resolve_token_without_refresh() -> Tuple[str, str]:
    """``(token, "" | "expired" | "absent")`` for the Anthropic credential the runtime would use.

    Mirrors :func:`agent.anthropic_credentials.resolve_anthropic_token`'s precedence — env OAuth
    token (deferring to the refreshable singleton file, as ``_prefer_refreshable_claude_code_token``
    does), then an explicit API key, then the Claude Code file, then the read-only credential pool —
    with ONE deliberate difference: it never refreshes.

    ``resolve_anthropic_token()`` refreshes an expired Claude Code token as a side effect, and that
    POST spends a single-use refresh token. If the write of the rotated pair then fails, the grant on
    disk replays forever as ``invalid_grant``
    (:class:`agent.anthropic_credentials.CredentialPersistError`). A verify button must not be able
    to brick a working grant, so an expired token is reported as ``expired`` — an honest "cannot
    check this without spending your refresh token" — rather than silently rotated.
    """
    from agent.anthropic_credentials import (
        _first_env, _is_oauth_token, _resolve_anthropic_pool_token, claude_code_credentials_path,
        is_claude_code_token_valid, is_rotation_consumed_uncommitted, read_claude_code_credentials,
    )
    creds = read_claude_code_credentials() or {}
    access = str(creds.get("accessToken") or "").strip()
    spent = bool(access) and is_rotation_consumed_uncommitted(
        access, source_path=claude_code_credentials_path())
    file_token = access if (access and not spent and is_claude_code_token_valid(creds)) else ""

    env_oauth = _first_env("ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
    if env_oauth:
        if _is_oauth_token(env_oauth) and creds.get("refreshToken") and file_token and file_token != env_oauth:
            return file_token, ""
        return env_oauth, ""
    if api_key := _first_env("ANTHROPIC_API_KEY"):
        return api_key, ""
    if file_token:
        return file_token, ""
    if access:
        # Present but unusable as-is: expired, or a spent-but-uncommitted rotation. Not proof of a
        # dead grant — the runtime may still recover it on its own — so this is never REJECTED.
        return "", "expired"
    if pool_token := (_resolve_anthropic_pool_token() or "").strip():
        return pool_token, ""
    return "", "absent"


def verify_anthropic_grant() -> Verdict:
    """Read-only check of the Anthropic credential the runtime would actually resolve.

    Asks about the SAME credential the agent loop uses, including the borrowed Claude Code grant at
    ``~/.claude/.credentials.json`` that the Connections projection cannot otherwise see into.
    Mirrors the request shape of ``hermes_cli.doctor_connectivity._probe_anthropic``, including its
    retry for OAuth subscriptions that reject the 1M-context beta with a 400.
    """
    try:
        import httpx
        from agent.anthropic_adapter import _COMMON_BETAS, _CONTEXT_1M_BETA, _OAUTH_ONLY_BETAS
        from agent.anthropic_credentials import _is_oauth_token
        token, why = _resolve_token_without_refresh()
    except Exception:
        return UNVERIFIED, None
    if not token:
        # Absent is a definitive local answer ("there is nothing here to connect with"); expired is
        # simply unknown. Neither can ever clear a fault, so the distinction only shapes the message.
        return (REJECTED if why == "absent" else UNVERIFIED), None
    try:
        is_oauth = _is_oauth_token(token)
        headers = {"anthropic-version": "2023-06-01"}
        if is_oauth:
            headers["Authorization"] = f"Bearer {token}"
            headers["anthropic-beta"] = ",".join(_COMMON_BETAS + _OAUTH_ONLY_BETAS)
        else:
            headers["x-api-key"] = token
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(_ANTHROPIC_MODELS_URL, headers=headers)
            if is_oauth and resp.status_code == 400 and "long context beta" in resp.text.lower():
                headers["anthropic-beta"] = ",".join(
                    [b for b in _COMMON_BETAS if b != _CONTEXT_1M_BETA] + list(_OAUTH_ONLY_BETAS))
                resp = client.get(_ANTHROPIC_MODELS_URL, headers=headers)
        status = int(resp.status_code)
    except Exception:
        # Type only, never the exception text: an httpx error can echo the request it sent.
        return UNVERIFIED, None
    return _from_status(status)


#: Connections row id -> its provider verifier. A row absent here has no verifier and can only ever
#: be reported UNVERIFIED, which is the honest answer rather than a fabricated one.
_VERIFIERS = {
    "claude-code": verify_anthropic_grant,
    "anthropic": verify_anthropic_grant,
}


def has_native_verifier(identifier: str) -> bool:
    """True when this row can be verified here (rather than through a declared ``.env`` probe)."""
    return str(identifier or "").strip() in _VERIFIERS


def verify_account(identifier: str) -> Verdict:
    """Provider verdict for a Connections account row, or ``UNVERIFIED`` when nothing can verify it."""
    verifier = _VERIFIERS.get(str(identifier or "").strip())
    if verifier is None:
        return UNVERIFIED, None
    try:
        return verifier()
    except Exception:
        return UNVERIFIED, None
