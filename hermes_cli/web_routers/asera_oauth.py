"""Public hosted Google OAuth pages. Tokens never appear in redirects."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from hermes_cli.asera_google_hosted import (
    AUTH_HOST,
    REDIRECT_URI,
    authorization_url,
    complete,
    consume_completion,
    finish_url,
)
from hermes_cli.web_deps import late

router = APIRouter()
_require_token = late("_require_token")


@router.get("/oauth/health")
async def oauth_health(request: Request):
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if host not in {AUTH_HOST, "127.0.0.1", "localhost"}:
        raise HTTPException(404)
    return {"ok": True, "service": "asera-oauth"}


@router.get("/oauth/google/start")
async def oauth_google_start(request: Request, t: str = ""):
    if (request.headers.get("host") or "").split(":")[0].lower() != AUTH_HOST:
        raise HTTPException(404)
    if not t or len(t) > 128:
        return RedirectResponse(finish_url(error=True), status_code=302)
    try:
        return RedirectResponse(authorization_url(t), status_code=302)
    except ValueError:
        return RedirectResponse(finish_url(error=True), status_code=302)


@router.get("/oauth/google/callback")
async def oauth_google_callback(request: Request):
    if (request.headers.get("host") or "").split(":")[0].lower() != AUTH_HOST:
        raise HTTPException(404)
    # Reconstruct the public HTTPS URL. Behind Traefik, request.url is the
    # loopback origin and must never be used for state/host validation.
    query = request.url.query
    callback = f"{REDIRECT_URI}?{query}" if query else REDIRECT_URI
    try:
        return RedirectResponse(complete(callback), status_code=302)
    except ValueError:
        return RedirectResponse(finish_url(error=True), status_code=302)


@router.post("/api/plugins/oauth/complete")
async def oauth_complete(request: Request):
    _require_token(request)
    try:
        body = await request.json()
        token = body.get("completion") if isinstance(body, dict) else None
        return consume_completion(token)
    except ValueError:
        raise HTTPException(400, detail="invalid_plugin_request") from None
    except Exception:
        return JSONResponse({"ok": False, "detail_code": "connection_action_failed"})
