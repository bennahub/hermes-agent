"""Bounded native credential mutation worker.

The CLI's canonical writers intentionally update process environment. Running
them in this short-lived process preserves that behavior without exposing a
profile's secret to concurrent readers in the shared server process.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys


def mutate(data):
    from hermes_cli.connections import scope_context, secret_fields
    from hermes_cli.config import is_managed, load_env, save_env_value, validate_env_var_name_for_write
    from hermes_cli import managed_scope
    from hermes_cli.credential_lifecycle import save_provider_env_credential, remove_provider_env_credential
    kind, identifier, action = (data[k] for k in ("kind", "id", "action"))
    with scope_context(data.get("scope") or "default"):
        if is_managed():
            raise ValueError("managed_credentials")
        if action == "save_credential":
            key, value = data["field_id"], data["value"]
            if managed_scope.is_env_managed(key):
                raise ValueError("managed_credentials")
            if kind == "mcp":
                from hermes_cli import mcp_catalog
                from hermes_cli.mcp_config import _get_mcp_servers, _save_mcp_server, _save_bearer_auth_token, _env_key_for_server
                server = dict(_get_mcp_servers()[identifier])
                catalog = mcp_catalog.get_entry(identifier)
                declared = {e.name for e in catalog.auth.env} if catalog else set()
                if server.get("url") and key == _env_key_for_server(identifier):
                    server["headers"] = _save_bearer_auth_token(identifier, value)
                elif key in declared:
                    save_env_value(key, value)
                else:
                    raise ValueError("undeclared_credential")
                if not _save_mcp_server(identifier, server):
                    raise ValueError("configuration_rejected")
            else:
                fields = secret_fields()
                if key not in fields or (kind == "account" and fields[key]["provider"] != identifier) or (kind in ("integration", "credential") and identifier != key):
                    raise ValueError("undeclared_credential")
                save_provider_env_credential(key, value)
            return
        if kind == "account" and action == "disconnect":
            from hermes_cli.auth import clear_provider_auth, PROVIDER_REGISTRY
            from hermes_cli.connections import secret_fields
            registered = PROVIDER_REGISTRY.get(identifier)
            if not registered:
                raise ValueError("unknown_provider")
            fields = secret_fields()
            # Only provider-owned keys. A Copilot disconnect must not revoke
            # GitHub's shared operational integration token.
            for key in registered.api_key_env_vars:
                if key in fields and fields[key]["provider"] == identifier:
                    if managed_scope.is_env_managed(key):
                        raise ValueError("managed_credentials")
                    remove_provider_env_credential(key)
            clear_provider_auth(identifier)
            return
        if kind == "mcp" and action == "install":
            from hermes_cli import mcp_catalog
            from hermes_cli.mcp_config import _get_mcp_servers, _save_mcp_server
            entry = mcp_catalog.get_entry(identifier)
            if entry is None or entry.install is not None or identifier in _get_mcp_servers():
                raise ValueError("unsupported_install")
            values = dict(data.get("values") or {})
            if data.get("field_id") and data.get("value"):
                values[data["field_id"]] = data["value"]
            declared = {spec.name: spec for spec in entry.auth.env}
            if set(values) - set(declared) or any(managed_scope.is_env_managed(key) for key in values):
                raise ValueError("undeclared_credential")
            existing = load_env()
            # Validate the entire intended write set before the first key is
            # persisted; native env name rules include reserved control keys.
            for key, spec in declared.items():
                if values.get(key) or (spec.default and not existing.get(key)):
                    validate_env_var_name_for_write(key)
            for key, spec in declared.items():
                if spec.required and not (values.get(key) or existing.get(key) or spec.default):
                    raise ValueError("credential_required")
            # Native manifest translation + writer, without install_entry's
            # terminal prompts/curses/network probe. Test is a separate action.
            server = mcp_catalog._build_server_config(entry, None)
            server["enabled"] = True
            if entry.tools.default_enabled is not None:
                server["tools"] = {"include": list(entry.tools.default_enabled)}
            elif entry.tools.default_excluded is not None:
                server["tools"] = {"exclude": list(entry.tools.default_excluded)}
            from hermes_cli.mcp_security import validate_mcp_server_entry
            if validate_mcp_server_entry(identifier, server):
                raise ValueError("configuration_rejected")
            for key, spec in declared.items():
                value = values.get(key) or (spec.default if not existing.get(key) else None)
                if value:
                    save_env_value(key, value)
            if not _save_mcp_server(identifier, server):
                raise ValueError("configuration_rejected")
            return
        raise ValueError("unsupported_action")


def main():
    ok = False
    try:
        data = json.loads(sys.stdin.buffer.read(128 * 1024 + 1))
        # Native helpers may print setup text. No output or exception payload
        # crosses back to the server, and no credential enters argv/logs.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            mutate(data)
        ok = True
    except Exception:
        pass
    sys.stdout.write(json.dumps({"ok": ok}))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
