"""Google Workspace projection of the native, privately completed auth operation."""
import asyncio
import json
import time
from fastapi import HTTPException


async def google_workspace_action(data, request):
    from hermes_cli.google_workspace_onboarding import run_operation
    from hermes_cli.web_routers.connections import result, auth_projection
    session = getattr(request.state, "session", None)
    if session is not None and session.user_id:
        owner = json.dumps([session.provider, session.user_id], separators=(",", ":"))
    elif not getattr(request.app.state, "auth_required", True):
        owner = "local-dashboard"
    else:
        raise HTTPException(401, "authentication_required")
    actions = {"save_credential":"save_client", "connect":"start", "auth_poll":"poll",
               "auth_submit":"submit", "auth_cancel":"cancel", "test":"test",
               "disconnect":"disconnect"}
    action = actions.get(data["action"])
    if action is None or (action == "save_client" and data.get("field_id") != "google_client_secret_json"):
        raise ValueError("unsupported_action")
    from hermes_cli.asera_google_hosted import begin as hosted_begin, hosted_ready
    if action == "start" and hosted_ready() and data.get("loopback_port") is None:
        raw = await asyncio.to_thread(
            hosted_begin, plugin="gmail", account=data.get("name"), owner_id=owner)
        projected = auth_projection({**raw, "status": "pending"}, identifier="google-workspace",
                                    scope=data["scope"])
        projected["auth"]["flow"] = "browser"
        projected["auth"]["requested_scopes"] = list(raw.get("requested_scopes") or [])
        return projected
    raw = await asyncio.to_thread(run_operation, data["scope"], owner, action,
                                  value=data.get("value"), session_id=data.get("session_id"),
                                  loopback_port=data.get("loopback_port"),
                                  account=data.get("name"))
    if raw.get("ok") is not True:
        code = raw.get("error", "connection_action_failed")
        if action == "test" and code != "google_operation_busy":
            # Replace an earlier native-client check result after a failed check.
            # Lock contention did not perform a check, so preserve its last evidence.
            return result(False, detail_code="check_failed", state="error",
                          last_checked_at=time.time(), check_detail_code=code)
        return result(False, detail_code=code)
    if action == "test":
        return result(detail_code="check_passed", state="connected", last_checked_at=time.time(),
                      account=raw.get("account"),
                      refresh_verified=True, gmail_verified=True)
    if action in {"save_client", "cancel", "disconnect"}:
        return result(detail_code="disconnected" if action == "disconnect" else
                      ("saved" if action == "save_client" else "cancelled"))
    # A poll acquires the operation lock. An exchanging marker surviving that
    # lock means the previous worker stopped with an uncertain token exchange.
    status = {"completed":"approved", "exchanging":"error", "failed":"error"}.get(raw.get("status"), raw.get("status", "pending"))
    projected = auth_projection({**raw, "status":status}, identifier="google-workspace", scope=data["scope"])
    projected["auth"]["flow"] = "manual_code"
    projected["auth"]["requested_scopes"] = list(raw.get("requested_scopes") or [])
    if raw.get("status") == "exchanging":
        projected["detail_code"] = "google_exchange_incomplete"
    return projected
