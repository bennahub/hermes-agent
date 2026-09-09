"""Connection-fault contract: a broken Connection is never an ordinary agent answer.

The defect these pin: a revoked Anthropic OAuth grant reached the Owner on Mac as the literal
assistant reply ``HTTP 401: OAuth access token has been revoked.`` — the terminal auth path had no
structured verdict (billing and content-policy did) and fell through to a raw-summary result.
"""

import json
import types

import pytest

from agent import connection_health as ch
from agent.error_classifier import ClassifiedError, FailoverReason


# ── Classification ──


@pytest.mark.parametrize("message,status,expected", [
    ("HTTP 401: OAuth access token has been revoked.", 401, ch.REASON_REVOKED),
    ("Error code: 401 - {'error': {'message': 'OAuth access token has been revoked.'}}", 401, ch.REASON_REVOKED),
    ("invalid_grant", 400, ch.REASON_REFRESH_FAILED),
    ("token expired", 401, ch.REASON_EXPIRED),
    ("No api key provided", 401, ch.REASON_MISSING),
    ("insufficient scope for this resource", 403, ch.REASON_FORBIDDEN),
    ("something the classifier has never seen", 401, ch.REASON_INVALID),
    ("something the classifier has never seen", 403, ch.REASON_FORBIDDEN),
])
def test_classify_reason_is_structural(message, status, expected):
    assert ch.classify_reason(message, status) == expected


def test_every_reason_is_owner_gated_and_not_retryable():
    """No surface may offer Retry for a fault only the Owner can clear."""
    for reason in (ch.REASON_REVOKED, ch.REASON_EXPIRED, ch.REASON_REFRESH_FAILED,
                   ch.REASON_MISSING, ch.REASON_INVALID, ch.REASON_FORBIDDEN):
        block = ch.build_connection_block(provider="anthropic", reason_code=reason)
        assert block.retryable is False
        assert block.owner_action in (ch.ACTION_REAUTHORIZE, ch.ACTION_CONFIGURE)


# ── The invariant: no provider prose crosses the boundary ──


# Provider prose that must never reach an owner-facing string. ``provider_status`` is a bare
# integer on purpose — a machine-readable code is not prose, and clients never render it as text.
_LEAK_MARKERS = ("HTTP 401", "OAuth access token", "revoked.", "Traceback", "anthropic.Authentication",
                 "request_id", "invalid_grant", "sk-ant", "Error code")


def test_block_never_carries_provider_text():
    raw = ("Error code: 401 - {'type': 'error', 'error': {'type': 'authentication_error', "
           "'message': 'OAuth access token has been revoked.'}, 'request_id': None}")
    block = ch.build_connection_block(
        provider="anthropic", base_url="https://api.anthropic.com", message=raw, status_code=401)
    serialized = json.dumps(block.to_dict())
    for marker in _LEAK_MARKERS:
        assert marker not in serialized, f"provider text {marker!r} leaked into the block"
    # Every string-typed field is owner-facing and must be free of the raw status too.
    for name, value in block.to_dict().items():
        if isinstance(value, str):
            assert "401" not in value, f"raw status leaked into the {name} string"
    assert block.reason_code == ch.REASON_REVOKED
    assert block.provider_label == "Anthropic"
    assert block.provider_status == 401


def test_owner_message_is_actionable_and_names_no_secret():
    block = ch.build_connection_block(provider="anthropic", reason_code=ch.REASON_REVOKED)
    assert "reconnect" in block.message.lower()
    assert "sk-" not in block.message and "token" not in block.message.lower()


# ── Canonical Connection identity ──


def test_borrowed_claude_code_grant_maps_to_the_claude_code_row(monkeypatch):
    """The runtime borrows ``~/.claude/.credentials.json``; the fault must name THAT row.

    Naming the plain ``anthropic`` row instead is what sent the Owner to an "Anthropic API Key"
    card that could not fix a revoked Claude subscription grant.
    """
    import agent.anthropic_credentials as ac
    monkeypatch.setattr(ac, "_getenv", lambda name, default="": "")
    monkeypatch.setattr(ac, "read_claude_code_credentials",
                        lambda: {"accessToken": "x", "refreshToken": "y", "expiresAt": 1})
    assert ch.connection_id_for("anthropic") == "claude-code"


