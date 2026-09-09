"""Asera plugin fold: integrations, not capabilities; no false Attention."""
from hermes_cli.asera_plugins import CATALOG, project_plugins, resolve_plugin_action


def _row(identifier, kind, title, state, scope="default", **extra):
    return {"id": identifier, "kind": kind, "title": title, "state": state,
            "scope": scope, "scope_kind": extra.pop("scope_kind", "GLOBAL_SHARED"),
            "actions": extra.pop("actions", []), "fields": extra.pop("fields", []),
            "lifecycle": "immediate", **extra}


def _inventory(rows):
    return {"schema_version": 1, "entries": rows, "scopes": [{"id": "default", "title": "Hermes"}]}


def test_gmail_is_one_plugin_despite_per_profile_rows_and_capabilities():
    rows = [_row("google-workspace", "integration", "Gmail / Google Workspace", "configured", scope)
            for scope in ("default", "faisal", "mishari")]
    rows += [_row("nous", "account", "Nous", "needs_auth"),
             [_row("browser/firecrawl", "plugin", "browser-firecrawl", "enabled")][0]]
    out = project_plugins(_inventory(rows), google={
        "token_present": True, "refreshable": True, "account": "owner@bennahub.com",
        "scopes": ["https://mail.google.com/", "https://www.googleapis.com/auth/gmail.settings.basic"],
        "verification": None, "client_present": True,
    })
    gmail = next(p for p in out["plugins"] if p["id"] == "gmail")
    assert gmail["status"] == "CONNECTED"
    assert [a["display"] for a in gmail["accounts"]] == ["owner@bennahub.com"]
    assert out["installed_count"] == 1
    assert not any(p["id"] == "nous" for p in out["plugins"])
    assert not any("search_mail" in p["id"] for p in out["plugins"])
    assert len([p for p in out["plugins"] if p["id"] == "gmail"]) == 1


def test_missing_verification_receipt_is_not_reconnect():
    rows = [_row("google-workspace", "integration", "Gmail / Google Workspace", "configured")]
    out = project_plugins(_inventory(rows), google={
        "token_present": True, "refreshable": True, "account": "a@b.com",
        "scopes": ["https://mail.google.com/"], "verification": None, "client_present": True,
    })
    assert next(p for p in out["plugins"] if p["id"] == "gmail")["status"] == "CONNECTED"


def test_expired_grant_asks_to_reconnect():
    rows = [_row("google-workspace", "integration", "Gmail / Google Workspace", "needs_auth",
                 owner_action="reauthorize", detail_code="reauthorization_required")]
    out = project_plugins(_inventory(rows), google={
        "token_present": True, "refreshable": False, "account": "a@b.com",
        "scopes": ["https://mail.google.com/"], "verification": None, "client_present": True,
    })
    assert next(p for p in out["plugins"] if p["id"] == "gmail")["status"] == "RECONNECT_REQUIRED"


def test_unused_llm_accounts_are_not_plugins_or_attention():
    rows = [_row("openrouter", "account", "OpenRouter", "needs_auth"),
            _row("anthropic", "account", "Anthropic", "needs_auth")]
    out = project_plugins(_inventory(rows), google={})
    assert out["installed_count"] == 0
    assert {p["id"] for p in out["plugins"]} == {c["id"] for c in CATALOG}


def test_calendar_and_drive_need_sign_in_until_scopes_exist():
    rows = [_row("google-workspace", "integration", "Gmail / Google Workspace", "configured")]
    out = project_plugins(_inventory(rows), google={
        "token_present": True, "account": "a@b.com",
        "scopes": ["https://mail.google.com/", "https://www.googleapis.com/auth/gmail.settings.basic"],
        "client_present": True,
    })
    assert next(p for p in out["plugins"] if p["id"] == "google-calendar")["status"] == "NEEDS_SIGN_IN"
    assert next(p for p in out["plugins"] if p["id"] == "google-drive")["status"] == "NEEDS_SIGN_IN"


def test_github_token_configured_is_connected():
    rows = [_row("GITHUB_TOKEN", "integration", "GitHub", "configured")]
    out = project_plugins(_inventory(rows), google={})
    assert next(p for p in out["plugins"] if p["id"] == "github")["status"] == "CONNECTED"


def test_jira_catalog_available_is_not_installed():
    rows = [_row("atlassian", "mcp", "atlassian", "available")]
    out = project_plugins(_inventory(rows), google={})
    assert next(p for p in out["plugins"] if p["id"] == "jira")["status"] == "NOT_INSTALLED"


def test_resolve_gmail_connect_hides_client_json_action():
    plugins = project_plugins(_inventory([
        _row("google-workspace", "integration", "Gmail", "needs_auth")
    ]), google={"client_present": True, "token_present": False})["plugins"]
    mapped = resolve_plugin_action("gmail", "connect", plugins)
    assert mapped == {"kind": "integration", "id": "google-workspace", "scope": "default",
                      "action": "connect"}
    assert "field_id" not in mapped
