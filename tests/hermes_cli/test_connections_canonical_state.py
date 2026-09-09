"""One canonical Connection state: the Connections projection and the agent runtime must agree.

Before this, ``build_inventory`` projected accounts purely from ``auth.json``'s ``credential_pool``.
The borrowed Claude Code grant the runtime actually uses lives in a singleton file that is not in
that pool, so its row was a permanently static zero-action ``external_gate`` — it read exactly the
same whether the grant was healthy, expired, or revoked 44 hours earlier.
"""

import json
import time

import pytest

from agent import connection_health as ch
from hermes_cli import connections as conn


HOUR_MS = 3600 * 1000


@pytest.fixture()
def creds(monkeypatch, tmp_path):
    """Drive ``singleton_account_state`` off a fake Claude Code credential file."""
    import agent.anthropic_credentials as ac
    path = tmp_path / ".credentials.json"
    monkeypatch.setattr(ac, "claude_code_credentials_path", lambda: path)
    monkeypatch.setattr(ac, "_read_claude_code_credentials_from_keychain", lambda: None)
    monkeypatch.setattr(ch, "health_store_path", lambda: tmp_path / "state" / "connection_health.json")

    def write(access="a-token", refresh="r-token", expires_at=None):
        payload = {"claudeAiOauth": {"accessToken": access, "refreshToken": refresh,
                                     "expiresAt": int(expires_at if expires_at is not None
                                                      else (time.time() * 1000) + HOUR_MS)}}
        path.write_text(json.dumps(payload), encoding="utf-8")
    return types_ns(write=write, path=path)


def types_ns(**kw):
    import types
    return types.SimpleNamespace(**kw)


# ── The singleton row now reports live truth ──


def test_healthy_grant_reports_configured(creds):
    creds.write()
    assert conn.singleton_account_state("claude-code") == ("configured", 1, "credential_present")


def test_expired_but_refreshable_stays_configured(creds):
    """Hermes can recover this one on its own — it is not an Owner problem."""
    creds.write(expires_at=(time.time() - 60) * 1000)
    state, count, detail = conn.singleton_account_state("claude-code")
    assert (state, count, detail) == ("configured", 1, "credential_refreshable")


def test_expired_without_refresh_needs_the_owner(creds):
    creds.write(refresh="", expires_at=(time.time() - 60) * 1000)
    assert conn.singleton_account_state("claude-code") == ("expired", 1, "reauthorization_required")


def test_absent_credentials_report_sign_in_required(creds):
    assert conn.singleton_account_state("claude-code") == ("needs_auth", 0, "sign_in_required")


def test_spent_rotation_needs_reauthorization(creds, monkeypatch):
    import agent.anthropic_credentials as ac
    creds.write()
    monkeypatch.setattr(ac, "is_rotation_consumed_uncommitted", lambda *a, **k: True)
    assert conn.singleton_account_state("claude-code") == ("needs_auth", 1, "reauthorization_required")


def test_other_identifiers_are_untouched(creds):
    """Only singleton-backed rows change; every other account keeps its pool projection."""
    assert conn.singleton_account_state("anthropic") is None
    assert conn.singleton_account_state("openai-codex") is None


# ── The join: the runtime's observed verdict wins over local metadata ──


def test_revoked_grant_overrides_a_healthy_looking_file(creds):
    """A revoked token still LOOKS valid on disk. Only the runtime knows — so the runtime's
    verdict must reach the row, or Connections keeps saying Connected while every call 401s."""
    creds.write()
    assert conn.singleton_account_state("claude-code")[0] == "configured"
    ch.record_fault(ch.build_connection_block(
        provider="anthropic", reason_code=ch.REASON_REVOKED).to_dict() | {"connection_id": "claude-code"})
    entry = conn.apply_observed_fault(conn.row("claude-code", "account", "Claude subscription", "configured"))
    assert entry["state"] == "needs_auth"
    assert entry["detail_code"] == ch.REASON_REVOKED
    assert entry["owner_action"] == ch.ACTION_REAUTHORIZE
    assert "reconnect" in entry["actions"]


def test_a_configure_fault_asks_for_configuration_not_consent(creds):
    ch.record_fault(ch.build_connection_block(
        provider="anthropic", reason_code=ch.REASON_MISSING).to_dict() | {"connection_id": "anthropic"})
    entry = conn.apply_observed_fault(conn.row("anthropic", "account", "Anthropic API Key", "needs_auth"))
    assert entry["state"] == "needs_configuration"
    assert entry["owner_action"] == ch.ACTION_CONFIGURE


