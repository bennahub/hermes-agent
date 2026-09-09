"""``reconnect`` must never fabricate success.

The defect these pin: the action decided recovery from LOCAL metadata. A revoked-but-locally-valid
token projects as ``("configured", 1, "credential_present")`` and an expired-but-refreshable one as
``("configured", 1, "credential_refreshable")`` — both were true for every one of the 44 hours the
Anthropic grant was dead, so pressing Reconnect would have answered ``reconnect_verified`` the whole
time, telling the Owner it worked while nothing had changed.

Only the provider can answer "is this fixed?", so these drive the action end to end through the real
HTTP surface with a stubbed provider transport, and assert on the three outcomes it is allowed to
have: verified (a live 2xx, and only then is a fault cleared), rejected (401/403), unverified
(everything else — a failure, not a pass).
"""

import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent import connection_health as ch
from hermes_cli import connections_verify as cv

HOUR_MS = 3600 * 1000


class _FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


class _FakeClient:
    """Stands in for ``httpx.Client`` so no test ever reaches a real provider."""

    def __init__(self, calls, responses, **_kw):
        self._calls, self._responses = calls, responses

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def get(self, url, headers=None, **_kw):
        self._calls.append({"url": url, "headers": dict(headers or {})})
        outcome = self._responses[min(len(self._calls) - 1, len(self._responses) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture()
def surface(tmp_path, monkeypatch):
    """Real router + real inventory + a fake Claude Code credential file and provider transport."""
    import agent.anthropic_credentials as ac
    import httpx

    home = tmp_path / ".hermes"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    creds_path = tmp_path / ".credentials.json"
    monkeypatch.setattr(ac, "claude_code_credentials_path", lambda: creds_path)
    monkeypatch.setattr(ac, "_read_claude_code_credentials_from_keychain", lambda: None)
    # A verify button must never spend a single-use refresh token or start consent.
    def _forbidden(*_a, **_k):
        raise AssertionError("reconnect must not refresh or exchange a token")
    monkeypatch.setattr(ac, "refresh_anthropic_oauth_pure", _forbidden)
    monkeypatch.setattr(ac, "_post_oauth_token", _forbidden)

    calls, responses = [], [_FakeResponse(200, "{}")]
    monkeypatch.setattr(httpx, "Client", lambda **kw: _FakeClient(calls, responses, **kw))

    from hermes_cli.web_routers.connections import router
    from hermes_cli import web_server
    app = FastAPI()
    app.state.auth_required = False
    app.include_router(router)
    client = TestClient(app)

    class _Surface:
        headers = {web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN}
        provider_calls = calls

        @staticmethod
        def credential(access="sk-ant-oat01-synthetic", refresh="sk-ant-ort01-synthetic", expires_at=None):
            creds_path.write_text(json.dumps({"claudeAiOauth": {
                "accessToken": access, "refreshToken": refresh,
                "expiresAt": int(expires_at if expires_at is not None else time.time() * 1000 + HOUR_MS),
            }}), encoding="utf-8")

        @staticmethod
        def provider_answers(*outcomes):
            responses[:] = list(outcomes)

        @staticmethod
        def fault(connection_id="claude-code", reason=ch.REASON_REVOKED):
            block = ch.build_connection_block(provider="anthropic", reason_code=reason).to_dict()
            block["connection_id"] = connection_id
            ch.record_fault(block)

        @staticmethod
        def reconnect(connection_id="claude-code", scope="default"):
            return client.post("/api/connections/actions", headers=_Surface.headers, json={
                "kind": "account", "id": connection_id, "scope": scope, "action": "reconnect"}).json()

        @staticmethod
        def row(connection_id="claude-code", scope="default"):
            from hermes_cli.connections import build_inventory
            from hermes_cli.web_routers.oauth import _build_oauth_catalog
            entries = build_inventory(_build_oauth_catalog())["entries"]
            return next(e for e in entries
                        if e["kind"] == "account" and e["id"] == connection_id and e["scope"] == scope)

    yield _Surface
    client.close()


# ── The incident: a revoked grant still looks perfectly valid on disk ──


def test_reconnect_does_not_fabricate_success_for_a_revoked_but_locally_valid_token(surface):
    """THE regression. Local metadata says ``credential_present``; the provider says 401."""
    surface.credential()  # unexpired, refreshable — exactly what was on disk during the outage
    surface.fault()
    assert surface.row()["state"] == "needs_auth" and "reconnect" in surface.row()["actions"]
    from hermes_cli.connections import singleton_account_state
    assert singleton_account_state("claude-code") == ("configured", 1, "credential_present")

    surface.provider_answers(_FakeResponse(401, "OAuth access token has been revoked."))
    answer = surface.reconnect()

    assert answer["ok"] is False
    assert answer["detail_code"] == "reconnect_rejected"
    assert answer["provider_status"] == 401
    # And the fault stays recorded, so Connections and the runtime keep agreeing it is broken.
    assert ch.observed_fault("claude-code")
    assert surface.row()["state"] == "needs_auth"


def test_reconnect_does_not_fabricate_success_for_an_expired_but_refreshable_token(surface):
    """The other state that held for the whole outage. Unverifiable without spending the
    single-use refresh token, so the honest answer is 'unverified' — never 'verified'."""
    surface.credential(expires_at=(time.time() - 60) * 1000)
    surface.fault()
    from hermes_cli.connections import singleton_account_state
    assert singleton_account_state("claude-code") == ("configured", 1, "credential_refreshable")

    answer = surface.reconnect()

    assert answer["ok"] is False
    assert answer["detail_code"] == "reconnect_unverified"
    assert surface.provider_calls == []  # no refresh, no exchange, no consent
    assert ch.observed_fault("claude-code")


def test_reconnect_never_reports_verified_without_a_provider_round_trip(surface):
    """Belt and braces over every local state the row can project."""
    surface.fault()
    for kwargs in ({}, {"refresh": ""}, {"expires_at": (time.time() - 60) * 1000},
                   {"refresh": "", "expires_at": (time.time() - 60) * 1000}):
        surface.credential(**kwargs)
        surface.provider_answers(_FakeResponse(401))
        assert surface.reconnect()["detail_code"] != "reconnect_verified"
        assert ch.observed_fault("claude-code")


# ── The three honest outcomes ──


def test_reconnect_clears_the_fault_only_on_a_live_provider_success(surface):
    surface.credential()
    surface.fault()
    surface.provider_answers(_FakeResponse(200, "{}"))

    answer = surface.reconnect()

    assert answer["ok"] is True and answer["detail_code"] == "reconnect_verified"
    assert answer["state"] == "connected" and answer["provider_status"] == 200
    assert ch.observed_fault("claude-code") is None
    assert surface.provider_calls and surface.provider_calls[0]["url"].startswith("https://api.anthropic.com/")
    assert surface.provider_calls[0]["headers"]["Authorization"].startswith("Bearer ")
    row = surface.row()
    assert row["state"] == "configured" and "reconnect" not in row["actions"]


def test_reconnect_reports_unverified_when_the_provider_cannot_be_reached(surface):
    """No answer is not a pass."""
    surface.credential()
    surface.fault()
    surface.provider_answers(RuntimeError("connect timeout to api.anthropic.com"))

    answer = surface.reconnect()

    assert answer["ok"] is False and answer["detail_code"] == "reconnect_unverified"
    assert "provider_status" not in answer
    assert ch.observed_fault("claude-code")


def test_reconnect_reports_unverified_for_a_provider_side_error(surface):
    surface.credential()
    surface.fault()
    surface.provider_answers(_FakeResponse(503, "upstream unavailable"))
    answer = surface.reconnect()
    assert answer["ok"] is False and answer["detail_code"] == "reconnect_unverified"
    assert answer["provider_status"] == 503
    assert ch.observed_fault("claude-code")


def test_an_api_key_credential_is_verified_with_the_api_key_header(surface, monkeypatch):
    """The non-OAuth branch: an explicit console key must not be sent as a bearer token."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-synthetic")
    surface.fault("anthropic", reason=ch.REASON_INVALID)
    surface.provider_answers(_FakeResponse(200, "{}"))
    assert surface.reconnect("anthropic")["detail_code"] == "reconnect_verified"
    headers = surface.provider_calls[0]["headers"]
    assert "x-api-key" in headers and "Authorization" not in headers


def test_reconnect_carries_no_provider_prose_or_credential(surface):
    surface.credential(access="sk-ant-oat01-SECRET", refresh="sk-ant-ort01-SECRET")
    surface.fault()
    surface.provider_answers(_FakeResponse(401, "HTTP 401: OAuth access token has been revoked."))
    body = json.dumps(surface.reconnect())
    for marker in ("sk-ant", "SECRET", "OAuth access token", "revoked", "HTTP 401", "Traceback"):
        assert marker not in body, f"{marker!r} leaked into the reconnect response"


def test_reconnect_survives_the_1m_context_beta_rejection(surface):
    """OAuth subscriptions without 1M context answer 400; that must not read as a dead grant."""
    surface.credential()
    surface.fault()
    surface.provider_answers(
        _FakeResponse(400, "long context beta is not yet available for this account"),
        _FakeResponse(200, "{}"))
    assert surface.reconnect()["detail_code"] == "reconnect_verified"
    assert len(surface.provider_calls) == 2


def test_reconnect_is_unavailable_on_a_healthy_row(surface):
    surface.credential()
    assert "reconnect" not in surface.row()["actions"]
    assert surface.reconnect()["ok"] is False  # unsupported_action, not a silent success


# ── The verifier in isolation ──


@pytest.mark.parametrize("status,expected", [
    (200, cv.VERIFIED), (204, cv.VERIFIED), (429, cv.VERIFIED),
    (401, cv.REJECTED), (403, cv.REJECTED),
    (400, cv.UNVERIFIED), (404, cv.UNVERIFIED), (500, cv.UNVERIFIED), (503, cv.UNVERIFIED),
])
def test_status_mapping_never_promotes_an_unknown_status_to_verified(status, expected):
    assert cv._from_status(status) == (expected, status)


def test_no_verifier_for_an_unknown_row_is_unverified_not_verified():
    assert cv.verify_account("some-provider-with-no-probe") == (cv.UNVERIFIED, None)
    assert cv.has_native_verifier("some-provider-with-no-probe") is False


def test_a_verifier_that_raises_is_unverified(monkeypatch):
    monkeypatch.setitem(cv._VERIFIERS, "claude-code",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert cv.verify_account("claude-code") == (cv.UNVERIFIED, None)


def test_resolution_never_refreshes_an_expired_credential(surface):
    """``resolve_anthropic_token`` would POST here and spend the refresh token; this must not."""
    surface.credential(expires_at=(time.time() - 60) * 1000)
    assert cv._resolve_token_without_refresh() == ("", "expired")
    assert cv.verify_anthropic_grant() == (cv.UNVERIFIED, None)
    assert surface.provider_calls == []


def test_an_absent_credential_is_rejected_not_verified(surface):
    assert cv._resolve_token_without_refresh() == ("", "absent")
    assert cv.verify_anthropic_grant() == (cv.REJECTED, None)
