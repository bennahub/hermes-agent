"""Asera plugin projection over the existing Connections inventory.

This is not a second registry. Credentials, OAuth, MCP and Google Workspace
stay where they already live. The projection folds per-profile duplicates and
capability rows into one owner-facing plugin each, and maps internal health
onto four user statuses. Diagnostics keep reading ``/api/connections``.
"""
from __future__ import annotations

from hermes_cli.connections import build_inventory


USER_STATUSES = ("NOT_INSTALLED", "NEEDS_SIGN_IN", "CONNECTED", "RECONNECT_REQUIRED")

# Curated product plugins. Hermes ``kind=plugin`` rows (gateway platforms,
# image-gen backends) are not owner "Plugins".
CATALOG = (
    {
        "id": "gmail",
        "display_name": "Gmail",
        "description": "Search, read, send and manage email.",
        "category": "productivity",
        "icon": "envelope.fill",
        "connection_type": "google",
        "supports_multiple_accounts": True,
        "featured": True,
        "source_kind": "integration",
        "source_id": "google-workspace",
        "service": "gmail",
        "required_scopes": (
            "https://mail.google.com/",
            "https://www.googleapis.com/auth/gmail.settings.basic",
        ),
    },
    {
        "id": "google-calendar",
        "display_name": "Google Calendar",
        "description": "See and manage your calendar.",
        "category": "productivity",
        "icon": "calendar",
        "connection_type": "google",
        "supports_multiple_accounts": True,
        "featured": True,
        "source_kind": "integration",
        "source_id": "google-workspace",
        "service": "calendar",
        "required_scopes": ("https://www.googleapis.com/auth/calendar.readonly",),
    },
    {
        "id": "google-drive",
        "display_name": "Google Drive",
        "description": "Search and open files in Drive.",
        "category": "productivity",
        "icon": "externaldrive.fill",
        "connection_type": "google",
        "supports_multiple_accounts": True,
        "featured": True,
        "source_kind": "integration",
        "source_id": "google-workspace",
        "service": "drive",
        "required_scopes": ("https://www.googleapis.com/auth/drive.readonly",),
    },
    {
        "id": "github",
        "display_name": "GitHub",
        "description": "Work with repositories and issues.",
        "category": "developer",
        "icon": "chevron.left.forwardslash.chevron.right",
        "connection_type": "token",
        "supports_multiple_accounts": False,
        "featured": True,
        "source_kind": "integration",
        "source_id": "GITHUB_TOKEN",
        "service": "github",
        "required_scopes": (),
    },
    {
        "id": "jira",
        "display_name": "Jira",
        "description": "Track issues and projects.",
        "category": "productivity",
        "icon": "checkmark.circle",
        "connection_type": "mcp",
        "supports_multiple_accounts": False,
        "featured": True,
        "source_kind": "mcp",
        "source_id": "atlassian",
        "service": "jira",
        "required_scopes": (),
    },
)

_REAUTH_STATES = frozenset({"needs_auth", "expired"})
_BROKEN_STATES = frozenset({"error", "needs_configuration"})
_USABLE_STATES = frozenset({"connected", "configured", "enabled"})
_INSTALLED_MCP = frozenset({"configured", "connected", "needs_auth", "disabled", "error", "expired"})


def _google_identity(home=None):
    """Email on the stored Google token, never the token itself."""
    try:
        from hermes_cli.google_workspace_onboarding import describe, credential_module
        from hermes_constants import get_default_hermes_root
        from pathlib import Path
        import json
        root = Path(home) if home is not None else Path(get_default_hermes_root())
        info = describe(root)
        store = credential_module()
        path = store.token_path(root, get_default_hermes_root())
        account = None
        if path.is_file() and not path.is_symlink() and path.stat().st_size < 65536:
            payload = json.loads(path.read_text())
            for candidate in (payload.get("account"), (payload.get("_hermes_gmail_verification") or {}).get("account")):
                if isinstance(candidate, str) and "@" in candidate and len(candidate) < 256:
                    account = candidate
                    break
            scopes = payload.get("scopes") or []
            if isinstance(scopes, str):
                scopes = scopes.split()
        else:
            scopes = []
        return {
            "client_present": bool(info.get("client_present")),
            "token_present": bool(info.get("token_present")),
            "verification": info.get("verification"),
            "account": account,
            "scopes": list(scopes) if isinstance(scopes, (list, tuple)) else [],
            "refreshable": bool(info.get("token_present")),
        }
    except Exception:
        return {"client_present": False, "token_present": False, "verification": None,
                "account": None, "scopes": [], "refreshable": False}


def _primary_rows(entries, kind, identifier):
    """Prefer the shared default-scope row; never treat agent profiles as accounts."""
    matched = [e for e in entries if e.get("kind") == kind and e.get("id") == identifier]
    if not matched:
        return []
    default = [e for e in matched if e.get("scope") == "default"]
    return default or matched[:1]


def _user_status(entry, *, token_usable, installed=True, service_ready=True):
    if not installed:
        return "NOT_INSTALLED"
    if not service_ready and token_usable:
        return "NEEDS_SIGN_IN"
    state = entry.get("state") if entry else None
    owner = entry.get("owner_action") if entry else None
    if owner == "reauthorize" or (state in _REAUTH_STATES and token_usable):
        return "RECONNECT_REQUIRED"
    if state in _REAUTH_STATES and not token_usable:
        return "NEEDS_SIGN_IN"
    if token_usable or state in _USABLE_STATES:
        return "CONNECTED"
    if state in _BROKEN_STATES and owner == "configure" and not token_usable:
        return "NEEDS_SIGN_IN"
    if state in ("available", "disabled") or entry is None:
        return "NOT_INSTALLED" if not installed else "NEEDS_SIGN_IN"
    return "NEEDS_SIGN_IN"


