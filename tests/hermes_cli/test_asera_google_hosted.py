"""Hosted Google OAuth: hosted redirect, state bind, no token leak."""
import json
from pathlib import Path

import pytest

from hermes_cli.asera_google_hosted import (
    REDIRECT_URI,
    authorization_url,
    begin,
    complete,
    consume_completion,
)


CLIENT = {
    "web": {
        "client_id": "test.apps.googleusercontent.com",
        "client_secret": "synthetic-only",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
}


@pytest.fixture
def hosted(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    secrets = tmp_path / ".secrets" / "google"
    secrets.mkdir(parents=True)
    (secrets / "web_client_secret.json").write_text(json.dumps(CLIENT))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("hermes_constants.get_default_hermes_root", lambda: home)
    return home, secrets


def test_start_url_uses_hosted_redirect_not_localhost(hosted):
    started = begin(plugin="gmail", account="google_account_1", owner_id="owner")
    assert started["verification_uri"].startswith("https://auth.asera.dev/oauth/google/start?t=")
    assert "localhost" not in started["verification_uri"]
    tx = started["session_id"]
    url = authorization_url(tx)
    assert url.startswith("https://accounts.google.com/")
    assert "redirect_uri=https%3A%2F%2Fauth.asera.dev%2Foauth%2Fgoogle%2Fcallback" in url
    assert "localhost" not in url
    assert "code_challenge=" in url


def test_callback_rejects_invalid_state_and_does_not_write_tokens(hosted):
    home, secrets = hosted
    begin(plugin="gmail", account="google_account_1", owner_id="owner")
    with pytest.raises(ValueError, match="invalid_google_callback_state|google_operation_not_found"):
        complete("https://auth.asera.dev/oauth/google/callback?code=x&state=wrong")
    with pytest.raises(ValueError, match="invalid_google_callback"):
        complete("http://localhost:1/?code=x&state=wrong")
    assert not (home / "google_token.json").exists()
    assert not (secrets / "accounts" / "google_account_1" / "token.json").exists()


def test_completion_is_one_time_and_carries_no_token(hosted, monkeypatch):
    started = begin(plugin="gmail", account="google_account_2", owner_id="owner")
    store = json.loads((hosted[0] / "google_oauth_hosted.json").read_text())
    tx = store["transactions"][started["session_id"]]
    monkeypatch.setattr("hermes_cli.asera_google_hosted._exchange",
                        lambda code, record: {"refresh_token": "synthetic", "scopes": ["https://mail.google.com/"]})
    finish = complete(
        f"https://auth.asera.dev/oauth/google/callback?code=synthetic-code&state={tx['state']}")
    assert finish.startswith("https://asera.dev/oauth/complete?c=")
    assert "refresh" not in finish and "token" not in finish.lower()
    token = finish.rsplit("c=", 1)[-1]
    first = consume_completion(token)
    assert first["account"] == "google_account_2"
    with pytest.raises(ValueError):
        consume_completion(token)
    assert (hosted[1] / "accounts" / "google_account_2" / "token.json").is_file()
    assert not (hosted[0] / "google_token.json").exists()
