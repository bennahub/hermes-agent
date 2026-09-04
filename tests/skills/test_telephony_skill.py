from __future__ import annotations

import importlib.util
import json
import sys
import stat
from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "optional-skills"
    / "productivity"
    / "telephony"
    / "scripts"
    / "telephony.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("telephony_skill", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_save_twilio_writes_env_and_state(tmp_path: Path, monkeypatch):
    mod = load_module()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    result = mod.save_twilio(
        "AC123",
        "secret-token",
        phone_number="+1 (702) 555-1234",
        phone_sid="PN123",
    )

    env_text = (tmp_path / ".hermes" / ".env").read_text(encoding="utf-8")
    state = json.loads((tmp_path / ".hermes" / "telephony_state.json").read_text(encoding="utf-8"))

    assert result["success"] is True
    assert "TWILIO_ACCOUNT_SID=AC123" in env_text
    assert "TWILIO_AUTH_TOKEN=secret-token" in env_text
    assert "TWILIO_PHONE_NUMBER=+17025551234" in env_text
    assert "TWILIO_PHONE_NUMBER_SID=PN123" in env_text
    assert state["twilio"]["default_phone_number"] == "+17025551234"
    assert state["twilio"]["default_phone_sid"] == "PN123"


def test_upsert_env_updates_existing_values(tmp_path: Path):
    mod = load_module()
    env_path = tmp_path / ".env"
    env_path.write_text("TWILIO_PHONE_NUMBER=+15550000000\nOTHER=keep\n", encoding="utf-8")

    mod._upsert_env_file(
        {
            "TWILIO_PHONE_NUMBER": "+15551112222",
            "TWILIO_PHONE_NUMBER_SID": "PN999",
        },
        env_path=env_path,
    )

    env_text = env_path.read_text(encoding="utf-8")
    assert "TWILIO_PHONE_NUMBER=+15551112222" in env_text
    assert "TWILIO_PHONE_NUMBER_SID=PN999" in env_text
    assert "OTHER=keep" in env_text




