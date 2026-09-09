"""Owner-authenticated, client-neutral projection of canonical management.

The mutation dispatcher is deliberately closed: clients cannot submit shell
commands, raw MCP configuration, undeclared environment names or store paths.
Native helpers own persistence, locking, OAuth sessions and lifecycle.
"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import urlsplit
from fastapi import APIRouter, HTTPException, Request
from hermes_cli.connections import build_inventory, scope_context
from hermes_cli.connections_google import google_workspace_action
from hermes_cli.asera_plugins import build_plugin_index, resolve_plugin_action
from hermes_cli.web_deps import late

router = APIRouter()
_require_token = late("_require_token")
_build_oauth_catalog = late("_build_oauth_catalog", "hermes_cli.web_routers.oauth")


def result(ok=True, lifecycle="immediate", detail_code="saved", **extra):
    return {"ok":bool(ok),"lifecycle":lifecycle,"detail_code":detail_code,**extra}


async def account_provider_verdict(entry, scope, request, config_env):
    """Ask the PROVIDER whether this account row's credential works. ``(verdict, provider_status)``.

    Native verifier first (it can reach the borrowed Claude Code singleton, which has no ``.env``
    field to probe); otherwise the very same credential probe the ``test`` action uses. A row with
    neither is ``unverified`` — never ``verified``.

    Local metadata is deliberately NOT consulted. A revoked grant reads as a perfectly valid token on
    disk; that is the whole failure mode. See ``hermes_cli.connections_verify``.
    """
    from hermes_cli.connections_verify import (
        REJECTED, UNVERIFIED, VERIFIED, has_native_verifier, verify_account,
    )
    if has_native_verifier(entry["id"]):
        return await asyncio.to_thread(verify_account, entry["id"])

    def read_probe_credential():
        from hermes_cli.config import load_env
        with scope_context(scope):
            env = load_env()
            for field in entry["fields"]:
                key = field["id"]
                if key in config_env._CREDENTIAL_PROBES and env.get(key):
                    return key, env[key]
        return None, None
    key, value = await asyncio.to_thread(read_probe_credential)
    if not key:
        return UNVERIFIED, None
    from hermes_cli.web_models import EnvVarUpdate
    raw = await config_env.validate_provider_credential(EnvVarUpdate(key=key, value=value), request)
    if raw.get("reachable") is not True:
        return UNVERIFIED, None  # The provider never answered; that is not a pass.
    return (VERIFIED if raw.get("ok") is True else REJECTED), None


def auth_projection(raw, *, identifier, scope, mcp=False):
    status = str(raw.get("status") or "pending")
    status = {"authorization_required":"pending","connecting":"pending","success":"approved",
              "cancelled":"denied"}.get(status, status)
    if status not in ("pending","approved","denied","expired","error"):
        status = "pending"
    auth = {"provider_id":identifier,"scope":scope,
            "session_id":str(raw.get("flow_id" if mcp else "session_id") or ""),
            "flow":"browser" if mcp else "device_code","status":status,
            "poll_interval":max(2,min(30,int(raw.get("poll_interval") or 3)))}
    url = raw.get("authorization_url") if mcp else raw.get("verification_url") or raw.get("verification_uri")
    if url:
        parts = urlsplit(str(url))
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise ValueError("invalid_login_url")
        auth["verification_uri"] = str(url)
    if raw.get("user_code"):
        auth["user_code"] = str(raw["user_code"])
    if raw.get("expires_at"):
        auth["expires_at"] = raw["expires_at"]
    elif raw.get("expires_in"):
        auth["expires_at"] = time.time() + int(raw["expires_in"])
    return result(status not in ("error","denied","expired"), detail_code="auth_"+status, auth=auth)


def parse_action(data):
    allowed = {"kind","id","scope","action","field_id","value","values","session_id","name","url","auth_type","loopback_port"}
    if not isinstance(data,dict) or set(data)-allowed:
        raise ValueError("invalid_request")
    for key in ("kind","id","scope","action","field_id","session_id","name","url","auth_type"):
        if key in data and (not isinstance(data[key],str) or len(data[key])>2048 or any(ord(c)<32 for c in data[key])):
            raise ValueError("invalid_request")
    if data.get("kind") not in ("account","integration","credential","mcp","plugin"):
        raise ValueError("invalid_kind")
    if not data.get("id") or not data.get("action"):
        raise ValueError("invalid_request")
    if "loopback_port" in data and (
            (data.get("kind"),data.get("id"),data.get("action")) != ("integration","google-workspace","connect")
            or type(data["loopback_port"]) is not int or not 1024 <= data["loopback_port"] <= 65535):
        raise ValueError("invalid_request")
    google_json = (data.get("kind"), data.get("id"), data.get("action"), data.get("field_id")) == (
        "integration", "google-workspace", "save_credential", "google_client_secret_json")
    def invalid_character(c):
        return (ord(c)<32 and not (google_json and c in "\n\r\t")) or ord(c)>126
    if "value" in data and (not isinstance(data["value"],str) or not data["value"] or len(data["value"])>32768 or any(invalid_character(c) for c in data["value"])):
        raise ValueError("invalid_credential")
    values=data.get("values",{})
    if not isinstance(values,dict) or len(values)>20 or any(not isinstance(k,str) or not isinstance(v,str) or len(v)>32768 or any(ord(c)<32 or ord(c)>126 for c in v) for k,v in values.items()):
        raise ValueError("invalid_credential")
    data=dict(data);data["scope"]=data.get("scope") or "default"
    return data


@router.get("/api/connections")
async def connections_inventory(request: Request):
    _require_token(request)
    try:
        return await asyncio.to_thread(build_inventory, _build_oauth_catalog())
    except Exception:
        # Never reflect exception text: native errors can include config/URLs.
        raise HTTPException(503,detail="connections_unavailable") from None


@router.get("/api/plugins")
async def plugins_index(request: Request):
    """Owner-facing plugin list. Same credentials as Connections; different fold."""
    _require_token(request)
    try:
        return await asyncio.to_thread(build_plugin_index, _build_oauth_catalog())
    except Exception:
        raise HTTPException(503, detail="plugins_unavailable") from None


@router.post("/api/plugins/actions")
async def plugins_action(request: Request):
    """Translate a plugin action onto the existing Connections dispatcher."""
    _require_token(request)
    try:
        raw = await request.body()
        if len(raw) > 128 * 1024:
            raise ValueError("invalid_request")
        import json
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError("invalid_request")
        plugin_id = body.get("plugin")
        action = body.get("action")
        if not isinstance(plugin_id, str) or not isinstance(action, str):
            raise ValueError("invalid_request")
        extra = {k: body[k] for k in ("session_id", "value", "loopback_port", "account") if k in body}
        account = extra.pop("account", None)
        if account is not None and (not isinstance(account, str) or len(account) > 64):
            raise ValueError("invalid_request")
        index = await asyncio.to_thread(build_plugin_index, _build_oauth_catalog())
        mapped = resolve_plugin_action(plugin_id, action, index["plugins"], account=account)
        mapped.update(extra)
        return await dispatch(parse_action(mapped), request)
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(400, detail="invalid_plugin_request") from None
    except Exception:
        return result(False, detail_code="connection_action_failed")


@router.post("/api/connections/actions")
async def connections_action(request: Request):
    _require_token(request)
    try:
        raw=await request.body()
        if len(raw)>128*1024:
            raise ValueError("invalid_request")
        import json
        data=parse_action(json.loads(raw))
    except Exception:
        raise HTTPException(400,detail="invalid_connection_request") from None
    try:
        return await dispatch(data,request)
    except HTTPException as exc:
        if exc.status_code in (401,403):
            raise HTTPException(exc.status_code,detail="authentication_required") from None
        if exc.status_code==404:
            return result(False,detail_code="not_found_or_expired")
        return result(False,detail_code="connection_action_failed")
    except Exception:
        return result(False,detail_code="connection_action_failed")


async def dispatch(data,request):
    from hermes_cli import web_server as web
    from hermes_cli.web_routers import mcp, oauth, config_env
    from hermes_cli import web_server_oauth, web_server_mcp
    kind,identifier,scope,action=(data[k] for k in ("kind","id","scope","action"))
    # Scope existence is validated before native session lookups, without
    # installing any mutable process-global profile selection.
    def validate_scope():
        with scope_context(scope):
            pass
    await asyncio.to_thread(validate_scope)
    if kind == "integration" and identifier == "google-workspace":
        return await google_workspace_action(data, request)
    if action in ("auth_poll","auth_cancel"):
        sid=data.get("session_id")
        if not sid:
            raise ValueError("session_required")
        if kind=="account":
            if action=="auth_poll":
                raw=await oauth.poll_oauth_session(identifier,sid,profile=scope)
                return auth_projection(raw,identifier=identifier,scope=scope)
            # Native cancel binds profile; additionally bind provider here.
            with web_server_oauth._oauth_sessions_lock:
                session=web_server_oauth._oauth_sessions.get(sid)
                if session and session.get("provider")!=identifier:
                    raise ValueError("session_mismatch")
            await oauth.cancel_oauth_session(sid,request,profile=scope)
            return result(detail_code="cancelled")
        if kind=="mcp":
            flow=web_server_mcp._mcp_oauth_flows.get(sid)
            if flow is None:
                return result(False,detail_code="not_found_or_expired")
            if flow.server_name!=identifier or flow.profile!=scope:
                raise ValueError("session_mismatch")
            if action=="auth_cancel":
                await mcp.cancel_mcp_oauth_flow(sid,request)
                return result(detail_code="cancelled")
            raw=await mcp.mcp_oauth_flow_status(sid,request)
            return auth_projection(raw,identifier=identifier,scope=scope,mcp=True)
        raise ValueError("unsupported_auth")
    if kind=="mcp" and identifier=="new" and action=="add":
        from hermes_cli.web_models import MCPServerCreate
        name=data.get("name","")
        if not name or len(name)>80 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in name):
            raise ValueError("invalid_name")
        parts=urlsplit(data.get("url", ""))
        if parts.scheme!="https" or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("invalid_url")
        auth_type=data.get("auth_type") or "oauth"
        if auth_type not in ("oauth","header","none"):
            raise ValueError("invalid_auth")
        await mcp.add_mcp_server(MCPServerCreate(name=name,url=data["url"],auth=auth_type,profile=scope),profile=scope)
        return result(lifecycle="next_session")

    inventory=await asyncio.to_thread(build_inventory,oauth._build_oauth_catalog())
    entry=next((e for e in inventory["entries"] if e["kind"]==kind and e["id"]==identifier and e["scope"]==scope),None)
    if not entry or action not in entry["actions"]:
        raise ValueError("unsupported_action")
    if action=="save_credential":
        declared={f["id"] for f in entry["fields"]}
        key=data.get("field_id")
        if key not in declared or not data.get("value"):
            raise ValueError("undeclared_credential")
        def save():
            from hermes_cli.connections import run_native_mutation
            with web._CONFIG_MUTATION_LOCK:
                run_native_mutation(data)
        await asyncio.to_thread(save)
        return result(lifecycle=entry["lifecycle"])
    if kind=="account":
        if action=="reconnect":
            # Re-verify a connection the runtime recorded as broken, against the PROVIDER. Consent
            # itself belongs to the Owner and happens with the provider, never here; this never
            # refreshes or exchanges a token either (Anthropic refresh tokens are single-use — a
            # verify button must not be able to brick a working grant).
            #
            # It also never answers from local metadata. A revoked-but-locally-valid token and an
            # expired-but-refreshable one BOTH read as "configured" on disk, and both states held for
            # every one of the 44 hours the connection was dead — so a metadata answer would have
            # told the Owner "reconnected" the entire time while nothing had changed. Only a live
            # 2xx clears the fault; 401/403 is an honest rejection; anything else is unverified,
            # which is a failure, not a pass.
            from hermes_cli.connections_verify import REJECTED, VERIFIED
            verdict, provider_status = await account_provider_verdict(entry, scope, request, config_env)
            extra = {"provider_status": provider_status} if isinstance(provider_status, int) else {}
            if verdict == VERIFIED:
                def clear():
                    # Cleared under the row's OWN scope, which is the key the fault was recorded
                    # under. No ``scope_context`` here on purpose: the canonical store is anchored to
                    # the Hermes root and is deliberately immune to the profile override.
                    from agent.connection_health import record_healthy
                    record_healthy(identifier, entry.get("scope") or "default")
                await asyncio.to_thread(clear)
                return result(detail_code="reconnect_verified", state="connected",
                              last_checked_at=time.time(), **extra)
            # Honest failure, both ways. The fault stays recorded, so Connections and the runtime keep
            # agreeing that this connection is broken.
            return result(False,
                          detail_code="reconnect_rejected" if verdict == REJECTED else "reconnect_unverified",
                          state="needs_auth", owner_action="reauthorize",
                          last_checked_at=time.time(), **extra)
        if action=="test":
            def read_probe_credential():
                from hermes_cli.config import load_env
                with scope_context(scope):
                    env=load_env()
                    for field in entry["fields"]:
                        key=field["id"]
                        if key in config_env._CREDENTIAL_PROBES and env.get(key):
                            return key,env[key]
                raise ValueError("credential_unavailable")
            key,value=await asyncio.to_thread(read_probe_credential)
            from hermes_cli.web_models import EnvVarUpdate
            raw=await config_env.validate_provider_credential(EnvVarUpdate(key=key,value=value),request)
            passed=raw.get("ok") is True and raw.get("reachable") is True
            return result(passed,detail_code="check_passed" if passed else "check_failed",
                          state="connected" if passed else "error",last_checked_at=time.time())
        if action=="connect":
            raw=await oauth.start_oauth_login(identifier,request,profile=scope)
            try:
                return auth_projection(raw,identifier=identifier,scope=scope)
            except (ValueError, TypeError):
                if raw.get("session_id"):
                    await oauth.cancel_oauth_session(raw["session_id"],request,profile=scope)
                raise
        if action=="disconnect":
            def disconnect():
                from hermes_cli.connections import run_native_mutation
                with web._CONFIG_MUTATION_LOCK:
                    run_native_mutation(data)
            await asyncio.to_thread(disconnect)
            return result(detail_code="disconnected")
    if kind=="mcp":
        from hermes_cli.web_models import MCPEnabledToggle
        if action in ("enable","disable"):
            await mcp.set_mcp_server_enabled(identifier,MCPEnabledToggle(enabled=action=="enable",profile=scope),profile=scope)
        elif action=="remove":
            await mcp.remove_mcp_server(identifier,profile=scope)
        elif action=="connect":
            raw=await mcp.auth_mcp_server(identifier,request,profile=scope)
            try:
                return auth_projection(raw,identifier=identifier,scope=scope,mcp=True)
            except (ValueError, TypeError):
                if raw.get("flow_id"):
                    await mcp.cancel_mcp_oauth_flow(raw["flow_id"],request)
                raise
        elif action=="test":
            raw=await mcp.test_mcp_server(identifier,profile=scope)
            return result(bool(raw.get("ok")),detail_code="check_passed" if raw.get("ok") else "check_failed",
                          state="connected" if raw.get("ok") else "error",last_checked_at=time.time())
        elif action=="reconnect":
            # The MCP half of the account ``reconnect`` contract, and the same three-way verdict.
            # Local metadata cannot answer this either: ``_oauth_tokens_present`` sees a file, and a
            # revoked grant leaves the file exactly where it was. So ask the SERVER, with the
            # credential the runtime would actually use — the same probe ``test`` runs, no consent
            # flow (that is the Owner's, in a browser, with the provider).
            #
            # Only a live success clears the fault. The probe's ``error`` string is the SERVER's own
            # words and is used ONLY to classify; it is never returned, which is the whole point of
            # this taxonomy.
            raw=await mcp.test_mcp_server(identifier,profile=scope)
            if raw.get("ok"):
                def clear_mcp():
                    from agent.connection_health import record_healthy
                    record_healthy(identifier, entry.get("scope") or "default")
                await asyncio.to_thread(clear_mcp)
                return result(detail_code="reconnect_verified", state="connected", last_checked_at=time.time())
            from tools.mcp_tool_errors import auth_rejection_text
            text=str(raw.get("error") or "")
            rejected="no token found" in text.lower() or auth_rejection_text(text)
            return result(False,
                          detail_code="reconnect_rejected" if rejected else "reconnect_unverified",
                          state="needs_auth", owner_action="reauthorize", last_checked_at=time.time())
        elif action=="install":
            values=dict(data.get("values") or {})
            if data.get("field_id") and data.get("value"):
                values[data["field_id"]]=data["value"]
            declared={f["id"] for f in entry["fields"]}
            if set(values)-declared:
                raise ValueError("undeclared_credential")
            def install():
                from hermes_cli.connections import run_native_mutation
                with web._CONFIG_MUTATION_LOCK:
                    run_native_mutation(data)
            await asyncio.to_thread(install)
        else:
            raise ValueError("unsupported_action")
        return result(lifecycle="next_session")
    if kind=="plugin":
        def plugin_action():
            from hermes_cli import plugins_cmd
            with web._CONFIG_MUTATION_LOCK, scope_context(scope):
                if action in ("enable","disable"):
                    return plugins_cmd.dashboard_set_agent_plugin_enabled(identifier,enabled=action=="enable")
                if action=="update":
                    return plugins_cmd.dashboard_update_user_plugin(identifier)
                if action=="remove":
                    return plugins_cmd.dashboard_remove_user_plugin(identifier)
                if action=="install":
                    return plugins_cmd.dashboard_install_plugin(identifier,force=False,enable=True)
                raise ValueError("unsupported_action")
        raw=await asyncio.to_thread(plugin_action)
        return result(raw.get("ok",False),lifecycle="next_session",detail_code="saved" if raw.get("ok") else "connection_action_failed")
    raise ValueError("unsupported_action")
