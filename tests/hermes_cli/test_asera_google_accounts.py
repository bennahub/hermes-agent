"""Named Google accounts stay isolated; aliases cannot be paths."""
import json
from pathlib import Path

from hermes_cli.asera_google_accounts import (
    badge_for,
    load_google_accounts,
    next_alias,
    revoke_named_account,
    scopes_cover,
    validate_alias,
)


def test_badge_and_scope_aliases():
    assert badge_for("i.aalmuzaini@gmail.com") == "default"
    assert badge_for("abdulrahman@bennahub.com") == "bennahub"
    assert badge_for("abdulrahman@mraia.com.sa") == "mraia"
    assert scopes_cover(["https://www.googleapis.com/auth/calendar"],
                        ["https://www.googleapis.com/auth/calendar.readonly"])
    assert scopes_cover(["https://www.googleapis.com/auth/gmail.modify"], [], service="gmail")
    assert not scopes_cover(["https://mail.google.com/"],
                            ["https://www.googleapis.com/auth/calendar.readonly"])


def test_alias_rejects_paths():
    try:
        validate_alias("../google_token.json")
    except ValueError as exc:
        assert str(exc) == "invalid_google_request"
    else:
        raise AssertionError("path must be rejected")
    assert validate_alias("google_account_3") == "google_account_3"


def test_load_accounts_dedupes_primary_and_keeps_revoked_siblings(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    secrets = tmp_path / ".secrets" / "google"
    (secrets / "accounts" / "google_account_1").mkdir(parents=True)
    (secrets / "accounts" / "google_account_2").mkdir(parents=True)
    (secrets / "ACL.json").write_text(json.dumps({
        "accounts": {
            "google_account_1": {"email": "owner@gmail.com", "role": "GENERAL_PRIMARY"},
            "google_account_2": {"email": "owner@mraia.com.sa", "role": "MRAIA_OFFICIAL"},
        }
    }))
    token = {"refresh_token": "shared-refresh", "scopes": ["https://mail.google.com/"],
             "account": "owner@gmail.com"}
    (home / "google_token.json").write_text(json.dumps(token))
    (secrets / "accounts" / "google_account_1" / "token.json").write_text(json.dumps(token))
    (secrets / "accounts" / "google_account_1" / "identity.json").write_text(
        json.dumps({"email": "owner@gmail.com"}))
    (secrets / "accounts" / "google_account_2" / "token.json").write_text(json.dumps({
        "refresh_token": "other-refresh",
        "scopes": ["https://www.googleapis.com/auth/calendar"],
        "account": "owner@mraia.com.sa",
    }))
    (secrets / "accounts" / "google_account_2" / "identity.json").write_text(
        json.dumps({"email": "owner@mraia.com.sa"}))
    monkeypatch.setattr("hermes_cli.asera_google_accounts.default_secrets_root", lambda: secrets)
    monkeypatch.setattr(
        "hermes_cli.asera_google_accounts._home_token",
        lambda home_path: (home / "google_token.json", json.loads((home / "google_token.json").read_text())),
    )
    rows = load_google_accounts(home, secrets_root=secrets, probe=False)
    assert [row["display"] for row in rows] == ["owner@gmail.com", "owner@mraia.com.sa"]
    assert rows[0]["primary"] is True
    assert next_alias(secrets) == "google_account_3"


def test_revoke_named_account_leaves_the_other_token(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    secrets = tmp_path / ".secrets" / "google"
    (secrets / "accounts" / "google_account_2").mkdir(parents=True)
    (secrets / "ACL.json").write_text(json.dumps({"accounts": {
        "google_account_1": {"email": "a@gmail.com"},
        "google_account_2": {"email": "b@mraia.com.sa"},
    }}))
    (home / "google_token.json").write_text(json.dumps({
        "refresh_token": "keep-me", "scopes": ["https://mail.google.com/"]
    }))
    other = secrets / "accounts" / "google_account_2" / "token.json"
    other.write_text(json.dumps({"refresh_token": "drop-me", "token": "access"}))
    monkeypatch.setattr("hermes_cli.asera_google_accounts._home_token",
                        lambda home_path: (home / "google_token.json",
                                           json.loads((home / "google_token.json").read_text())))
    monkeypatch.setattr("hermes_cli.asera_google_accounts._revoke_file", Path.unlink)
    assert revoke_named_account("google_account_2", home=home, secrets_root=secrets) == "named"
    assert (home / "google_token.json").is_file()
    assert not other.exists()
