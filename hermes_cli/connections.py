"""Secret-free owner projection of native Hermes connection primitives.

No credentials, accounts, plugins or MCP configuration are persisted here.
Listing reads local metadata only; it never loads a credential pool (which can
refresh/save), starts an MCP, imports plugin code or probes a service.
"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit


def run_native_mutation(data):
    """Run canonical CLI writers without sharing their process environment."""
    import os
    import subprocess
    import sys
    import hermes_cli
    from hermes_constants import get_default_hermes_root
    # The import roots are our loaded source identity, not request data. This
    # also lets an isolated candidate execute its exact worker before deploy.
    bootstrap = ("import sys;sys.path[:0]=" + repr(list(sys.path)) +
                 ";import hermes_cli;hermes_cli.__path__[:0]=" + repr(list(hermes_cli.__path__)) +
                 ";from hermes_cli.connections_worker import main;raise SystemExit(main())")
    env = dict(os.environ)
    env["HERMES_HOME"] = str(get_default_hermes_root())
    response = subprocess.run([sys.executable, "-c", bootstrap],
                              input=json.dumps(data).encode(), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=60, env=env, check=False)
    if response.returncode != 0 or len(response.stdout) > 1024:
        raise ValueError("native_write_failed")
    if json.loads(response.stdout) != {"ok": True}:
        raise ValueError("native_write_failed")
    if data.get("scope", "default") == "default":
        # A GLOBAL rotation intentionally changes the launch fallback, after
        # canonical persistence succeeds. A profile mutation never enters this
        # branch, even transiently. Publish only keys this operation owns.
        with scope_context():
            from hermes_cli.config import load_env
            keys = []
            if data["action"] == "save_credential":
                keys = [data["field_id"]]
            elif data["action"] == "disconnect":
                keys = [key for key, spec in secret_fields().items() if spec["provider"] == data["id"]]
            elif data["kind"] == "mcp" and data["action"] == "install":
                from hermes_cli import mcp_catalog
                entry = mcp_catalog.get_entry(data["id"])
                keys = [spec.name for spec in entry.auth.env] if entry else []
            current = load_env()
            for key in keys:
                if key in current:
                    os.environ[key] = current[key]
                else:
                    os.environ.pop(key, None)


def clean_title(value, fallback="Connection"):
    text = str(value or "")
    if len(text) > 120 or any(ord(c) < 32 for c in text):
        return fallback
    if any(marker in text.lower() for marker in ("bearer ", "sk-", "ghp_", "gho_", "eyj")):
        return fallback
    return text or fallback


def origin(value):
    """Never return URL credentials, path, query or fragment."""
    try:
        parts = urlsplit(str(value or ""))
        if parts.scheme not in ("https", "http") or not parts.hostname:
            return None
        return parts.scheme + "://" + parts.hostname
    except ValueError:
        return None


@contextmanager
def scope_context(scope="default"):
    from hermes_cli.profiles import get_profile_dir, profile_exists, validate_profile_name
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    validate_profile_name(scope)
    if not profile_exists(scope):
        raise ValueError("unknown_scope")
    token = set_hermes_home_override(str(get_profile_dir(scope)))
    try:
        yield get_profile_dir(scope)
    finally:
        reset_hermes_home_override(token)


def row(identifier, kind, title, state, scope="default", **extra):
    return {"id": str(identifier), "kind": kind, "title": clean_title(title),
            "state": state, "scope": scope, "scope_kind": "PROFILE_SPECIFIC",
            "actions": [], "fields": [], "lifecycle": "immediate", **extra}


def credential_state(entries, legacy, env_present=False):
    """Presence/explicit expiry only. Never call presence 'connected'."""
    present = [e for e in entries if isinstance(e, dict) and
               any(e.get(k) for k in ("access_token", "api_key", "refresh_token", "token"))]
    if isinstance(legacy, dict) and any(legacy.get(k) for k in ("access_token", "api_key", "refresh_token", "token")):
        present.append(legacy)
    if env_present:
        return "configured", max(1, len(present))
    if not present:
        return "needs_auth", 0
    if all(e.get("last_status") == "dead" for e in present):
        return "needs_auth", len(present)
    if all(e.get("last_status") in ("dead", "exhausted") for e in present):
        return "error", len(present)
    expired = []
    for item in present:
        expiry = item.get("expires_at") or item.get("access_expires_at")
        try:
            if isinstance(expiry, str) and not expiry.replace(".", "", 1).isdigit():
                from datetime import datetime
                expiry = datetime.fromisoformat(expiry.replace("Z", "+00:00")).timestamp()
            expired.append(bool(expiry) and float(expiry) <= time.time() and not item.get("refresh_token"))
        except (ValueError, TypeError, OverflowError):
            expired.append(False)
    return ("expired" if all(expired) else "configured"), len(present)


def singleton_account_state(identifier):
    """Live state for an account whose credential lives in a shared singleton file rather than in
    ``auth.json`` (today: the borrowed Claude Code grant behind ``claude-code``).

    ``build_inventory`` otherwise projects accounts purely from the ``credential_pool``, so a
    provider the runtime is actively USING through a singleton renders as a permanently static
    zero-action row — the exact divergence that let a revoked grant sit unreported for 44 hours.
    Reads metadata only: presence and expiry, never a token value.
    """
    try:
        from agent.connection_health import SINGLETON_BACKED_CONNECTIONS as _singletons
    except Exception:
        _singletons = frozenset({"claude-code"})
    if identifier not in _singletons:
        return None
    try:
        from agent.anthropic_credentials import (
            claude_code_credentials_path, is_claude_code_token_valid,
            is_rotation_consumed_uncommitted, read_claude_code_credentials,
        )
        creds = read_claude_code_credentials()
    except Exception:
        return None
    if not creds or not creds.get("accessToken"):
        return "needs_auth", 0, "sign_in_required"
    try:
        spent = is_rotation_consumed_uncommitted(creds.get("accessToken", ""), source_path=claude_code_credentials_path())
    except Exception:
        spent = False
    if spent:
        # A single-use rotation was consumed but never committed: only fresh consent recovers this.
        return "needs_auth", 1, "reauthorization_required"
    if is_claude_code_token_valid(creds):
        return "configured", 1, "credential_present"
    # Expired. Refreshable is a Hermes-recoverable state; unrefreshable needs the Owner.
    return ("configured" if creds.get("refreshToken") else "expired"), 1, (
        "credential_refreshable" if creds.get("refreshToken") else "reauthorization_required")


def canonical_fault(connection_id, scope=None):
    """The runtime's recorded verdict for one Connections row, or None.

    THE lookup for every owner-facing projection of connection health. ``/api/connections`` reads it
    through :func:`apply_observed_fault`; ``/api/providers/oauth`` reads it directly. Two surfaces
    that answer the same question from different sources is precisely the divergence this taxonomy
    exists to end — the Accounts tab reported a revoked grant as signed in for the whole outage
    because it only ever consulted local metadata.

    Pass *scope* when the caller already knows the row's scope (the inventory does). Omit it and the
    owning scope is resolved exactly as ``record_fault`` resolved it on the way in, so a lookup can
    never miss a fault by asking under a different key than the one it was written under.
    """
    key = str(connection_id or "").strip()
    if not key:
        return None
    try:
        from agent.connection_health import observed_fault, owning_scope
        return observed_fault(key, scope if scope else owning_scope(key))
    except Exception:
        return None


def apply_observed_fault(entry):
    """Overlay the runtime's observed verdict onto a projected row, in place.

    Local metadata cannot see a revoked grant — the stored token still looks perfectly valid. Only
    the runtime knows, because only the runtime made the call. This is the join that makes the two
    projections agree: when the runtime has recorded a fault for this row, the row reports it and
    offers ``reconnect``.

    Looked up by the row's OWN scope. A fault the runtime observed while serving profile ``badr``
    against a globally shared credential was recorded under ``default`` (``owning_scope``), so it
    lands on the ``default`` row here — the row the Connections screen actually renders.
    """
    fault = canonical_fault(entry["id"], entry.get("scope") or "default")
    if not fault or entry.get("state") == "disabled":
        # A disabled server makes no calls, so a fault recorded before it was turned off says
        # nothing about now; reporting it would ask the Owner to reconnect something they switched
        # off themselves. It is not cleared either — re-enabling restores the honest verdict.
        return entry
    # The row reports the runtime's verdict and nothing else. What local metadata believed underneath
    # is deliberately NOT published: for a revoked grant it says "configured", and every consumer that
    # trusted it (``reconnect`` did, once) turns that into a fabricated recovery.
    entry["state"] = "needs_configuration" if fault.get("owner_action") == "configure" else "needs_auth"
    entry["detail_code"] = str(fault.get("reason_code") or "reauthorization_required")
    entry["owner_action"] = str(fault.get("owner_action") or "reauthorize")
    for key in ("first_seen_at", "last_seen_at"):
        if isinstance(fault.get(key), (int, float)):
            entry[key] = float(fault[key])
    if "reconnect" not in entry["actions"]:
        entry["actions"] = [*entry["actions"], "reconnect"]
    return entry


def secret_fields():
    """Allow only declared owner-facing credentials, never arbitrary env keys."""
    from hermes_cli.config import OPTIONAL_ENV_VARS
    from hermes_cli.provider_catalog import provider_catalog
    from agent.secret_scope import is_deployment_only_secret
    result = {}
    for descriptor in provider_catalog():
        for key in descriptor.api_key_env_vars:
            if (OPTIONAL_ENV_VARS.get(key) or {}).get("category") not in (None, "", "provider"):
                continue  # Shared tool credentials remain integrations.
            if not is_deployment_only_secret(key):
                result[key] = {"id": key, "title": clean_title(descriptor.label), "secret": True,
                               "required": True, "provider": descriptor.slug}
    for key, info in OPTIONAL_ENV_VARS.items():
        if info.get("password") and info.get("category") in ("tool", "tools") and not is_deployment_only_secret(key):
            tools = info.get("tools") or []
            title = clean_title(info.get("provider_label") or info.get("name") or
                                (str(tools[0]).replace("_", " ").title() if tools else info.get("description")), "Integration")
            result.setdefault(key, {"id": key, "title": title,
                                    "secret": True, "required": True, "provider": ""})
    return result


def auth_metadata(path, entries, scope):
    try:
        data = json.loads(path.read_text()) if path.is_file() else {}
        if not isinstance(data, dict) or any(not isinstance(data.get(k, {}), dict) for k in ("providers", "credential_pool")):
            raise ValueError("invalid_auth_metadata")
        return data
    except (OSError, ValueError, TypeError):
        entries.append(row("auth-store", "account", "Account configuration", "error", scope,
                           detail_code="configuration_invalid"))
        return {}


def build_inventory(oauth_catalog):
    from hermes_cli.config import load_config, load_env
    from hermes_cli.provider_catalog import provider_catalog
    from hermes_cli.profiles import get_profile_dir
    from hermes_cli import mcp_catalog, plugins_cmd
    from hermes_cli.web_routers import config_env
    from hermes_cli.mcp_config import _oauth_tokens_present, _env_key_for_server
    entries = []
    scopes = [{"id": "default", "title": "Hermes"}]
    with scope_context() as home:
        root = Path(home)
        env = load_env()
        auth = auth_metadata(root / "auth.json", entries, "default")
        providers = auth.get("providers") or {}
        pools = auth.get("credential_pool") or {}
        fields = secret_fields()
        descriptors = {d.slug: d for d in provider_catalog()}
        oauth = {str(p["id"]): p for p in oauth_catalog}
        for identifier in dict.fromkeys([*oauth, *descriptors]):
            desc = descriptors.get(identifier)
            meta = oauth.get(identifier, {})
            title = meta.get("name") or (desc.label if desc else identifier)
            keys = [k for k, f in fields.items() if f["provider"] == identifier]
            state, count = credential_state(pools.get(identifier) or [], providers.get(identifier), any(env.get(k) for k in keys))
            flow = meta.get("flow", "api_key" if keys else "external")
            actions = ["connect"] if flow == "device_code" else []
            if keys:
                actions.append("save_credential")
            if any(env.get(key) and key in config_env._CREDENTIAL_PROBES for key in keys):
                actions.append("test")
            if count and identifier != "claude-code":
                actions.append("disconnect")
            detail_code = "credential_present" if count else "sign_in_required"
            # A singleton-backed account (the borrowed Claude Code grant) has no pool row to read;
            # its live state comes from the credential file the runtime itself resolves from.
            singleton = singleton_account_state(identifier)
            if singleton is not None:
                state, count, detail_code = singleton
            if not count and not actions:
                state = "external_gate"
            entries.append(apply_observed_fault(
                row(identifier, "account", title, state, auth_type=flow,
                    identity_count=count, actions=actions,
                    scope_kind="GLOBAL_SHARED" if singleton is not None or credential_state(pools.get(identifier) or [], providers.get(identifier))[1] else "PROFILE_SPECIFIC",
                    fields=[{k:v for k,v in fields[key].items() if k != "provider"} for key in keys],
                    detail_code=detail_code)))
        for key, field in fields.items():
            if field["provider"]:
                continue
            entries.append(row(key, "integration", field["title"], "configured" if env.get(key) else "needs_configuration",
                               actions=["save_credential"], fields=[{k:v for k,v in field.items() if k != "provider"}],
                               detail_code="credential_present" if env.get(key) else "credential_missing"))
        profile_root = get_profile_dir("default") / "profiles"
        if profile_root.is_dir():
            scopes += [{"id": p.name, "title": clean_title(p.name)} for p in sorted(profile_root.iterdir())
                       if p.is_dir() and not p.is_symlink() and (p / "config.yaml").is_file()]
        catalog = {e.name:e for e in mcp_catalog.list_catalog()}
    for scope in scopes:
        with scope_context(scope["id"]) as scope_home:
            cfg = load_config()
            from hermes_cli.google_workspace_onboarding import describe
            google = describe(scope_home)
            configured = google["client_present"]
            entries.append(row(
                "google-workspace", "integration", "Gmail / Google Workspace",
                "connected" if google["verification"] else "configured" if google["token_present"] else "needs_auth" if configured else "needs_configuration",
                scope["id"], auth_type="manual_code", scope_kind="GLOBAL_SHARED" if google.get("shared") or scope["id"] == "default" else "PROFILE_SPECIFIC",
                actions=["save_credential"] + (["connect"] if configured and google["dependencies_available"] else [])
                    + (["test"] if google["token_present"] and google["dependencies_available"] else []),
                fields=[{"id":"google_client_secret_json", "title":"Google OAuth client JSON",
                         "secret":True, "required":not configured, "format":"json", "multiline":True}],
                requested_scopes=google["requested_scopes"],
                detail_code="google_verified" if google["verification"] else "credential_present" if google["token_present"] else
                    "google_client_required" if not configured else
                    "dependencies_unavailable" if not google["dependencies_available"] else "sign_in_required",
            ))
            if scope["id"] != "default":
                path = get_profile_dir(scope["id"]) / "auth.json"
                local_auth = auth_metadata(path, entries, scope["id"])
                local_providers = local_auth.get("providers") or {}
                local_pools = local_auth.get("credential_pool") or {}
                local_env = load_env()
                selected = (cfg.get("model") or {}).get("provider") if isinstance(cfg.get("model"), dict) else None
                env_providers = [spec["provider"] for key, spec in fields.items() if spec["provider"] and local_env.get(key)]
                for identifier in dict.fromkeys([*local_providers, *local_pools, *env_providers, *([selected] if selected else [])]):
                    desc = descriptors.get(identifier)
                    meta = oauth.get(identifier, {})
                    keys = [k for k, f in fields.items() if f["provider"] == identifier]
                    state, count = credential_state(local_pools.get(identifier) or [], local_providers.get(identifier), any(local_env.get(k) for k in keys))
                    if not count and identifier != selected:
                        continue  # historical empty pools are not failed accounts
                    flow = meta.get("flow", "api_key" if keys else "external")
                    actions = ["connect"] if flow == "device_code" else []
                    if keys:
                        actions.append("save_credential")
                    if any(local_env.get(key) and key in config_env._CREDENTIAL_PROBES for key in keys):
                        actions.append("test")
                    if count and identifier != "claude-code":
                        actions.append("disconnect")
                    if not count:
                        global_state, global_count = credential_state(pools.get(identifier) or [], providers.get(identifier))
                        if global_count:
                            # Canonical read_credential_pool fallback; no copy
                            # or write to the profile's auth file is performed.
                            continue
                        if not actions:
                            state = "external_gate"
                    # A profile that supplies its OWN credential owns the fault for it
                    # (agent.connection_health.owning_scope), so this row must read the overlay too
                    # — otherwise a profile-scoped fault would be recorded and never rendered.
                    entries.append(apply_observed_fault(
                        row(identifier, "account", meta.get("name") or (desc.label if desc else identifier), state,
                            scope["id"], auth_type=flow, identity_count=count, actions=actions,
                            fields=[{k:v for k,v in fields[key].items() if k != "provider"} for key in keys],
                            detail_code="profile_credential_override" if count else "sign_in_required")))
                for key, field in fields.items():
                    if not field["provider"] and local_env.get(key):
                        entries.append(row(key, "integration", field["title"], "configured", scope["id"],
                                           actions=["save_credential"], fields=[{k:v for k,v in field.items() if k != "provider"}],
                                           detail_code="profile_credential_override"))
            servers = cfg.get("mcp_servers") or {}
            for name, server in sorted(servers.items()):
                if not isinstance(server, dict):
                    entries.append(row(name, "mcp", name, "error", scope["id"], detail_code="configuration_invalid"))
                    continue
                enabled = server.get("enabled", True) is not False
                auth_type = server.get("auth") or ("header" if server.get("headers") else "none")
                # Presence only, and presence is not health: a REVOKED token is still a well-formed
                # file with a future expiry, so this alone reported a dead Atlassian/Jira grant as
                # ``configured`` for as long as it sat on disk — the account-side failure exactly,
                # one kind over. ``apply_observed_fault`` below is what corrects it, from the
                # runtime's own observation.
                has_oauth = _oauth_tokens_present(name) if auth_type == "oauth" else False
                state = "disabled" if not enabled else "needs_auth" if auth_type == "oauth" and not has_oauth else "configured"
                actions = ["disable" if enabled else "enable", "test", "remove"]
                entry_fields = []
                if auth_type == "oauth" and server.get("url"):
                    actions.append("connect")
                elif server.get("url") and auth_type == "header":
                    actions.append("save_credential")
                    entry_fields = [{"id": _env_key_for_server(name), "title": "API key", "secret": True, "required": True}]
                if name in catalog and catalog[name].auth.env:
                    entry_fields = [{"id": e.name, "title": clean_title(e.prompt, e.name), "secret": True,
                                     "required": e.required} for e in catalog[name].auth.env]
                    if "save_credential" not in actions:
                        actions.append("save_credential")
                    local_env = load_env()
                    if enabled and any(e["required"] and not local_env.get(e["id"]) for e in entry_fields):
                        state = "needs_configuration"
                # An MCP server is a Connection like any other: the overlay is what turns a
                # revoked-but-present grant from ``configured`` into ``needs_auth`` with a
                # ``reconnect`` action, and it must reach this kind too or the taxonomy stops at
                # accounts and the second broken provider stays invisible.
                entries.append(apply_observed_fault(
                    row(name, "mcp", name, state, scope["id"], enabled=enabled,
                        auth_type=auth_type, actions=actions, fields=entry_fields,
                        transport="http" if server.get("url") else "stdio", host=origin(server.get("url")),
                        lifecycle="next_session", detail_code="configuration_present")))
            for name, item in (catalog.items() if scope["id"] == "default" else []):
                if name in servers:
                    continue
                entry_fields = [{"id": e.name, "title": clean_title(e.prompt, e.name), "secret": True,
                                 "required": e.required} for e in item.auth.env or []]
                # Background git bootstraps require native action tracking; do
                # not pretend immediate success or expose an untracked install.
                entries.append(row(name, "mcp", name, "available", actions=["install"] if item.install is None else [],
                                   fields=entry_fields, transport=item.transport.type,
                                   auth_type=item.auth.type, lifecycle="next_session",
                                   detail_code="catalog_available" if item.install is None else "external_install_required"))
            enabled_set = plugins_cmd._get_enabled_set(); disabled_set = plugins_cmd._get_disabled_set()
            for name, version, desc, source, directory, key in sorted(plugins_cmd._discover_all_plugins()):
                if scope["id"] != "default" and source not in ("user", "git") and not ({name, key} & (enabled_set | disabled_set)):
                    continue  # Global inventory shows concrete local overrides, not duplicate bundled rows.
                status = plugins_cmd._plugin_status(name, enabled_set, disabled_set, key=key)
                active = status == "enabled" or (status == "not enabled" and source == "bundled" and plugins_cmd._bundled_default_on(directory))
                actions = ["disable" if active else "enable"]
                if source in ("user", "git"):
                    actions.append("remove")
                    if (Path(directory) / ".git").is_dir():
                        actions.append("update")
                entries.append(row(key or name, "plugin", name, "enabled" if active else "disabled", scope["id"],
                                   enabled=active, version=clean_title(version, ""), actions=actions,
                                   lifecycle="next_session", detail_code="enabled_not_health_checked" if active else "disabled"))
            if scope["id"] != "default":
                continue
            from hermes_cli.plugin_index import load_index
            installed_names = {e["title"] for e in entries if e["kind"] == "plugin"}
            indexed, _ = load_index(offline=True)
            for item in indexed:
                if item.name not in installed_names:
                    entries.append(row(item.install_identifier, "plugin", item.name, "available",
                                       actions=["install"], lifecycle="next_session", detail_code="native_catalog_available"))
    return {"schema_version":1,"scope":"global_management","entries":entries,"scopes":scopes,
            "assignment_supported":False,"mcp_add_supported":True,
            "summary":{"connected":sum(e["state"]=="connected" for e in entries),
                       "configured":sum(e["state"] in ("configured","enabled") for e in entries),
                       "needs_attention":sum(e["state"] in ("needs_auth","needs_configuration","expired","error") for e in entries)}}