def test_twilio_buy_number_saves_env_and_state(tmp_path: Path):
    mod = load_module()
    state_path = tmp_path / "telephony_state.json"
    env_path = tmp_path / ".env"

    mod._twilio_request = lambda method, path, params=None, form=None: {
        "sid": "PN111",
        "phone_number": "+17025550123",
        "friendly_name": "Test Number",
        "capabilities": {"voice": True, "sms": True},
    }

    result = mod._twilio_buy_number(
        "+17025550123",
        save_env=True,
        state_path=state_path,
        env_path=env_path,
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    env_text = env_path.read_text(encoding="utf-8")

    assert result["phone_sid"] == "PN111"
    assert state["twilio"]["default_phone_number"] == "+17025550123"
    assert state["twilio"]["default_phone_sid"] == "PN111"
    assert "TWILIO_PHONE_NUMBER=+17025550123" in env_text
    assert "TWILIO_PHONE_NUMBER_SID=PN111" in env_text






def test_diagnose_includes_decision_tree_and_saved_state(tmp_path: Path, monkeypatch):
    mod = load_module()
    hermes_home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    mod._save_state(
        {
            "version": 1,
            "twilio": {
                "default_phone_number": "+17025550123",
                "last_inbound_message_sid": "SM123",
            },
            "vapi": {
                "phone_number_id": "vapi-abc",
            },
        },
        hermes_home / "telephony_state.json",
    )
    (hermes_home / ".env").parent.mkdir(parents=True, exist_ok=True)
    (hermes_home / ".env").write_text(
        "TWILIO_ACCOUNT_SID=AC123\nTWILIO_AUTH_TOKEN=token\nBLAND_API_KEY=bland\n",
        encoding="utf-8",
    )

    result = mod.diagnose()

    assert result["providers"]["twilio"]["default_phone_number"] == "+17025550123"
    assert result["providers"]["twilio"]["last_inbound_message_sid"] == "SM123"
    assert result["providers"]["bland"]["configured"] is True
    assert result["providers"]["vapi"]["phone_number_id"] == "vapi-abc"
    assert any(item["use"] == "Twilio" for item in result["decision_tree"])
    assert result["providers"]["wave"]["configured"] is False
    assert result["providers"]["wave"]["external_gate"] is True
    assert any("Wave" in item["use"] for item in result["decision_tree"])


def test_save_wave_and_dry_run_call_never_posts(tmp_path: Path, monkeypatch):
    mod = load_module()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    saved = mod.save_wave("sk_sandbox_testkey", from_number="0551234567")
    env_text = (tmp_path / ".hermes" / ".env").read_text(encoding="utf-8")

    assert saved["success"] is True
    assert saved["external_gate"] is True
    assert "WAVE_API_KEY=sk_sandbox_testkey" in env_text
    assert "WAVE_FROM_NUMBER=+966551234567" in env_text

    called = []

    def _fail_http(*args, **kwargs):
        called.append((args, kwargs))
        raise AssertionError("dry-run must not HTTP")

    mod._json_request = _fail_http
    preview = mod.wave_call("0512345678", confirm=False)
    assert preview["dry_run"] is True
    assert preview["external_gate"] is True
    assert preview["to_phone_number_masked"].endswith("5678")
    assert called == []

    try:
        mod.wave_call("+966501234567", confirm=True)
        raise AssertionError("sandbox key must not place a live call")
    except mod.TelephonyError as exc:
        assert "EXTERNAL_GATE" in str(exc) or "production key" in str(exc)


def test_normalize_sa_phone_rejects_non_saudi():
    mod = load_module()
    assert mod._normalize_sa_phone("0501234567") == "+966501234567"
    try:
        mod._normalize_sa_phone("+15551234567")
        raise AssertionError("US number must not pass Wave validation")
    except mod.TelephonyError:
        pass


@pytest.mark.macos_only
def test_saved_credentials_are_private_from_creation(tmp_path, monkeypatch):
    mod = load_module()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mod.save_wave("sk_live_fake", from_number="0501234567")
    assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600


def test_wave_live_key_without_number_is_a_configuration_gate(tmp_path, monkeypatch):
    mod = load_module()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("WAVE_API_KEY", "sk_live_fake")
    monkeypatch.delenv("WAVE_FROM_NUMBER", raising=False)
    assert mod.diagnose()["providers"]["wave"]["external_gate"] is True
    with pytest.raises(mod.TelephonyError, match="from-number"):
        mod.wave_call("0501234567", confirm=True)


def test_wave_validates_caller_and_never_echoes_provider_phone(tmp_path, monkeypatch):
    mod = load_module()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("WAVE_API_KEY", "sk_live_fake")
    calls = []
    def request(*args, **kwargs):
        calls.append((args, kwargs))
        return {"call_id": "fixture-call", "status": "initiated", "to": "+966501234567"}
    mod._json_request = request
    with pytest.raises(mod.TelephonyError):
        mod.wave_call("0501234567", confirm=True, caller_id_number="+15551234567")
    assert calls == []
    result = mod.wave_call("0501234567", confirm=True, caller_id_number="0551234567")
    assert calls[0][1]["json_body"]["caller_id_number"] == "+966551234567"
    assert "+966501234567" not in json.dumps(result)
    assert result["call_id"] == "fixture-call"


def test_wave_logs_only_returns_operational_fields(tmp_path, monkeypatch):
    mod = load_module()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("WAVE_API_KEY", "sk_live_fake")
    mod._json_request = lambda *a, **kw: {"data": [{"id": "call-1", "status": "ended", "to": "+966501234567", "from": "+966551234567", "duration_seconds": 42, "metadata": {"secret": "private-fixture"}, "recording_url": "https://example.test/private"}]}
    result = mod.wave_logs()
    encoded = json.dumps(result)
    assert "private-fixture" not in encoded
    assert "+966501234567" not in encoded
    assert "+966551234567" not in encoded
    assert result["calls"][0]["duration_seconds"] == 42


def test_wave_provider_errors_do_not_echo_credentials(tmp_path, monkeypatch):
    mod = load_module()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("WAVE_API_KEY", "sk_live_fake")
    def request(*a, **kw):
        raise mod.TelephonyError("HTTP 403: {'error_code': 'PRODUCTION_TIER_REQUIRED', 'message': 'sk_live_private +966501234567'}")
    mod._json_request = request
    with pytest.raises(mod.TelephonyError) as caught:
        mod.wave_logs()
    assert "sk_live_private" not in str(caught.value)
    assert "+966501234567" not in str(caught.value)


def test_save_wave_accepts_hidden_prompt_without_argv_secret(tmp_path, monkeypatch):
    mod = load_module()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(mod.getpass, "getpass", lambda prompt: "sk_sandbox_fake")
    result = mod._dispatch(mod._build_parser().parse_args(["save-wave"]))
    assert result["success"] is True
    assert "sk_sandbox_fake" not in json.dumps(result)