def test_healthy_row_is_left_alone(creds):
    entry = conn.apply_observed_fault(conn.row("claude-code", "account", "Claude subscription", "configured"))
    assert entry["state"] == "configured"
    assert "reconnect" not in entry["actions"]


def test_cleared_fault_restores_the_row(creds):
    ch.record_fault(ch.build_connection_block(
        provider="anthropic", reason_code=ch.REASON_REVOKED).to_dict() | {"connection_id": "claude-code"})
    ch.record_healthy("claude-code")
    entry = conn.apply_observed_fault(conn.row("claude-code", "account", "Claude subscription", "configured"))
    assert entry["state"] == "configured" and "reconnect" not in entry["actions"]


def test_the_row_never_carries_a_credential_value(creds):
    creds.write(access="sk-ant-oat01-SECRET", refresh="sk-ant-ort01-SECRET")
    ch.record_fault(ch.build_connection_block(
        provider="anthropic", reason_code=ch.REASON_REVOKED).to_dict() | {"connection_id": "claude-code"})
    entry = conn.apply_observed_fault(conn.row("claude-code", "account", "Claude subscription", "configured"))
    assert "SECRET" not in json.dumps(entry) and "sk-ant" not in json.dumps(entry)


def test_overlay_is_fail_soft(monkeypatch):
    monkeypatch.setattr(ch, "observed_fault", lambda *_a: (_ for _ in ()).throw(RuntimeError("boom")))
    entry = conn.apply_observed_fault(conn.row("claude-code", "account", "Claude subscription", "configured"))
    assert entry["state"] == "configured"


def test_singleton_state_is_fail_soft(monkeypatch):
    import agent.anthropic_credentials as ac
    monkeypatch.setattr(ac, "read_claude_code_credentials", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert conn.singleton_account_state("claude-code") is None


def test_the_row_never_publishes_what_local_metadata_believed(creds):
    """A faulted row reports the runtime's verdict and nothing else.

    It used to also carry ``underlying_state`` — the pre-overlay state — and ``reconnect`` decided
    recovery from it. For a revoked grant that value is ``configured``, so every consumer that trusts
    it turns a dead connection into a fabricated success. There is no honest use for it on the wire.
    """
    ch.record_fault(ch.build_connection_block(
        provider="anthropic", reason_code=ch.REASON_INVALID).to_dict() | {"connection_id": "anthropic"})
    entry = conn.apply_observed_fault(conn.row("anthropic", "account", "Anthropic API Key", "configured"))
    assert entry["state"] == "needs_configuration"
    assert "underlying_state" not in entry


# ── The join across scopes: the row the Connections screen renders must carry a
#    fault observed by ANY profile sharing that credential ──


def test_the_default_row_reports_a_fault_observed_by_a_named_profile(monkeypatch, tmp_path):
    """End to end at the projection level: `badr`'s runtime observed it, `build_inventory` must
    render it. This is the pairing the wave's contract forbids diverging."""
    import agent.anthropic_credentials as ac
    from hermes_cli.connections import build_inventory
    from hermes_cli.web_routers.oauth import _build_oauth_catalog
    home = tmp_path / ".hermes"
    (home / "profiles" / "badr").mkdir(parents=True)
    (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    (home / "profiles" / "badr" / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(ac, "claude_code_credentials_path", lambda: tmp_path / ".credentials.json")
    monkeypatch.setattr(ac, "_read_claude_code_credentials_from_keychain", lambda: None)
    (tmp_path / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-synthetic", "refreshToken": "sk-ant-ort01-synthetic",
        "expiresAt": int(time.time() * 1000 + HOUR_MS)}}), encoding="utf-8")

    # The runtime ran as profile `badr` and observed the grant revoked.
    monkeypatch.setenv("HERMES_HOME", str(home / "profiles" / "badr"))
    ch.record_fault(ch.build_connection_block(
        provider="anthropic", reason_code=ch.REASON_REVOKED).to_dict() | {"connection_id": "claude-code"})

    # The Connections screen runs at the root.
    monkeypatch.setenv("HERMES_HOME", str(home))
    row = next(e for e in build_inventory(_build_oauth_catalog())["entries"]
               if e["kind"] == "account" and e["id"] == "claude-code" and e["scope"] == "default")
    assert row["state"] == "needs_auth"          # not "configured" — the local file still looks fine
    assert row["detail_code"] == ch.REASON_REVOKED
    assert "reconnect" in row["actions"]