def test_explicit_api_key_maps_to_the_anthropic_row(monkeypatch):
    import agent.anthropic_credentials as ac
    monkeypatch.setattr(ac, "_getenv", lambda name, default="": "sk-ant-api-xxx" if name == "ANTHROPIC_API_KEY" else "")
    monkeypatch.setattr(ac, "read_claude_code_credentials", lambda: None)
    assert ch.connection_id_for("anthropic") == "anthropic"


def test_connection_id_is_fail_soft(monkeypatch):
    import agent.anthropic_credentials as ac
    monkeypatch.setattr(ac, "read_claude_code_credentials", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert ch.connection_id_for("anthropic") == "anthropic"


# ── The health store: one canonical state for runtime + Connections UI ──


@pytest.fixture()
def health_home(tmp_path, monkeypatch):
    monkeypatch.setattr(ch, "health_store_path", lambda: tmp_path / "state" / "connection_health.json")
    return tmp_path


def test_fault_is_recorded_read_back_and_cleared(health_home):
    block = ch.build_connection_block(provider="anthropic", reason_code=ch.REASON_REVOKED).to_dict()
    block["connection_id"] = "claude-code"
    ch.record_fault(block)
    fault = ch.observed_fault("claude-code")
    assert fault["reason_code"] == ch.REASON_REVOKED
    assert fault["owner_action"] == ch.ACTION_REAUTHORIZE
    assert fault["first_seen_at"] <= fault["last_seen_at"]
    ch.record_healthy("claude-code")
    assert ch.observed_fault("claude-code") is None


def test_repeat_faults_keep_first_seen_at(health_home):
    block = ch.build_connection_block(provider="anthropic", reason_code=ch.REASON_REVOKED).to_dict()
    block["connection_id"] = "claude-code"
    ch.record_fault(block)
    first = ch.observed_fault("claude-code")["first_seen_at"]
    ch.record_fault(block)
    assert ch.observed_fault("claude-code")["first_seen_at"] == first


def test_health_store_is_secret_free(health_home):
    block = ch.build_connection_block(
        provider="anthropic", message="sk-ant-oat01-SECRET rejected", status_code=401).to_dict()
    ch.record_fault(block)
    written = (health_home / "state" / "connection_health.json").read_text()
    assert "sk-ant" not in written and "SECRET" not in written


def test_health_store_is_owner_readable_only(health_home):
    ch.record_fault(ch.build_connection_block(provider="anthropic", reason_code=ch.REASON_REVOKED).to_dict())
    assert oct((health_home / "state" / "connection_health.json").stat().st_mode & 0o777) == "0o600"


def test_store_failure_never_breaks_the_turn(monkeypatch):
    monkeypatch.setattr(ch, "health_store_path", lambda: (_ for _ in ()).throw(OSError("no home")))
    ch.record_fault({"connection_id": "claude-code", "reason_code": ch.REASON_REVOKED})  # must not raise
    assert ch.observed_fault("claude-code") is None


# ── Cross-scope: one canonical location, or the two projections still cannot agree ──
#
# On the VPS 16 named profiles borrow ONE Claude Code grant. The runtime that observed the fault ran
# as profile ``badr``; ``build_inventory`` builds account rows inside ``scope_context()`` (default).
# A store anchored to ``get_hermes_home()`` put those two on different files, so Connections would
# have shown that row healthy for the whole 44-hour outage. These read and write through the REAL
# ``health_store_path`` on purpose — the fixture that stubbed it is what let the divergence through.


@pytest.fixture()
def real_store(monkeypatch):
    """Real store resolution against the conftest HERMES_HOME, plus a named profile beside it."""
    from pathlib import Path
    from hermes_constants import get_default_hermes_root
    root = Path(get_default_hermes_root())
    (root / "profiles" / "badr").mkdir(parents=True, exist_ok=True)
    (root / "profiles" / "badr" / "config.yaml").write_text("{}\n", encoding="utf-8")
    return root


def _revoked(connection_id):
    block = ch.build_connection_block(provider="anthropic", reason_code=ch.REASON_REVOKED).to_dict()
    block["connection_id"] = connection_id
    return block


def test_the_store_path_is_identical_from_every_scope(real_store):
    """One canonical location: the path may not move when a reader scopes into a profile."""
    from hermes_cli.connections import scope_context
    with scope_context():
        default_path = ch.health_store_path()
    with scope_context("badr"):
        profile_path = ch.health_store_path()
    assert default_path == profile_path == real_store / "state" / "connection_health.json"


def test_a_fault_observed_under_a_named_profile_is_visible_from_the_default_scope(real_store):
    """The incident, exactly: `badr` observed it, the Connections projection must see it."""
    from hermes_cli.connections import scope_context
    with scope_context("badr"):
        ch.record_fault(_revoked("claude-code"))
    with scope_context():
        fault = ch.observed_fault("claude-code")
    assert fault and fault["reason_code"] == ch.REASON_REVOKED


def test_a_fault_recorded_from_the_default_scope_is_visible_inside_a_named_profile(real_store):
    from hermes_cli.connections import scope_context
    with scope_context():
        ch.record_fault(_revoked("claude-code"))
    with scope_context("badr"):
        assert ch.observed_fault("claude-code")


def test_a_runtime_launched_as_a_profile_records_the_global_row(real_store, monkeypatch):
    """The real VPS topology: HERMES_HOME is the profile dir, not a context override."""
    monkeypatch.setenv("HERMES_HOME", str(real_store / "profiles" / "badr"))
    assert ch.current_profile_scope() == "badr"
    # A singleton-backed grant is shared by every profile, so its fault belongs to the global row.
    assert ch.owning_scope("claude-code") == ch.GLOBAL_SCOPE
    assert ch.health_store_path() == real_store / "state" / "connection_health.json"
    ch.record_fault(_revoked("claude-code"))
    monkeypatch.setenv("HERMES_HOME", str(real_store))
    assert ch.observed_fault("claude-code")


def test_a_profile_that_owns_its_credential_keeps_the_fault_scoped_to_itself(real_store, monkeypatch):
    """The other half of the contract: a genuinely profile-local credential must not mark the
    global row — 15 other profiles are still fine."""
    import json
    profile = real_store / "profiles" / "badr"
    (profile / "auth.json").write_text(json.dumps(
        {"providers": {}, "credential_pool": {"openrouter": [{"api_key": "x"}]}}), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    assert ch.owning_scope("openrouter") == "badr"
    block = ch.build_connection_block(provider="openrouter", reason_code=ch.REASON_INVALID).to_dict()
    ch.record_fault(block)
    assert ch.observed_fault("openrouter", "badr")
    assert ch.observed_fault("openrouter") is None  # the global row is untouched


def test_a_profile_without_its_own_credential_falls_back_to_the_global_row(real_store, monkeypatch):
    """build_inventory renders no profile row when the profile has no local credential, so a
    profile-scoped fault there would address a row nothing draws."""
    monkeypatch.setenv("HERMES_HOME", str(real_store / "profiles" / "badr"))
    assert ch.owning_scope("openrouter") == ch.GLOBAL_SCOPE


def test_clearing_from_the_default_scope_clears_a_profile_observed_fault(real_store):
    """Record and clear must resolve the same key, or a reconnected account stays broken forever."""
    from hermes_cli.connections import scope_context
    with scope_context("badr"):
        ch.record_fault(_revoked("claude-code"))
    with scope_context():
        ch.record_healthy("claude-code")
    with scope_context("badr"):
        assert ch.observed_fault("claude-code") is None


def test_the_canonical_store_is_owner_readable_only(real_store):
    ch.record_fault(_revoked("claude-code"))
    path = real_store / "state" / "connection_health.json"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert not list(path.parent.glob("connection_health.json.*"))  # no temp file left behind


def test_the_canonical_store_never_lands_under_a_profile(real_store):
    from hermes_cli.connections import scope_context
    with scope_context("badr"):
        ch.record_fault(_revoked("claude-code"))
    assert not list((real_store / "profiles").rglob("connection_health.json"))


# ── The terminal turn result ──


def _auth_classified(reason=FailoverReason.auth):
    return ClassifiedError(reason=reason, status_code=401, provider="anthropic",
                           model="claude-opus-5", retryable=True)


def test_terminal_auth_result_replaces_the_raw_401_answer(health_home):
    from agent.conversation_loop import _connection_failure_result
    raw = "HTTP 401: OAuth access token has been revoked."
    result = _connection_failure_result(
        classified=_auth_classified(), summary=raw, messages=[], api_call_count=1,
        provider="anthropic", base_url="https://api.anthropic.com", model="claude-opus-5",
        status_code=401,
    )
    # The Owner-visible field carries no provider prose...
    assert result["final_response"] != raw
    for marker in _LEAK_MARKERS:
        assert marker not in result["final_response"]
    # ...while the diagnostic field keeps the evidence for logs and support.
    assert result["error"] == raw
    assert result["connection_block"]["reason_code"] == ch.REASON_REVOKED
    assert result["failure_retryable"] is False
    assert result["failed"] is True and result["completed"] is False


def test_terminal_auth_result_is_needs_owner_not_done(health_home):
    """5D: the business task is blocked on the Owner, never silently Done."""
    from agent.conversation_loop import _connection_failure_result
    result = _connection_failure_result(
        classified=_auth_classified(), summary="HTTP 401: OAuth access token has been revoked.",
        messages=[], api_call_count=1, provider="anthropic",
        base_url="https://api.anthropic.com", model="claude-opus-5", status_code=401,
    )
    assert result["needs_owner"] is True
    assert result["completed"] is False
    assert "reconnect" in result["needs_owner_reason"].lower()
    assert "401" not in result["needs_owner_reason"]


def test_terminal_auth_result_publishes_to_the_health_store(health_home):
    from agent.conversation_loop import _connection_failure_result
    import agent.anthropic_credentials as ac
    ac.read_claude_code_credentials.cache_clear() if hasattr(ac.read_claude_code_credentials, "cache_clear") else None
    _connection_failure_result(
        classified=_auth_classified(), summary="HTTP 401: OAuth access token has been revoked.",
        messages=[], api_call_count=1, provider="anthropic",
        base_url="https://api.anthropic.com", model="claude-opus-5", status_code=401,
    )
    # Whichever row the resolver names, the runtime's verdict is now visible to Connections.
    assert ch.observed_fault("claude-code") or ch.observed_fault("anthropic")


# ── Both terminal paths route here (the regression that produced the defect) ──


class _FakeAgent:
    log_prefix = ""
    provider = "anthropic"
    model = "claude-opus-5"

    def __init__(self):
        self.emitted = []
        self.verbose_lines = []

    def _dump_api_request_debug(self, *a, **k):
        pass

    def _flush_status_buffer(self):
        pass

    def _emit_status(self, text):
        self.emitted.append(text)

    def _summarize_api_error(self, error):
        return "HTTP 401: OAuth access token has been revoked."

    def _persist_session(self, *a, **k):
        pass


def test_nonretryable_path_returns_the_connection_verdict(health_home, monkeypatch):
    from agent import turn_recovery
    monkeypatch.setattr(turn_recovery, "_vlines", lambda *a, **k: None)
    monkeypatch.setattr(turn_recovery, "_print_nonretryable_auth_guidance", lambda *a, **k: None)
    result = turn_recovery.nonretryable_client_error_result(
        _FakeAgent(), RuntimeError("401"), _auth_classified(), status_code=401, api_kwargs=None,
        api_messages=[], messages=[], conversation_history=None, api_call_count=1, approx_tokens=10,
        provider="anthropic", base_url="https://api.anthropic.com", model="claude-opus-5",
    )
    assert result["connection_block"]["reason_code"] == ch.REASON_REVOKED
    assert "401" not in result["final_response"]


def test_max_retries_path_returns_the_same_connection_verdict(health_home, monkeypatch):
    from agent import turn_recovery
    monkeypatch.setattr(turn_recovery, "_vlines", lambda *a, **k: None)
    monkeypatch.setattr(turn_recovery, "is_thinking_timeout", lambda *a, **k: False)
    result = turn_recovery.max_retries_exhausted_result(
        _FakeAgent(), RuntimeError("401"), _auth_classified(FailoverReason.auth_permanent),
        max_retries=3, is_rate_limited=False, error_msg="401", api_kwargs=None, api_messages=[],
        messages=[], conversation_history=None, api_call_count=1, approx_tokens=10,
        provider="anthropic", base_url="https://api.anthropic.com", model="claude-opus-5",
    )
    assert result["connection_block"]["reason_code"] == ch.REASON_REVOKED
    assert "401" not in result["final_response"]
    assert result["needs_owner"] is True


def test_billing_and_content_policy_paths_are_untouched(health_home, monkeypatch):
    """Regression: the auth branch must not swallow the other classified terminal verdicts."""
    from agent import turn_recovery
    monkeypatch.setattr(turn_recovery, "_vlines", lambda *a, **k: None)
    monkeypatch.setattr(turn_recovery, "_print_nonretryable_auth_guidance", lambda *a, **k: None)
    billing = ClassifiedError(reason=FailoverReason.billing, status_code=402, provider="anthropic",
                              model="claude-opus-5", retryable=False)
    result = turn_recovery.nonretryable_client_error_result(
        _FakeAgent(), RuntimeError("402"), billing, status_code=402, api_kwargs=None,
        api_messages=[], messages=[], conversation_history=None, api_call_count=1, approx_tokens=10,
        provider="anthropic", base_url="https://api.anthropic.com", model="claude-opus-5",
    )
    assert result.get("billing_block") is not None
    assert result.get("connection_block") is None


def test_non_auth_non_billing_failure_still_returns_the_plain_result(health_home, monkeypatch):
    from agent import turn_recovery
    monkeypatch.setattr(turn_recovery, "_vlines", lambda *a, **k: None)
    other = ClassifiedError(reason=FailoverReason.format_error, status_code=400, provider="anthropic",
                            model="claude-opus-5", retryable=False)
    result = turn_recovery.nonretryable_client_error_result(
        _FakeAgent(), RuntimeError("400"), other, status_code=400, api_kwargs=None,
        api_messages=[], messages=[], conversation_history=None, api_call_count=1, approx_tokens=10,
        provider="anthropic", base_url="https://api.anthropic.com", model="claude-opus-5",
    )
    assert result.get("connection_block") is None and result.get("billing_block") is None


# ── error_surface parity (the advisory descriptor clients already read) ──


def test_error_surface_reports_the_auth_layer_as_non_retryable(health_home):
    from agent.conversation_loop import _connection_failure_result
    from agent.error_surface import build_error_surface_from_result
    result = _connection_failure_result(
        classified=_auth_classified(), summary="HTTP 401: OAuth access token has been revoked.",
        messages=[], api_call_count=1, provider="anthropic",
        base_url="https://api.anthropic.com", model="claude-opus-5", status_code=401,
    )
    surface = build_error_surface_from_result(result, provider="anthropic", model="claude-opus-5")
    assert surface["layer"] == "auth"
    assert surface["retryable"] is False


# ── Task semantics (5D) ──


def test_a_connection_fault_releases_the_task_instead_of_failing_it(health_home, monkeypatch, tmp_path):
    """A revoked credential fails every attempt identically. Counting those would burn the task's
    whole retry budget on a problem no retry can fix — and marking it Done would be a lie.

    The Owner-gated release does NOT share the quota sentinel: a quota window reopens on its own,
    so releasing it onto a cooldown timer is right, while an Owner-gated fault persists until the
    Owner re-consents. See ``test_owner_gated_release_is_not_the_quota_sentinel``.
    """
    from agent.conversation_loop import _connection_failure_result
    from hermes_cli.kanban_db import KANBAN_NEEDS_OWNER_EXIT_CODE, KANBAN_RATE_LIMIT_EXIT_CODE

    result = _connection_failure_result(
        classified=_auth_classified(), summary="HTTP 401: OAuth access token has been revoked.",
        messages=[], api_call_count=1, provider="anthropic",
        base_url="https://api.anthropic.com", model="claude-opus-5", status_code=401,
    )
    # The turn is failed-but-owner-gated, which is what the worker branches on.
    assert result["failed"] is True and result["completed"] is False
    assert result["needs_owner"] is True

    # Mirror of the worker's exit-code decision in cli.py (kept in lockstep by this assertion).
    def worker_exit_code(res, kanban: bool):
        if not (isinstance(res, dict) and res.get("failed")):
            return 0
        if kanban and (res.get("needs_owner") or res.get("failure_reason") in ("rate_limit", "billing")):
            return KANBAN_NEEDS_OWNER_EXIT_CODE if res.get("needs_owner") else KANBAN_RATE_LIMIT_EXIT_CODE
        return 1

    assert worker_exit_code(result, kanban=True) == KANBAN_NEEDS_OWNER_EXIT_CODE
    # Outside a kanban worker nothing changes: it is still an ordinary failed turn.
    assert worker_exit_code(result, kanban=False) == 1
    # And an ordinary failure is untouched by the new branch.
    assert worker_exit_code({"failed": True, "failure_reason": "format_error"}, kanban=True) == 1
    # A quota wall keeps the quota sentinel.
    assert worker_exit_code({"failed": True, "failure_reason": "rate_limit"}, kanban=True) == KANBAN_RATE_LIMIT_EXIT_CODE


def test_the_worker_branch_matches_the_source(health_home):
    """Pins the mirrored condition above to the real one in cli.py."""
    from pathlib import Path
    source = Path(__file__).resolve().parents[2] / "cli.py"
    text = source.read_text(encoding="utf-8")
    assert 'result.get("needs_owner") or result.get("failure_reason") in ("rate_limit", "billing")' in text
    assert '_OWNER_CODE if result.get("needs_owner") else _RL_CODE' in text
