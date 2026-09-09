"""Hosted OAuth HTTP surface: public start/callback, no tokens in redirects."""
import json

import pytest
from fastapi.testclient import TestClient

from hermes_cli.asera_google_hosted import begin
from hermes_cli.web_server import app


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
    return home


def test_health_and_callback_reject_wrong_host(hosted):
    wrong = TestClient(app, base_url="https://asera.dev")
    assert wrong.get("/oauth/health").status_code == 404
    assert wrong.get("/oauth/google/callback").status_code == 404


def test_start_builds_hosted_google_url_not_localhost(hosted):
    started = begin(plugin="gmail", account="google_account_2", owner_id="owner")
    client = TestClient(app, base_url="https://auth.asera.dev")
    response = client.get(f"/oauth/google/start?t={started['session_id']}", follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://accounts.google.com/")
    assert "redirect_uri=https%3A%2F%2Fauth.asera.dev%2Foauth%2Fgoogle%2Fcallback" in location
    assert "localhost" not in location
    assert "code_challenge=" in location


def test_callback_missing_state_does_not_write_tokens(hosted):
    begin(plugin="gmail", account="google_account_1", owner_id="owner")
    client = TestClient(app, base_url="https://auth.asera.dev")
    response = client.get("/oauth/google/callback?code=x", follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://asera.dev/oauth/complete")
    assert "error=" in location
    assert "refresh" not in location.lower()
    assert "token" not in location.lower()
    assert not (hosted / "google_token.json").exists()
