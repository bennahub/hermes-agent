"""Read-only native connection cards, persisted as ordinary tool results."""
import json
import re
from tools.registry import registry

KINDS = ("account", "integration", "credential", "mcp", "plugin")
_ID = re.compile(r"[A-Za-z0-9@][A-Za-z0-9_./:@+-]{0,159}\Z")

def _text(value, maximum):
    return isinstance(value, str) and 0 < len(value) <= maximum and not any(ord(c) < 32 for c in value)

def normalize_connection_request(result):
    """Closed display projection; never render arbitrary tool JSON as actions."""
    if isinstance(result, str):
        if len(result) > 4096:
            return None
        try:
            result = json.loads(result)
        except (TypeError, ValueError):
            return None
    if not isinstance(result, dict) or result.get("ok") is not True or "error" in result:
        return None
    card = result.get("connection_request")
    if not isinstance(card, dict) or set(card) != {"schema_version", "kind", "id", "scope", "title", "reason"}:
        return None
    if type(card["schema_version"]) is not int or card["schema_version"] != 1 or card["kind"] not in KINDS:
        return None
    if any(not isinstance(card[k], str) or not _ID.fullmatch(card[k]) for k in ("id", "scope")):
        return None
    if not _text(card["title"], 160) or not _text(card["reason"], 500):
        return None
    return {"ok": True, "connection_request": dict(card)}

def _profile():
    from hermes_cli.profiles import get_active_profile_name
    return get_active_profile_name() or "default"

def _inventory():
    from hermes_cli.connections import build_inventory
    from hermes_cli.web_routers.oauth import _build_oauth_catalog
    return build_inventory(_build_oauth_catalog())

def request_connection(kind, id, reason, scope=None):
    """Only creates a display request: no login, writes, shell, or browser."""
    try:
        caller = _profile()
        scope = caller if scope in (None, "", "current") else scope
        if scope not in (caller, "default"):
            return json.dumps({"ok": False, "error": "scope_not_permitted", "allowed_scopes": ["current", "default"]})
        if kind not in KINDS or not isinstance(id, str) or not _ID.fullmatch(id) or not _text(reason, 500):
            raise ValueError("invalid_request")
        inventory = _inventory()
        entry = next((e for e in inventory["entries"] if e["kind"] == kind and e["id"] == id and e["scope"] == scope), None)
        if entry is None:
            return json.dumps({"ok": False, "error": "connection_not_found_in_scope", "scope": scope,
                               "next_tool": "list_connections",
                               "lookup_scope": "default" if scope != "default" else scope})
        result = normalize_connection_request({"ok": True, "connection_request": {
            "schema_version": 1, "kind": kind, "id": id, "scope": scope,
            "title": entry["title"], "reason": reason,
        }})
        if result is None:
            raise ValueError("invalid_connection")
        return json.dumps(result, ensure_ascii=False)
    except Exception:
        # No exception text, auth data, configured fields, paths or URLs leave this tool.
        return json.dumps({"ok": False, "error": "connection_request_unavailable"})

SCHEMA = {
    "name": "request_connection",
    "description": (
        "Show the Owner an actionable connection card INSIDE the conversation for an account, "
        "integration, MCP server or plugin. Use this whenever the Owner asks to connect/setup a "
        "provider from the app or you need a missing connection. The Owner taps the card to open "
        "the canonical native Connections detail and completes setup privately. This only requests "
        "setup; it does not authorize or perform login/install/configuration and does not prove "
        "connected status. Do not use computer_use, terminal, shell, AppleScript or browser control "
        "to open/close Owner apps or perform this setup. Never request credentials in chat or put "
        "secrets in reason. Use list_connections first when the canonical ID is unknown. Use exact canonical connection ID (Anthropic API account: anthropic; "
        "Claude Code account is separate). Scope current (or omitted) means your current profile; default means "
        "shared Hermes installation. Other agents' scopes are not permitted. If connection_not_found_in_scope is returned, follow next_tool/lookup_scope to discover shared entries before reporting unavailable. If unavailable, "
        "explain it without falling back to desktop control."
    ),
    "parameters": {"type": "object", "additionalProperties": False, "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "id": {"type": "string", "description": "Exact canonical Connections entry ID."},
        "scope": {"type": "string", "description": "Use current (or omit) for current agent profile, or default for shared Hermes."},
        "reason": {"type": "string", "maxLength": 500, "description": "Brief Owner-facing purpose, no secrets or commands."},
    }, "required": ["kind", "id", "reason"]},
}
registry.register(name="request_connection", toolset="connections", schema=SCHEMA,
                  handler=lambda args, **kw: request_connection(**args), emoji="🔗")


def list_connections(scope=None, kind=None, query="", offset=0):
    """Bounded safe discovery, never credential fields or auth URLs."""
    try:
        caller = _profile()
        scope = caller if scope in (None, "", "current") else scope
        if scope not in (caller, "default"):
            return json.dumps({"ok": False, "error": "scope_not_permitted", "allowed_scopes": ["current", "default"]})
        if kind not in (None, "", *KINDS):
            return json.dumps({"ok": False, "error": "invalid_kind"})
        if not isinstance(query, str) or len(query) > 160 or any(ord(c) < 32 for c in query):
            raise ValueError("invalid_query")
        if type(offset) is not int or not 0 <= offset <= 10000:
            raise ValueError("invalid_offset")
        rows = [e for e in _inventory()["entries"] if e["scope"] == scope
                and (not kind or e["kind"] == kind)
                and (not query or query.casefold() in (e["id"] + " " + e["title"]).casefold())]
        entries = [{k: e[k] for k in ("kind", "id", "title", "scope", "state", "scope_kind", "auth_type", "actions", "detail_code") if k in e} for e in rows[offset:offset+50]]
        return json.dumps({"ok": True, "entries": entries, "total": len(rows), "offset": offset,
                           "next_offset": offset+50 if offset+50 < len(rows) else None,
                           "scope": scope,
                           "next_tool": "list_connections" if not rows and scope != "default" else None,
                           "lookup_scope": "default" if not rows and scope != "default" else None}, ensure_ascii=False)
    except Exception:
        return json.dumps({"ok": False, "error": "connections_unavailable"})

LIST_SCHEMA = {
    "name": "list_connections",
    "description": "Read canonical connection IDs and status for your current profile (omit scope) or shared Hermes (scope default). Use before request_connection when the exact ID is unknown. Results never include credentials or login URLs. Configured is not proof of connected. If no matching entry exists in current scope, search scope default explicitly for shared setup; never assume a local entry exists. Page using next_offset; this does not connect or modify anything.",
    "parameters": {"type": "object", "additionalProperties": False, "properties": {
        "scope": {"type": "string", "description": "current (or omit) for this agent; default for shared Hermes."}, "kind": {"type": "string", "enum": list(KINDS)},
        "query": {"type": "string", "maxLength": 160}, "offset": {"type": "integer", "minimum": 0, "maximum": 10000},
    }},
}
registry.register(name="list_connections", toolset="connections", schema=LIST_SCHEMA,
                  handler=lambda args, **kw: list_connections(**args), emoji="🔗")