def _account(label, status, *, scope="default", reconnect=False):
    return {"id": label, "display": label, "status": status, "scope": scope, "reconnect": reconnect}


def _plugin_actions(status, spec):
    if status == "NOT_INSTALLED":
        return ["add"]
    if status == "NEEDS_SIGN_IN":
        return ["connect"]
    if status == "RECONNECT_REQUIRED":
        return ["reconnect", "disconnect"]
    actions = ["disconnect"]
    if spec.get("supports_multiple_accounts"):
        actions.append("add_account")
    return actions


def _scopes_cover(granted, required):
    if not required:
        return True
    have = {str(s) for s in granted}
    return set(required).issubset(have)


def project_plugins(inventory, *, google=None):
    """Fold a Connections inventory into owner-facing plugins."""
    entries = list(inventory.get("entries") or [])
    google = google or {}
    plugins = []
    for spec in CATALOG:
        rows = _primary_rows(entries, spec["source_kind"], spec["source_id"])
        entry = rows[0] if rows else None
        if spec["connection_type"] == "google":
            token_usable = bool(google.get("token_present") or google.get("refreshable")
                                or google.get("verification") or (entry and entry.get("state") in _USABLE_STATES))
            # A missing 24h receipt is not Reconnect. Transient verify gaps stay Connected.
            if entry and entry.get("owner_action") == "reauthorize":
                token_usable = False
            installed = True
            service_ready = _scopes_cover(google.get("scopes") or [], spec.get("required_scopes") or ())
            if spec["id"] == "gmail":
                # Gmail is the live Google grant. Token present ⇒ service ready
                # even when the receipt cache is empty.
                service_ready = service_ready or token_usable
            status = _user_status(entry, token_usable=token_usable, installed=installed,
                                  service_ready=service_ready)
            accounts = []
            label = google.get("account")
            if label and status in ("CONNECTED", "RECONNECT_REQUIRED"):
                accounts.append(_account(label, status, scope=(entry or {}).get("scope") or "default",
                                         reconnect=status == "RECONNECT_REQUIRED"))
        elif spec["source_kind"] == "mcp":
            installed = bool(entry) and entry.get("state") in _INSTALLED_MCP
            token_usable = bool(entry) and entry.get("state") in _USABLE_STATES
            status = _user_status(entry, token_usable=token_usable, installed=installed)
            accounts = []
        else:
            token_usable = bool(entry) and entry.get("state") in _USABLE_STATES
            status = _user_status(entry, token_usable=token_usable, installed=True)
            accounts = []
            if token_usable and entry:
                accounts.append(_account(spec["display_name"], "CONNECTED",
                                         scope=entry.get("scope") or "default"))
        plugins.append({
            "id": spec["id"],
            "display_name": spec["display_name"],
            "description": spec["description"],
            "category": spec["category"],
            "icon": spec["icon"],
            "connection_type": spec["connection_type"],
            "supports_multiple_accounts": bool(spec["supports_multiple_accounts"]),
            "featured": bool(spec.get("featured")),
            "status": status,
            "accounts": accounts,
            "actions": _plugin_actions(status, spec),
            "source": {"kind": spec["source_kind"], "id": spec["source_id"],
                       "scope": (entry or {}).get("scope") or "default"},
        })
    seen = {p["id"] for p in plugins}
    for entry in entries:
        if entry.get("kind") != "mcp" or entry.get("state") not in _INSTALLED_MCP:
            continue
        if entry.get("id") in seen or any(p["source"]["id"] == entry.get("id") for p in plugins):
            continue
        if entry.get("scope") not in (None, "default"):
            continue
        status = _user_status(entry, token_usable=entry.get("state") in _USABLE_STATES, installed=True)
        plugins.append({
            "id": f"mcp:{entry['id']}",
            "display_name": entry.get("title") or entry["id"],
            "description": "Connected workspace.",
            "category": "productivity",
            "icon": "puzzlepiece.extension",
            "connection_type": "mcp",
            "supports_multiple_accounts": False,
            "featured": False,
            "status": status,
            "accounts": [],
            "actions": _plugin_actions(status, {"supports_multiple_accounts": False}),
            "source": {"kind": "mcp", "id": entry["id"], "scope": entry.get("scope") or "default"},
        })
        seen.add(entry["id"])
    installed = sum(1 for p in plugins if p["status"] == "CONNECTED")
    return {
        "schema_version": 1,
        "plugins": plugins,
        "installed_count": installed,
        "categories": ["featured", "productivity", "communication", "developer", "installed"],
    }


def build_plugin_index(oauth_catalog, *, home=None):
    inventory = build_inventory(oauth_catalog)
    return project_plugins(inventory, google=_google_identity(home))


def resolve_plugin_action(plugin_id, action, plugins):
    """Map a plugin action onto the existing Connections action tuple."""
    plugin = next((p for p in plugins if p["id"] == plugin_id), None)
    if plugin is None:
        raise ValueError("unknown_plugin")
    if action not in plugin["actions"] and action not in ("auth_poll", "auth_cancel", "auth_submit"):
        raise ValueError("unsupported_action")
    source = plugin["source"]
    mapped = {"add": "connect", "add_account": "connect", "connect": "connect",
              "reconnect": "reconnect" if source["kind"] != "integration" or source["id"] != "google-workspace"
              else "connect",
              "disconnect": "disconnect"}.get(action, action)
    if source["kind"] == "mcp" and action == "add":
        mapped = "install"
    return {"kind": source["kind"], "id": source["id"], "scope": source.get("scope") or "default",
            "action": mapped}
