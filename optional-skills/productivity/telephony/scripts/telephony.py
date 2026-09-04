#!/usr/bin/env python3
"""Telephony helper for the Hermes optional telephony skill.

Capabilities:
- Persist telephony provider credentials to the Hermes .env file ($HERMES_HOME/.env)
- Search for, buy, and remember Twilio phone numbers
- Make direct Twilio calls (TwiML <Say> or <Play>)
- Send SMS / MMS via Twilio
- Poll inbound SMS for an owned Twilio number using only this script + state
- Import a Twilio number into Vapi and persist the returned Vapi phone_number_id
- Make outbound AI voice calls via Bland.ai or Vapi

This file intentionally uses Python stdlib HTTP clients so the skill can run in a
minimal environment with no extra pip installs.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import escape as xml_escape
from pathlib import Path
from typing import Any

TWILIO_API_BASE = "https://api.twilio.com/2010-04-01/Accounts"
VAPI_API_BASE = "https://api.vapi.ai"
BLAND_API_BASE = "https://api.bland.ai/v1"
WAVE_API_BASE = "https://api.wave.sa"
# Wave production destinations: Saudi geographic / mobile / toll-free / 9200.
WAVE_SA_NUMBER_RE = re.compile(r"^(\+966|0)(1[1-9]\d{7}|5\d{8}|800\d{7}|9200\d{6})$")

BLAND_DEFAULT_VOICE = "mason"
BLAND_DEFAULT_MODEL = "enhanced"
BLAND_VOICES = {
    "mason": "Male, natural, friendly (recommended)",
    "josh": "Male, conversational",
    "ryan": "Male, professional",
    "matt": "Male, casual",
    "evelyn": "Female, natural, warm (recommended)",
    "tina": "Female, warm, friendly",
    "june": "Female, conversational",
}

VAPI_DEFAULT_VOICE_PROVIDER = "11labs"
VAPI_DEFAULT_VOICE_ID = "cjVigY5qzO86Huf0OWal"  # ElevenLabs "Eric"
VAPI_DEFAULT_MODEL = "gpt-4o"
TWILIO_DEFAULT_TTS_VOICE = "Polly.Joanna"
DEFAULT_AI_PROVIDER = "bland"
STATE_VERSION = 1


class TelephonyError(RuntimeError):
    """Domain-specific failure surfaced to the skill/user."""


@dataclass
class OwnedTwilioNumber:
    sid: str
    phone_number: str
    friendly_name: str
    capabilities: dict[str, Any]


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


def _env_path() -> Path:
    return _hermes_home() / ".env"


def _config_path() -> Path:
    return _hermes_home() / "config.yaml"


def _state_path() -> Path:
    return _hermes_home() / "telephony_state.json"


def _load_root_config() -> dict[str, Any]:
    path = _config_path()
    if not path.exists():
        return {}
    try:
        import yaml  # optional dependency; Hermes already ships PyYAML
    except Exception:
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _config_lookup(*paths: tuple[str, ...], default: str = "") -> str:
    root = _load_root_config()
    for path in paths:
        node: Any = root
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if node not in {None, ""} and not isinstance(node, dict):
            return str(node)
    return default


def _load_dotenv_values(path: Path | None = None) -> dict[str, str]:
    env_file = path or _env_path()
    if not env_file.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = raw_line.partition("=")
        key = key.strip()
        value = value.strip()
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            value = value[1:-1].replace('\\"', '"').replace('\\\\', '\\')
        values[key] = value
    return values


def _env_or_config(env_key: str, *config_paths: tuple[str, ...], default: str = "") -> str:
    value = os.environ.get(env_key, "")
    if value:
        return value
    dotenv_value = _load_dotenv_values().get(env_key, "")
    if dotenv_value:
        return dotenv_value
    return _config_lookup(*config_paths, default=default)


def _load_state(path: Path | None = None) -> dict[str, Any]:
    state_file = path or _state_path()
    if not state_file.exists():
        return {"version": STATE_VERSION}
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("version", STATE_VERSION)
            return data
    except Exception:
        pass
    return {"version": STATE_VERSION}


def _save_state(state: dict[str, Any], path: Path | None = None) -> Path:
    state_file = path or _state_path()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return state_file


def _quote_env_value(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise TelephonyError("Credential values must be a single line")
    if re.fullmatch(r"[A-Za-z0-9_./:+@-]+", value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _upsert_env_file(updates: dict[str, str], env_path: Path | None = None) -> Path:
    path = env_path or _env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    seen: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            new_lines.append(line)
            continue
        key, _, _rest = line.partition("=")
        key = key.strip()
        if key in updates:
            new_lines.append(f"{key}={_quote_env_value(str(updates[key]))}")
            seen.add(key)
        else:
            new_lines.append(line)

    if new_lines and new_lines[-1].strip():
        new_lines.append("")
    for key, value in updates.items():
        if key not in seen:
            new_lines.append(f"{key}={_quote_env_value(str(value))}")

    # mkstemp creates mode 0600 before any credential bytes are written.
    # Replace atomically so a failed write cannot truncate the existing .env.
    fd, temporary = tempfile.mkstemp(prefix=".telephony-env-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(new_lines).rstrip() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def _normalize_phone(number: str) -> str:
    if not number:
        raise TelephonyError("Phone number is required")
    trimmed = number.strip()
    if not trimmed.startswith("+"):
        raise TelephonyError(
            f"Phone number must be E.164 format (for example +15551234567), got: {number}"
        )
    digits = "+" + re.sub(r"\D", "", trimmed)
    if len(digits) < 8:
        raise TelephonyError(f"Phone number looks too short: {number}")
    return digits


def _normalize_sa_phone(number: str) -> str:
    """Normalize a Saudi number to E.164 for Wave ``POST /v1/calls``.

    Accepts ``+9665XXXXXXXX``, ``05XXXXXXXX``, or a 9-digit mobile ``5XXXXXXXX``.
    """
    if not number or not str(number).strip():
        raise TelephonyError("Saudi phone number is required")
    digits = re.sub(r"\D", "", str(number))
    if digits.startswith("966"):
        e164 = "+" + digits
        national = "0" + digits[3:]
    elif digits.startswith("0"):
        e164 = "+966" + digits[1:]
        national = digits
    else:
        e164 = "+966" + digits
        national = "0" + digits
    if WAVE_SA_NUMBER_RE.fullmatch(e164) or WAVE_SA_NUMBER_RE.fullmatch(national):
        return e164
    raise TelephonyError(
        "Wave destinations must be a Saudi number (+9665XXXXXXXX or 05XXXXXXXX). "
        "The supplied number is invalid."
    )


def _mask_phone(number: str) -> str:
    digits = re.sub(r"\D", "", number or "")
    if len(digits) < 4:
        return "***"
    return f"***-***-{digits[-4:]}"


def _parse_twilio_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _json_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    form: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if params:
        query = urllib.parse.urlencode(params, doseq=True)
        url = f"{url}?{query}"

    request_headers = dict(headers or {})
    body: bytes | None = None
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    elif form is not None:
        body = urllib.parse.urlencode(form, doseq=True).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

    req = urllib.request.Request(url, data=body, headers=request_headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        try:
            parsed = json.loads(body_text) if body_text else {}
        except Exception:
            parsed = {"raw": body_text}
        raise TelephonyError(f"HTTP {exc.code} from {url}: {parsed or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise TelephonyError(f"Connection error for {url}: {exc.reason}") from exc


def _twilio_creds() -> tuple[str, str]:
    sid = _env_or_config(
        "TWILIO_ACCOUNT_SID",
        ("telephony", "twilio", "account_sid"),
        ("phone", "twilio", "account_sid"),
    )
    token = _env_or_config(
        "TWILIO_AUTH_TOKEN",
        ("telephony", "twilio", "auth_token"),
        ("phone", "twilio", "auth_token"),
    )
    if not sid or not token:
        raise TelephonyError(
            "Twilio credentials are not configured. Use 'save-twilio' or set "
            f"TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN in {_env_path()}."
        )
    return sid, token


def _twilio_basic_headers() -> dict[str, str]:
    sid, token = _twilio_creds()
    auth = base64.b64encode(f"{sid}:{token}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {auth}"}


def _twilio_request(method: str, path: str, *, params=None, form=None) -> dict[str, Any]:
    sid, _token = _twilio_creds()
    return _json_request(
        method,
        f"{TWILIO_API_BASE}/{sid}/{path.lstrip('/')}",
        headers=_twilio_basic_headers(),
        params=params,
        form=form,
    )


def _twilio_owned_numbers(limit: int = 50) -> list[OwnedTwilioNumber]:
    payload = _twilio_request("GET", "IncomingPhoneNumbers.json", params={"PageSize": limit})
    items = payload.get("incoming_phone_numbers", []) or []
    results: list[OwnedTwilioNumber] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        caps = item.get("capabilities") if isinstance(item.get("capabilities"), dict) else {}
        results.append(
            OwnedTwilioNumber(
                sid=str(item.get("sid", "")),
                phone_number=str(item.get("phone_number", "")),
                friendly_name=str(item.get("friendly_name", "")),
                capabilities=caps,
            )
        )
    return results


def _remember_twilio_number(
    *,
    phone_number: str,
    phone_sid: str = "",
    save_env: bool = False,
    state_path: Path | None = None,
    env_path: Path | None = None,
) -> dict[str, Any]:
    state = _load_state(state_path)
    twilio_state = state.setdefault("twilio", {})
    twilio_state["default_phone_number"] = phone_number
    if phone_sid:
        twilio_state["default_phone_sid"] = phone_sid
    _save_state(state, state_path)

    saved_env_keys: list[str] = []
    if save_env:
        updates = {"TWILIO_PHONE_NUMBER": phone_number}
        if phone_sid:
            updates["TWILIO_PHONE_NUMBER_SID"] = phone_sid
        _upsert_env_file(updates, env_path)
        saved_env_keys = sorted(updates)

    return {
        "state_path": str(state_path or _state_path()),
        "saved_env_keys": saved_env_keys,
    }


def _remember_vapi_number(
    *,
    phone_number_id: str,
    save_env: bool = False,
    state_path: Path | None = None,
    env_path: Path | None = None,
) -> dict[str, Any]:
    state = _load_state(state_path)
    vapi_state = state.setdefault("vapi", {})
    vapi_state["phone_number_id"] = phone_number_id
    _save_state(state, state_path)

    saved_env_keys: list[str] = []
    if save_env:
        _upsert_env_file({"VAPI_PHONE_NUMBER_ID": phone_number_id}, env_path)
        saved_env_keys = ["VAPI_PHONE_NUMBER_ID"]

    return {
        "state_path": str(state_path or _state_path()),
        "saved_env_keys": saved_env_keys,
    }


def _resolve_twilio_number(identifier: str | None = None) -> OwnedTwilioNumber:
    if identifier:
        wanted = identifier.strip()
        normalized = None
        if wanted.startswith("+"):
            normalized = _normalize_phone(wanted)
        for item in _twilio_owned_numbers(limit=100):
            if item.sid == wanted or item.phone_number == normalized:
                return item
        raise TelephonyError(f"Could not find an owned Twilio number matching {identifier}")

    env_number = _env_or_config(
        "TWILIO_PHONE_NUMBER",
        ("telephony", "twilio", "phone_number"),
        ("phone", "twilio", "phone_number"),
    )
    env_sid = _env_or_config(
        "TWILIO_PHONE_NUMBER_SID",
        ("telephony", "twilio", "phone_number_sid"),
        ("phone", "twilio", "phone_number_sid"),
    )
    state = _load_state()
    twilio_state = state.get("twilio", {}) if isinstance(state.get("twilio"), dict) else {}
    preferred_number = env_number or str(twilio_state.get("default_phone_number", ""))
    preferred_sid = env_sid or str(twilio_state.get("default_phone_sid", ""))

    owned = _twilio_owned_numbers(limit=100)
    if preferred_sid:
        for item in owned:
            if item.sid == preferred_sid:
                return item
    if preferred_number:
        normalized = _normalize_phone(preferred_number)
        for item in owned:
            if item.phone_number == normalized:
                return item
    if len(owned) == 1:
        return owned[0]

    raise TelephonyError(
        "No default Twilio phone number is set. Use 'twilio-buy --save-env', "
        f"'twilio-set-default', or set TWILIO_PHONE_NUMBER in {_env_path()}."
    )


def _vapi_api_key() -> str:
    return _env_or_config(
        "VAPI_API_KEY",
        ("telephony", "vapi", "api_key"),
        ("phone", "vapi", "api_key"),
    )


def _vapi_phone_number_id() -> str:
    state = _load_state()
    vapi_state = state.get("vapi", {}) if isinstance(state.get("vapi"), dict) else {}
    return _env_or_config(
        "VAPI_PHONE_NUMBER_ID",
        ("telephony", "vapi", "phone_number_id"),
        ("phone", "vapi", "phone_number_id"),
        default=str(vapi_state.get("phone_number_id", "")),
    )


def _bland_api_key() -> str:
    return _env_or_config(
        "BLAND_API_KEY",
        ("telephony", "bland", "api_key"),
        ("phone", "bland", "api_key"),
    )


def _ai_provider(default: str = DEFAULT_AI_PROVIDER) -> str:
    return _env_or_config(
        "PHONE_PROVIDER",
        ("telephony", "provider"),
        ("phone", "provider"),
        default=default,
    ).lower().strip()


def _twilio_search_numbers(
    *,
    country: str = "US",
    area_code: str | None = None,
    contains: str | None = None,
    limit: int = 10,
    sms_enabled: bool = True,
    voice_enabled: bool = True,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "PageSize": max(1, min(limit, 20)),
        "SmsEnabled": str(bool(sms_enabled)).lower(),
        "VoiceEnabled": str(bool(voice_enabled)).lower(),
    }
    if area_code:
        params["AreaCode"] = str(area_code)
    if contains:
        params["Contains"] = str(contains)

    payload = _twilio_request(
        "GET",
        f"AvailablePhoneNumbers/{country.upper()}/Local.json",
        params=params,
    )
    items = payload.get("available_phone_numbers", []) or []
    return {
        "success": True,
        "country": country.upper(),
        "count": len(items),
        "numbers": [
            {
                "phone_number": item.get("phone_number"),
                "friendly_name": item.get("friendly_name"),
                "locality": item.get("locality"),
                "region": item.get("region"),
                "postal_code": item.get("postal_code"),
                "iso_country": item.get("iso_country"),
                "capabilities": {
                    "voice": item.get("voice_enabled"),
                    "sms": item.get("sms_enabled"),
                    "mms": item.get("mms_enabled"),
                },
            }
            for item in items
            if isinstance(item, dict)
        ],
    }


def _twilio_buy_number(
    phone_number: str,
    *,
    save_env: bool = False,
    state_path: Path | None = None,
    env_path: Path | None = None,
) -> dict[str, Any]:
    normalized = _normalize_phone(phone_number)
    payload = _twilio_request("POST", "IncomingPhoneNumbers.json", form={"PhoneNumber": normalized})
    purchased = {
        "success": True,
        "provider": "twilio",
        "phone_number": payload.get("phone_number", normalized),
        "phone_sid": payload.get("sid"),
        "friendly_name": payload.get("friendly_name"),
        "capabilities": payload.get("capabilities", {}),
        "message": "Twilio number purchased successfully.",
    }
    purchased.update(
        _remember_twilio_number(
            phone_number=str(purchased["phone_number"]),
            phone_sid=str(purchased.get("phone_sid") or ""),
            save_env=save_env,
            state_path=state_path,
            env_path=env_path,
        )
    )
    return purchased


def _twilio_list_owned() -> dict[str, Any]:
    owned = _twilio_owned_numbers(limit=100)
    return {
        "success": True,
        "provider": "twilio",
        "count": len(owned),
        "numbers": [
            {
                "phone_number": item.phone_number,
                "phone_sid": item.sid,
                "friendly_name": item.friendly_name,
                "capabilities": item.capabilities,
            }
            for item in owned
        ],
    }


def _twilio_set_default(identifier: str, *, save_env: bool = False) -> dict[str, Any]:
    owned = _resolve_twilio_number(identifier)
    result = {
        "success": True,
        "provider": "twilio",
        "phone_number": owned.phone_number,
        "phone_sid": owned.sid,
        "message": "Default Twilio number updated.",
    }
    result.update(
        _remember_twilio_number(
            phone_number=owned.phone_number,
            phone_sid=owned.sid,
            save_env=save_env,
        )
    )
    return result


def _twiml_say(message: str, voice: str) -> str:
    return f"<Response><Say voice=\"{xml_escape(voice)}\">{xml_escape(message)}</Say></Response>"


def _twiml_play(audio_url: str) -> str:
    return f"<Response><Play>{xml_escape(audio_url)}</Play></Response>"


def _twilio_call(
    to_number: str,
    *,
    message: str | None = None,
    audio_url: str | None = None,
    voice: str = TWILIO_DEFAULT_TTS_VOICE,
    send_digits: str | None = None,
    from_identifier: str | None = None,
    record: bool = False,
) -> dict[str, Any]:
    destination = _normalize_phone(to_number)
    source = _resolve_twilio_number(from_identifier)
    if bool(message) == bool(audio_url):
        raise TelephonyError("Provide exactly one of 'message' or 'audio_url' for twilio-call")

    twiml = _twiml_play(audio_url) if audio_url else _twiml_say(message or "", voice)
    form: dict[str, Any] = {
        "To": destination,
        "From": source.phone_number,
        "Twiml": twiml,
    }
    if send_digits:
        form["SendDigits"] = send_digits
    if record:
        form["Record"] = "true"

    payload = _twilio_request("POST", "Calls.json", form=form)
    return {
        "success": True,
        "provider": "twilio",
        "call_sid": payload.get("sid"),
        "status": payload.get("status"),
        "from_phone_number": source.phone_number,
        "to_phone_number_masked": _mask_phone(destination),
        "mode": "play" if audio_url else "say",
        "recording_requested": record,
        "message": "Twilio call initiated.",
    }


def _twilio_call_status(call_sid: str) -> dict[str, Any]:
    payload = _twilio_request("GET", f"Calls/{call_sid}.json")
    return {
        "success": True,
        "provider": "twilio",
        "call_sid": payload.get("sid"),
        "status": payload.get("status"),
        "direction": payload.get("direction"),
        "duration": payload.get("duration"),
        "from_phone_number": payload.get("from"),
        "to_phone_number_masked": _mask_phone(str(payload.get("to") or "")),
        "start_time": payload.get("start_time"),
        "end_time": payload.get("end_time"),
        "answered_by": payload.get("answered_by"),
    }


def _twilio_send_sms(
    to_number: str,
    body: str,
    *,
    media_urls: list[str] | None = None,
    from_identifier: str | None = None,
) -> dict[str, Any]:
    destination = _normalize_phone(to_number)
    source = _resolve_twilio_number(from_identifier)
    if not body.strip():
        raise TelephonyError("SMS body cannot be empty")
    form: dict[str, Any] = {
        "To": destination,
        "From": source.phone_number,
        "Body": body,
    }
    if media_urls:
        form["MediaUrl"] = media_urls
    payload = _twilio_request("POST", "Messages.json", form=form)
    return {
        "success": True,
        "provider": "twilio",
        "message_sid": payload.get("sid"),
        "status": payload.get("status"),
        "from_phone_number": source.phone_number,
        "to_phone_number_masked": _mask_phone(destination),
        "media_count": len(media_urls or []),
        "message": "SMS/MMS queued via Twilio.",
    }


def _checkpoint_for_messages(messages: list[dict[str, Any]]) -> tuple[str, str]:
    if not messages:
        return "", ""
    newest = messages[0]
    return str(newest.get("sid") or ""), str(newest.get("date_sent") or newest.get("date_created") or "")


def _messages_after_checkpoint(messages: list[dict[str, Any]], last_sid: str) -> list[dict[str, Any]]:
    if not last_sid:
        return messages
    filtered: list[dict[str, Any]] = []
    for message in messages:
        if str(message.get("sid") or "") == last_sid:
            break
        filtered.append(message)
    return filtered


def _twilio_inbox(
    *,
    limit: int = 20,
    since_last: bool = False,
    mark_seen: bool = False,
    phone_identifier: str | None = None,
    state_path: Path | None = None,
) -> dict[str, Any]:
    owned = _resolve_twilio_number(phone_identifier)
    payload = _twilio_request(
        "GET",
        "Messages.json",
        params={"To": owned.phone_number, "PageSize": max(1, min(limit, 100))},
    )
    raw_messages = payload.get("messages", []) or []
    messages = [m for m in raw_messages if isinstance(m, dict)]

    state = _load_state(state_path)
    twilio_state = state.setdefault("twilio", {})
    last_sid = str(twilio_state.get("last_inbound_message_sid", ""))
    if since_last:
        messages = _messages_after_checkpoint(messages, last_sid)

    message_rows = [
        {
            "sid": msg.get("sid"),
            "direction": msg.get("direction"),
            "status": msg.get("status"),
            "from_phone_number": msg.get("from"),
            "to_phone_number": msg.get("to"),
            "date_sent": msg.get("date_sent"),
            "body": msg.get("body"),
            "num_media": msg.get("num_media"),
        }
        for msg in messages
    ]

    if mark_seen and message_rows:
        last_seen_sid, last_seen_date = _checkpoint_for_messages(message_rows)
        twilio_state["last_inbound_message_sid"] = last_seen_sid
        twilio_state["last_inbound_message_date"] = last_seen_date
        _save_state(state, state_path)

    return {
        "success": True,
        "provider": "twilio",
        "phone_number": owned.phone_number,
        "count": len(message_rows),
        "messages": message_rows,
        "since_last": since_last,
        "marked_seen": bool(mark_seen and message_rows),
        "state_path": str(state_path or _state_path()),
        "last_seen_message_sid": twilio_state.get("last_inbound_message_sid", ""),
    }


def _vapi_import_twilio_number(
    *,
    phone_identifier: str | None = None,
    save_env: bool = False,
    state_path: Path | None = None,
    env_path: Path | None = None,
) -> dict[str, Any]:
    api_key = _vapi_api_key()
    if not api_key:
        raise TelephonyError(
            f"Vapi is not configured. Use 'save-vapi' or set VAPI_API_KEY in {_env_path()} first."
        )
    owned = _resolve_twilio_number(phone_identifier)
    sid, token = _twilio_creds()
    payload = _json_request(
        "POST",
        f"{VAPI_API_BASE}/phone-number",
        headers={"Authorization": f"Bearer {api_key}"},
        json_body={
            "provider": "twilio",
            "number": owned.phone_number,
            "twilioAccountSid": sid,
            "twilioAuthToken": token,
        },
    )
    phone_number_id = str(payload.get("id") or "")
    if not phone_number_id:
        raise TelephonyError(f"Vapi did not return a phone number id: {payload}")
    result = {
        "success": True,
        "provider": "vapi",
        "phone_number_id": phone_number_id,
        "phone_number": owned.phone_number,
        "message": "Twilio number imported into Vapi.",
    }
    result.update(
        _remember_vapi_number(
            phone_number_id=phone_number_id,
            save_env=save_env,
            state_path=state_path,
            env_path=env_path,
        )
    )
    return result


def _bland_call(
    phone_number: str,
    task: str,
    *,
    voice: str | None = None,
    first_sentence: str | None = None,
    max_duration: int = 3,
) -> dict[str, Any]:
    api_key = _bland_api_key()
    if not api_key:
        raise TelephonyError(
            f"Bland.ai is not configured. Use 'save-bland' or set BLAND_API_KEY in {_env_path()}."
        )
    normalized = _normalize_phone(phone_number)
    if voice is None:
        voice = _env_or_config(
            "BLAND_DEFAULT_VOICE",
            ("telephony", "bland", "default_voice"),
            ("phone", "bland", "default_voice"),
            default=BLAND_DEFAULT_VOICE,
        )
    payload = _json_request(
        "POST",
        f"{BLAND_API_BASE}/calls",
        headers={"authorization": api_key},
        json_body={
            "phone_number": normalized,
            "task": task,
            "voice": voice,
            "model": BLAND_DEFAULT_MODEL,
            "max_duration": max_duration,
            "record": True,
            "wait_for_greeting": True,
            **({"first_sentence": first_sentence} if first_sentence else {}),
        },
    )
    call_id = str(payload.get("call_id") or "")
    if not call_id:
        raise TelephonyError(f"Bland.ai returned no call_id: {payload}")
    return {
        "success": True,
        "provider": "bland",
        "call_id": call_id,
        "voice": voice,
        "max_duration_minutes": max_duration,
        "to_phone_number_masked": _mask_phone(normalized),
        "message": "AI call queued with Bland.ai.",
    }


def _bland_status(call_id: str, analyze: str | None = None) -> dict[str, Any]:
    api_key = _bland_api_key()
    if not api_key:
        raise TelephonyError("Bland.ai is not configured.")
    payload = _json_request("GET", f"{BLAND_API_BASE}/calls/{call_id}", headers={"authorization": api_key})
    result = {
        "success": True,
        "provider": "bland",
        "call_id": call_id,
        "status": payload.get("status"),
        "answered_by": payload.get("answered_by"),
        "duration_minutes": payload.get("call_length"),
        "transcript": payload.get("concatenated_transcript", ""),
        "recording_url": payload.get("recording_url"),
    }
    if analyze and payload.get("status") == "completed":
        questions = [[q.strip(), "string"] for q in analyze.split(",") if q.strip()]
        if questions:
            analysis = _json_request(
                "POST",
                f"{BLAND_API_BASE}/calls/{call_id}/analyze",
                headers={"authorization": api_key},
                json_body={"questions": questions},
            )
            result["analysis"] = analysis
    return result


def _vapi_call(
    phone_number: str,
    task: str,
    *,
    voice_id: str | None = None,
    first_sentence: str | None = None,
    max_duration: int = 3,
) -> dict[str, Any]:
    api_key = _vapi_api_key()
    if not api_key:
        raise TelephonyError(
            f"Vapi is not configured. Use 'save-vapi' or set VAPI_API_KEY in {_env_path()}."
        )
    phone_number_id = _vapi_phone_number_id()
    if not phone_number_id:
        raise TelephonyError(
            "No Vapi phone number id is configured. Import an owned Twilio number with "
            f"'vapi-import-twilio --save-env' or set VAPI_PHONE_NUMBER_ID in {_env_path()}."
        )
    normalized = _normalize_phone(phone_number)
    voice_provider = _env_or_config(
        "VAPI_VOICE_PROVIDER",
        ("telephony", "vapi", "default_voice_provider"),
        ("phone", "vapi", "default_voice_provider"),
        default=VAPI_DEFAULT_VOICE_PROVIDER,
    )
    if voice_id is None:
        voice_id = _env_or_config(
            "VAPI_VOICE_ID",
            ("telephony", "vapi", "default_voice_id"),
            ("phone", "vapi", "default_voice_id"),
            default=VAPI_DEFAULT_VOICE_ID,
        )
    model = _env_or_config(
        "VAPI_MODEL",
        ("telephony", "vapi", "model"),
        ("phone", "vapi", "model"),
        default=VAPI_DEFAULT_MODEL,
    )
    assistant = {
        "model": {
            "provider": "openai",
            "model": model,
            "messages": [{"role": "system", "content": task}],
        },
        "voice": {"provider": voice_provider, "voiceId": voice_id},
        "maxDurationSeconds": max_duration * 60,
    }
    if first_sentence:
        assistant["firstMessage"] = first_sentence
    payload = _json_request(
        "POST",
        f"{VAPI_API_BASE}/call",
        headers={"Authorization": f"Bearer {api_key}"},
        json_body={
            "phoneNumberId": phone_number_id,
            "customer": {"number": normalized},
            "assistant": assistant,
        },
    )
    call_id = str(payload.get("id") or "")
    if not call_id:
        raise TelephonyError(f"Vapi returned no call id: {payload}")
    return {
        "success": True,
        "provider": "vapi",
        "call_id": call_id,
        "voice_provider": voice_provider,
        "voice_id": voice_id,
        "max_duration_minutes": max_duration,
        "to_phone_number_masked": _mask_phone(normalized),
        "message": "AI call queued with Vapi.",
    }


def _vapi_status(call_id: str) -> dict[str, Any]:
    api_key = _vapi_api_key()
    if not api_key:
        raise TelephonyError("Vapi is not configured.")
    payload = _json_request(
        "GET",
        f"{VAPI_API_BASE}/call/{call_id}",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    return {
        "success": True,
        "provider": "vapi",
        "call_id": call_id,
        "status": payload.get("status"),
        "duration_seconds": payload.get("duration"),
        "ended_reason": payload.get("endedReason"),
        "transcript": payload.get("transcript", ""),
        "recording_url": payload.get("recordingUrl"),
        "summary": payload.get("summary"),
        "cost": payload.get("cost"),
    }


def _provider_decision_tree() -> list[dict[str, str]]:
    return [
        {
            "need": "I want the agent to own a real number for SMS, inbound polling, or future telephony identity.",
            "use": "Twilio",
            "why": "Twilio is the clearest path to provisioning numbers, sending SMS/MMS, polling inbound texts, and later webhook-based inbound telephony.",
        },
        {
            "need": "I only want the easiest outbound AI voice calls right now.",
            "use": "Bland.ai",
            "why": "Bland is the simplest outbound AI calling setup: one API key, no separate number import flow.",
        },
        {
            "need": "I want premium conversational voice quality for AI calls, ideally on my own number.",
            "use": "Twilio + Vapi",
            "why": "Buy/import the number with Twilio, then import it into Vapi for better voices and more flexible assistants.",
        },
        {
            "need": "I want to call with a prerecorded/custom voice message generated elsewhere.",
            "use": "Twilio direct call + public audio URL",
            "why": "Generate or host audio separately, then let Twilio play it with a simple outbound call.",
        },
        {
            "need": "I need one shared Saudi +966 number for local voice (Twilio cannot do this).",
            "use": "Wave (fallback Unifonic)",
            "why": (
                "Wave documents Saudi number allocation and POST /v1/calls. "
                "Production eligibility, number entitlement, pricing and account voice flow require vendor confirmation. "
                "Unifonic and CEQUENS are alternatives to compare if Wave cannot enable this account."
            ),
        },
    ]


def _wave_api_key() -> str:
    return _env_or_config(
        "WAVE_API_KEY",
        ("telephony", "wave", "api_key"),
        ("phone", "wave", "api_key"),
    )


def _wave_from_number() -> str:
    return _env_or_config(
        "WAVE_FROM_NUMBER",
        ("telephony", "wave", "from_number"),
        ("phone", "wave", "from_number"),
    )


def _wave_headers(api_key: str | None = None) -> dict[str, str]:
    key = (api_key or _wave_api_key()).strip()
    if not key:
        raise TelephonyError(
            "Wave is not configured. Use 'save-wave' or set WAVE_API_KEY in "
            f"{_env_path()}. Live calls also need a production key (sk_live_) "
            "after Owner KYC / Go-Live."
        )
    return {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }


def _wave_key_kind(api_key: str) -> str:
    if api_key.startswith("sk_live_"):
        return "live"
    if api_key.startswith("sk_sandbox_"):
        return "sandbox"
    return "unknown"


def _wave_request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    """Keep provider error bodies, which may echo credentials, out of results."""
    try:
        return _json_request(method, f"{WAVE_API_BASE}{path}", **kwargs)
    except TelephonyError as exc:
        status = re.search(r"HTTP (\d{3})", str(exc))
        suffix = f" (HTTP {status.group(1)})" if status else ""
        raise TelephonyError(
            f"Wave request failed{suffix}. Check account status, scopes and Go-Live "
            "in the Wave dashboard. Call initiation was not confirmed; inspect "
            "call logs before retrying to avoid a duplicate call."
        ) from None


def save_wave(api_key: str, from_number: str = "", caller_id_name: str = "") -> dict[str, Any]:
    key = api_key.strip()
    if not key:
        raise TelephonyError("WAVE_API_KEY is required")
    if not re.fullmatch(r"sk_(?:live|sandbox)_[A-Za-z0-9_-]+", key):
        raise TelephonyError("Expected a Wave sk_live_ or sk_sandbox_ key")
    updates = {"WAVE_API_KEY": key}
    if from_number:
        updates["WAVE_FROM_NUMBER"] = _normalize_sa_phone(from_number)
    if caller_id_name.strip():
        updates["WAVE_CALLER_ID_NAME"] = caller_id_name.strip()[:64]
    env_file = _upsert_env_file(updates)
    kind = _wave_key_kind(key)
    return {
        "success": True,
        "provider": "wave",
        "saved_env_keys": sorted(updates),
        "env_path": str(env_file),
        "key_kind": kind,
        "external_gate": kind != "live" or not bool(from_number or _wave_from_number()),
        "provider_verified": False,
        "message": (
            f"Wave credentials saved to {env_file}. "
            "POST /v1/calls is dry-run unless you pass --confirm after Owner KYC."
        ),
    }


def wave_health() -> dict[str, Any]:
    payload = _wave_request("GET", "/v1/health")
    return {
        "success": True,
        "provider": "wave",
        "status": payload.get("status", ""),
        "service": payload.get("service", ""),
    }


def wave_call(
    to_number: str,
    *,
    confirm: bool = False,
    caller_id_name: str = "",
    caller_id_number: str = "",
) -> dict[str, Any]:
    """Place or preview a Wave outbound call.

    Default is a dry-run: validates the Saudi destination and reports
    EXTERNAL_GATE when no live key is present. Never POSTs unless
    ``confirm=True``.
    """
    destination = _normalize_sa_phone(to_number)
    api_key = _wave_api_key()
    kind = _wave_key_kind(api_key) if api_key else "missing"
    body: dict[str, Any] = {"to": destination}
    name = caller_id_name.strip() or _env_or_config(
        "WAVE_CALLER_ID_NAME",
        ("telephony", "wave", "caller_id_name"),
    )
    if name:
        body["caller_id_name"] = name[:64]
    from_number = caller_id_number.strip() or _wave_from_number()
    if from_number:
        body["caller_id_number"] = _normalize_sa_phone(from_number)
    result = {
        "success": True,
        "provider": "wave",
        "dry_run": not confirm,
        "key_kind": kind,
        "to_phone_number_masked": _mask_phone(destination),
        "request": {k: v for k, v in body.items() if k != "to"} | {"to_masked": _mask_phone(destination)},
        "external_gate": kind != "live" or not bool(from_number),
        "provider_verified": False,
        "notes": [
            "Wave POST /v1/calls requires calls:write and a production key.",
            "Sandbox keys can only place web callbacks, not unconstrained outbound calls.",
            "Owner KYC / Go-Live is EXTERNAL_GATE. Do not provision a number from this skill.",
            "A key prefix does not prove account eligibility, number ownership or an AI call flow.",
            "This command initiates the account's configured call flow; it does not supply a booking task.",
        ],
    }
    if not confirm:
        result["message"] = (
            "Dry-run only. Re-run with --confirm after Owner KYC and a sk_live_ key "
            "to place a real Wave call."
        )
        return result
    if kind != "live":
        raise TelephonyError(
            "Refusing live Wave call: production key (sk_live_) required. "
            "Owner must complete Wave Go-Live / KYC first (EXTERNAL_GATE)."
        )
    if not from_number:
        raise TelephonyError("Wave from-number is required; configure an owned Saudi number before calling (EXTERNAL_GATE).")
    payload = _wave_request(
        "POST",
        "/v1/calls",
        headers=_wave_headers(api_key),
        json_body=body,
    )
    return {
        "success": True,
        "provider": "wave",
        "dry_run": False,
        "call_id": payload.get("call_id", ""),
        "status": payload.get("status", ""),
        "direction": payload.get("direction", "outbound"),
        "to_phone_number_masked": _mask_phone(destination),
        "sandbox": payload.get("sandbox"),
        "message": "Wave outbound call accepted.",
    }


def wave_logs(*, limit: int = 20, cursor: str = "") -> dict[str, Any]:
    api_key = _wave_api_key()
    if not api_key:
        raise TelephonyError("Wave is not configured.")
    params: dict[str, Any] = {"limit": max(1, min(int(limit), 100))}
    if cursor:
        params["cursor"] = cursor
    payload = _wave_request(
        "GET",
        "/v1/calls",
        headers=_wave_headers(api_key),
        params=params,
    )
    return {
        "success": True,
        "provider": "wave",
        "calls": [
            {
                **{key: call[key] for key in (
                    "id", "type", "status", "duration_seconds", "created_at",
                    "answered_at", "ended_at",
                ) if key in call},
                "to_masked": _mask_phone(str(call.get("to_masked") or call.get("to") or "")),
                "from_masked": _mask_phone(str(call.get("from") or "")),
            }
            for call in payload.get("data", []) if isinstance(call, dict)
        ],
        "next_cursor": payload.get("next_cursor"),
    }


def diagnose() -> dict[str, Any]:
    state = _load_state()
    twilio_state = state.get("twilio", {}) if isinstance(state.get("twilio"), dict) else {}
    vapi_state = state.get("vapi", {}) if isinstance(state.get("vapi"), dict) else {}
    provider = _ai_provider()

    twilio_sid = _env_or_config(
        "TWILIO_ACCOUNT_SID",
        ("telephony", "twilio", "account_sid"),
        ("phone", "twilio", "account_sid"),
    )
    twilio_token = _env_or_config(
        "TWILIO_AUTH_TOKEN",
        ("telephony", "twilio", "auth_token"),
        ("phone", "twilio", "auth_token"),
    )
    twilio_phone = _env_or_config(
        "TWILIO_PHONE_NUMBER",
        ("telephony", "twilio", "phone_number"),
        ("phone", "twilio", "phone_number"),
        default=str(twilio_state.get("default_phone_number", "")),
    )

    bland_key = _bland_api_key()
    vapi_key = _vapi_api_key()
    vapi_phone_id = _vapi_phone_number_id() or str(vapi_state.get("phone_number_id", ""))
    wave_key = _wave_api_key()
    wave_kind = _wave_key_kind(wave_key) if wave_key else "missing"

    return {
        "success": True,
        "state_path": str(_state_path()),
        "env_path": str(_env_path()),
        "ai_call_provider": provider,
        "providers": {
            "twilio": {
                "account_sid_configured": bool(twilio_sid),
                "auth_token_configured": bool(twilio_token),
                "default_phone_number": twilio_phone,
                "default_phone_sid": twilio_state.get("default_phone_sid", ""),
                "last_inbound_message_sid": twilio_state.get("last_inbound_message_sid", ""),
                "last_inbound_message_date": twilio_state.get("last_inbound_message_date", ""),
            },
            "bland": {
                "configured": bool(bland_key),
                "default_voice": _env_or_config(
                    "BLAND_DEFAULT_VOICE",
                    ("telephony", "bland", "default_voice"),
                    ("phone", "bland", "default_voice"),
                    default=BLAND_DEFAULT_VOICE,
                ),
            },
            "wave": {
                "configured": bool(wave_key),
                "key_kind": wave_kind,
                "from_number_configured": bool(_wave_from_number()),
                "external_gate": wave_kind != "live" or not bool(_wave_from_number()),
                "provider_verified": False,
                "live_number": None,
            },
            "vapi": {
                "configured": bool(vapi_key),
                "phone_number_id": vapi_phone_id,
                "voice_provider": _env_or_config(
                    "VAPI_VOICE_PROVIDER",
                    ("telephony", "vapi", "default_voice_provider"),
                    ("phone", "vapi", "default_voice_provider"),
                    default=VAPI_DEFAULT_VOICE_PROVIDER,
                ),
                "voice_id": _env_or_config(
                    "VAPI_VOICE_ID",
                    ("telephony", "vapi", "default_voice_id"),
                    ("phone", "vapi", "default_voice_id"),
                    default=VAPI_DEFAULT_VOICE_ID,
                ),
                "model": _env_or_config(
                    "VAPI_MODEL",
                    ("telephony", "vapi", "model"),
                    ("phone", "vapi", "model"),
                    default=VAPI_DEFAULT_MODEL,
                ),
            },
        },
        "decision_tree": _provider_decision_tree(),
        "notes": [
            "Twilio is the best path for owning a durable phone number, texting, and polling inbound SMS.",
            "Bland is the easiest path for outbound AI calls only.",
            "Vapi is best when you want better AI voice quality, usually backed by a Twilio-owned number.",
            "Wave is a provisional Saudi +966 path, pending provider production/account-flow confirmation and Owner KYC.",
            "Unifonic and CEQUENS are alternatives; neither has a client in this skill.",
            "VoIP numbers are not guaranteed to work for every third-party 2FA flow.",
        ],
    }


def save_twilio(account_sid: str, auth_token: str, phone_number: str = "", phone_sid: str = "") -> dict[str, Any]:
    updates = {
        "TWILIO_ACCOUNT_SID": account_sid.strip(),
        "TWILIO_AUTH_TOKEN": auth_token.strip(),
    }
    if phone_number:
        updates["TWILIO_PHONE_NUMBER"] = _normalize_phone(phone_number)
    if phone_sid:
        updates["TWILIO_PHONE_NUMBER_SID"] = phone_sid.strip()
    env_file = _upsert_env_file(updates)
    result = {
        "success": True,
        "provider": "twilio",
        "saved_env_keys": sorted(updates),
        "env_path": str(env_file),
        "message": f"Twilio credentials saved to {env_file}.",
    }
    if phone_number:
        result.update(_remember_twilio_number(phone_number=updates["TWILIO_PHONE_NUMBER"], phone_sid=phone_sid.strip(), save_env=False))
    return result


def save_bland(api_key: str, voice: str = BLAND_DEFAULT_VOICE) -> dict[str, Any]:
    env_file = _upsert_env_file(
        {
            "BLAND_API_KEY": api_key.strip(),
            "BLAND_DEFAULT_VOICE": voice.strip() or BLAND_DEFAULT_VOICE,
            "PHONE_PROVIDER": "bland",
        }
    )
    return {
        "success": True,
        "provider": "bland",
        "saved_env_keys": ["BLAND_API_KEY", "BLAND_DEFAULT_VOICE", "PHONE_PROVIDER"],
        "env_path": str(env_file),
        "message": f"Bland.ai configuration saved to {env_file}.",
    }


def save_vapi(
    api_key: str,
    *,
    phone_number_id: str = "",
    voice_provider: str = VAPI_DEFAULT_VOICE_PROVIDER,
    voice_id: str = VAPI_DEFAULT_VOICE_ID,
    model: str = VAPI_DEFAULT_MODEL,
) -> dict[str, Any]:
    updates = {
        "VAPI_API_KEY": api_key.strip(),
        "VAPI_VOICE_PROVIDER": voice_provider.strip() or VAPI_DEFAULT_VOICE_PROVIDER,
        "VAPI_VOICE_ID": voice_id.strip() or VAPI_DEFAULT_VOICE_ID,
        "VAPI_MODEL": model.strip() or VAPI_DEFAULT_MODEL,
        "PHONE_PROVIDER": "vapi",
    }
    if phone_number_id:
        updates["VAPI_PHONE_NUMBER_ID"] = phone_number_id.strip()
    env_file = _upsert_env_file(updates)
    result = {
        "success": True,
        "provider": "vapi",
        "saved_env_keys": sorted(updates),
        "env_path": str(env_file),
        "message": f"Vapi configuration saved to {env_file}.",
    }
    if phone_number_id:
        result.update(_remember_vapi_number(phone_number_id=phone_number_id.strip(), save_env=False))
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hermes telephony helper")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("diagnose", help="Show saved telephony state and provider readiness")

    p = sub.add_parser("save-twilio", help="Save Twilio credentials to the Hermes .env file")
    p.add_argument("account_sid")
    p.add_argument("auth_token")
    p.add_argument("--phone-number", default="")
    p.add_argument("--phone-sid", default="")

    p = sub.add_parser("save-bland", help="Save Bland.ai settings to the Hermes .env file")
    p.add_argument("api_key")
    p.add_argument("--voice", default=BLAND_DEFAULT_VOICE)

    p = sub.add_parser("save-vapi", help="Save Vapi settings to the Hermes .env file")
    p.add_argument("api_key")
    p.add_argument("--phone-number-id", default="")
    p.add_argument("--voice-provider", default=VAPI_DEFAULT_VOICE_PROVIDER)
    p.add_argument("--voice-id", default=VAPI_DEFAULT_VOICE_ID)
    p.add_argument("--model", default=VAPI_DEFAULT_MODEL)

    p = sub.add_parser("save-wave", help="Save Wave API credentials to the Hermes .env file")
    p.add_argument("api_key", nargs="?", help="Omit for hidden Owner input; avoid credentials in shell history")
    p.add_argument("--from-number", default="")
    p.add_argument("--caller-id-name", default="")

    sub.add_parser("wave-health", help="GET /v1/health on api.wave.sa (no credentials)")

    p = sub.add_parser("wave-call", help="Preview or place a Wave outbound call (dry-run unless --confirm)")
    p.add_argument("to_number")
    p.add_argument("--confirm", action="store_true", help="Actually POST /v1/calls (requires sk_live_)")
    p.add_argument("--caller-id-name", default="")
    p.add_argument("--from-number", default="")

    p = sub.add_parser("wave-logs", help="List Wave call logs (masked destinations)")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--cursor", default="")

    p = sub.add_parser("twilio-search", help="Search Twilio numbers available for purchase")
    p.add_argument("--country", default="US")
    p.add_argument("--area-code", default="")
    p.add_argument("--contains", default="")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--sms-enabled", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--voice-enabled", action=argparse.BooleanOptionalAction, default=True)

    p = sub.add_parser("twilio-buy", help="Buy a Twilio phone number")
    p.add_argument("phone_number")
    p.add_argument("--save-env", action="store_true")

    sub.add_parser("twilio-owned", help="List Twilio numbers already owned by the account")

    p = sub.add_parser("twilio-set-default", help="Remember one owned Twilio number as the default")
    p.add_argument("identifier", help="Owned phone number in E.164 or Twilio phone SID")
    p.add_argument("--save-env", action="store_true")

    p = sub.add_parser("twilio-call", help="Place a direct Twilio call")
    p.add_argument("to_number")
    p.add_argument("--message", default="")
    p.add_argument("--audio-url", default="")
    p.add_argument("--voice", default=TWILIO_DEFAULT_TTS_VOICE)
    p.add_argument("--send-digits", default="")
    p.add_argument("--from-number", default="")
    p.add_argument("--record", action="store_true")

    p = sub.add_parser("twilio-call-status", help="Check a Twilio call status")
    p.add_argument("call_sid")

    p = sub.add_parser("twilio-send-sms", help="Send SMS or MMS via Twilio")
    p.add_argument("to_number")
    p.add_argument("body")
    p.add_argument("--media-url", action="append", default=[])
    p.add_argument("--from-number", default="")

    p = sub.add_parser("twilio-inbox", help="Poll inbound SMS for the default or specified Twilio number")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--since-last", action="store_true")
    p.add_argument("--mark-seen", action="store_true")
    p.add_argument("--phone-number", default="")

    p = sub.add_parser("vapi-import-twilio", help="Import an owned Twilio number into Vapi")
    p.add_argument("--phone-number", default="")
    p.add_argument("--save-env", action="store_true")

    p = sub.add_parser("ai-call", help="Place an outbound AI voice call via Bland.ai or Vapi")
    p.add_argument("to_number")
    p.add_argument("task")
    p.add_argument("--provider", choices=["bland", "vapi"], default="")
    p.add_argument("--voice", default="")
    p.add_argument("--first-sentence", default="")
    p.add_argument("--max-duration", type=int, default=3)

    p = sub.add_parser("ai-status", help="Check an AI call status via Bland.ai or Vapi")
    p.add_argument("call_id")
    p.add_argument("--provider", choices=["bland", "vapi"], default="")
    p.add_argument("--analyze", default="")

    return parser


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    cmd = args.command
    if cmd == "diagnose":
        return diagnose()
    if cmd == "save-twilio":
        return save_twilio(args.account_sid, args.auth_token, phone_number=args.phone_number, phone_sid=args.phone_sid)
    if cmd == "save-bland":
        return save_bland(args.api_key, voice=args.voice)
    if cmd == "save-vapi":
        return save_vapi(
            args.api_key,
            phone_number_id=args.phone_number_id,
            voice_provider=args.voice_provider,
            voice_id=args.voice_id,
            model=args.model,
        )
    if cmd == "save-wave":
        return save_wave(
            args.api_key if args.api_key is not None else getpass.getpass("Wave API key (hidden): "),
            from_number=args.from_number,
            caller_id_name=args.caller_id_name,
        )
    if cmd == "wave-health":
        return wave_health()
    if cmd == "wave-call":
        return wave_call(
            args.to_number,
            confirm=args.confirm,
            caller_id_name=args.caller_id_name,
            caller_id_number=args.from_number,
        )
    if cmd == "wave-logs":
        return wave_logs(limit=args.limit, cursor=args.cursor)
    if cmd == "twilio-search":
        return _twilio_search_numbers(
            country=args.country,
            area_code=args.area_code or None,
            contains=args.contains or None,
            limit=args.limit,
            sms_enabled=args.sms_enabled,
            voice_enabled=args.voice_enabled,
        )
    if cmd == "twilio-buy":
        return _twilio_buy_number(args.phone_number, save_env=args.save_env)
    if cmd == "twilio-owned":
        return _twilio_list_owned()
    if cmd == "twilio-set-default":
        return _twilio_set_default(args.identifier, save_env=args.save_env)
    if cmd == "twilio-call":
        return _twilio_call(
            args.to_number,
            message=args.message or None,
            audio_url=args.audio_url or None,
            voice=args.voice,
            send_digits=args.send_digits or None,
            from_identifier=args.from_number or None,
            record=args.record,
        )
    if cmd == "twilio-call-status":
        return _twilio_call_status(args.call_sid)
    if cmd == "twilio-send-sms":
        return _twilio_send_sms(
            args.to_number,
            args.body,
            media_urls=args.media_url or None,
            from_identifier=args.from_number or None,
        )
    if cmd == "twilio-inbox":
        return _twilio_inbox(
            limit=args.limit,
            since_last=args.since_last,
            mark_seen=args.mark_seen,
            phone_identifier=args.phone_number or None,
        )
    if cmd == "vapi-import-twilio":
        return _vapi_import_twilio_number(
            phone_identifier=args.phone_number or None,
            save_env=args.save_env,
        )
    if cmd == "ai-call":
        provider = (args.provider or _ai_provider()).lower().strip()
        if provider == "vapi":
            return _vapi_call(
                args.to_number,
                args.task,
                voice_id=args.voice or None,
                first_sentence=args.first_sentence or None,
                max_duration=args.max_duration,
            )
        if provider == "bland":
            return _bland_call(
                args.to_number,
                args.task,
                voice=args.voice or None,
                first_sentence=args.first_sentence or None,
                max_duration=args.max_duration,
            )
        raise TelephonyError(
            f"Unsupported AI call provider '{provider}'. Use --provider bland or --provider vapi, "
            f"or set PHONE_PROVIDER in {_env_path()}."
        )
    if cmd == "ai-status":
        provider = (args.provider or _ai_provider()).lower().strip()
        if provider == "vapi":
            return _vapi_status(args.call_id)
        if provider == "bland":
            return _bland_status(args.call_id, analyze=args.analyze or None)
        raise TelephonyError(
            f"Unsupported AI call provider '{provider}'. Use --provider bland or --provider vapi, "
            f"or set PHONE_PROVIDER in {_env_path()}."
        )
    raise TelephonyError(f"Unknown command: {cmd}")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        result = _dispatch(args)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except TelephonyError as exc:
        print(json.dumps({"success": False, "error": str(exc)}, indent=2, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
