"""One canonical Connection state, and an Owner-gated block that actually blocks.

Three defects these pin, all of the same shape — a surface answering "is this connection working?"
from something that structurally cannot know:

* ``/api/providers/oauth`` built its Accounts card from local metadata alone, so it reported a
  revoked grant as signed in while ``/api/connections`` (which carries the health overlay) said it
  needed re-auth. Two owner-facing REST surfaces, one credential, opposite answers.
* A kanban task blocked on an Owner-gated Connection was released on the QUOTA sentinel, so the
  dispatcher held it on a quota-shaped cooldown and then re-dispatched into a turn that could not
  succeed — for as long as the fault lasted, which on the incident was 44 hours — replaying from
  step one over side effects the previous attempt had already committed.
* ``needs_owner`` on a turn result reached exactly one consumer (that exit code). Nothing set the
  autonomy state Needs You renders, so a task could be blocked on the Owner with nothing telling
  the Owner so.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent import connection_health as ch
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture()
def health_home(tmp_path, monkeypatch):
    monkeypatch.setattr(ch, "health_store_path", lambda: tmp_path / "state" / "connection_health.json")
    return tmp_path


def _record_revoked(connection_id, scope="default"):
    block = ch.build_connection_block(
        provider=connection_id, connection_id=connection_id, provider_label=connection_id,
        reason_code=ch.REASON_REVOKED, scope=scope).to_dict()
    ch.record_fault(block, scope=scope)
    return block


# ── P1-2: the two owner-facing REST surfaces agree ──


def test_the_accounts_card_reports_the_runtime_verdict(health_home):
    """It said "signed in" for the whole outage because the token file still looked perfect."""
    from hermes_cli.web_routers import oauth
    _record_revoked("claude-code")
    status = oauth._resolve_provider_status(
        "claude-code", lambda: {"logged_in": True, "source": "claude_code_cli",
                                "source_label": "~/.claude/.credentials.json"})
    assert status["logged_in"] is False
    assert status["detail_code"] == ch.REASON_REVOKED
    assert status["owner_action"] == ch.ACTION_REAUTHORIZE
    assert status["connection_state"] == "needs_auth"
    assert status["retryable"] is False


def test_the_accounts_card_keeps_what_disconnect_needs(health_home):
    """A revoked credential still has to be findable and removable; ``source`` gates disconnect."""
    from hermes_cli.web_routers import oauth
    _record_revoked("claude-code")
    status = oauth._resolve_provider_status(
        "claude-code", lambda: {"logged_in": True, "source": "env_var", "source_label": "X"})
    assert status["source"] == "env_var" and status["source_label"] == "X"


def test_a_healthy_account_card_is_untouched(health_home):
    from hermes_cli.web_routers import oauth
    status = oauth._resolve_provider_status("claude-code", lambda: {"logged_in": True, "source": "s"})
    assert status == {"logged_in": True, "source": "s"}


def test_both_surfaces_read_one_lookup(health_home):
    """The point is not two agreeing implementations — it is one implementation."""
    from hermes_cli import connections as conns
    _record_revoked("claude-code")
    assert conns.canonical_fault("claude-code")["reason_code"] == ch.REASON_REVOKED
    entry = conns.apply_observed_fault(
        conns.row("claude-code", "account", "Claude Code", "configured", "default"))
    assert entry["state"] == "needs_auth" and "reconnect" in entry["actions"]


# ── MCP is a Connection of the same shape ──


def test_the_overlay_reaches_an_mcp_row(health_home):
    """``_oauth_tokens_present`` sees a file; a revoked grant leaves the file exactly where it was."""
    from hermes_cli import connections as conns
    _record_revoked("atlassian")
    entry = conns.apply_observed_fault(
        conns.row("atlassian", "mcp", "atlassian", "configured", "default", actions=["test"]))
    assert entry["state"] == "needs_auth"
    assert entry["detail_code"] == ch.REASON_REVOKED
    assert entry["owner_action"] == ch.ACTION_REAUTHORIZE
    assert "reconnect" in entry["actions"]


def test_a_disabled_mcp_row_is_not_asked_to_reconnect(health_home):
    from hermes_cli import connections as conns
    _record_revoked("atlassian")
    entry = conns.apply_observed_fault(
        conns.row("atlassian", "mcp", "atlassian", "disabled", "default", actions=["enable"]))
    assert entry["state"] == "disabled" and "reconnect" not in entry["actions"]


def test_the_mcp_row_id_is_not_normalised(health_home):
    """``build_inventory`` keys the row by the configured name verbatim; so must the fault."""
    from tools.mcp_tool_errors import mcp_connection_block, mcp_connection_id
    assert mcp_connection_id("Atlassian-Prod") == "Atlassian-Prod"
    block = mcp_connection_block("Atlassian-Prod", RuntimeError("HTTP 401 Unauthorized"))
    assert block["connection_id"] == "Atlassian-Prod"


@pytest.mark.parametrize("exc,expected", [
    (RuntimeError("Server responded 401 Unauthorized"), 401),
    (RuntimeError("403 Forbidden: insufficient scope"), 403),
    (RuntimeError("{'error': 'invalid_token'}"), 401),
    (RuntimeError("upstream returned status_code=401"), 401),
])
def test_a_401_the_type_gate_misses_is_still_a_connection_fault(exc, expected):
    """Every one of these used to reach the model as the provider's own words."""
    from tools.mcp_tool_errors import auth_status_code, is_connection_auth_failure
    assert auth_status_code(exc) == expected
    assert is_connection_auth_failure(exc) is True


def test_a_wrapped_401_is_found_through_the_cause_chain():
    from tools.mcp_tool_errors import is_connection_auth_failure
    try:
        try:
            raise RuntimeError("HTTP 401 Unauthorized")
        except RuntimeError as inner:
            raise RuntimeError("MCP request failed") from inner
    except RuntimeError as outer:
        assert is_connection_auth_failure(outer) is True


def test_a_session_expiry_is_not_an_owner_gated_fault():
    """Hermes reconnects this itself; a reconnect prompt here would be the mirror defect."""
    from tools.mcp_tool_errors import is_connection_auth_failure
    assert is_connection_auth_failure(RuntimeError("Invalid or expired session")) is False


def test_an_ordinary_tool_failure_is_not_a_connection_fault():
    from tools.mcp_tool_errors import is_connection_auth_failure
    assert is_connection_auth_failure(ValueError("issue key must not be empty")) is False


def test_the_model_never_receives_the_providers_words(health_home):
    """The invariant: no raw provider prose is an ordinary answer."""
    import json
    from tools.mcp_tool_errors import mcp_connection_tool_error
    prose = ("401 Unauthorized: Your Atlassian session has ended. Please sign in again at "
             "https://id.atlassian.com and re-issue an API token for site bennahub.")
    payload, block = mcp_connection_tool_error("atlassian", RuntimeError(prose))
    assert payload is not None
    body = json.loads(payload)
    assert "Atlassian session has ended" not in body["error"]
    assert "id.atlassian.com" not in json.dumps(body, ensure_ascii=False)
    assert body["connection"]["reason_code"] and body["retryable"] is False
    assert body["needs_owner"] is True
    assert block["owner_action"] in (ch.ACTION_REAUTHORIZE, ch.ACTION_CONFIGURE)


def test_the_model_is_not_handed_operator_instructions(health_home):
    """It cannot run them, and the Owner reads them as the agent asking for a terminal."""
    import json
    from tools import mcp_tool_handlers as handlers
    from tools.mcp_tool_errors import mcp_connection_tool_error
    payload, _block = mcp_connection_tool_error("atlassian", RuntimeError("HTTP 401 Unauthorized"))
    text = json.loads(payload)["error"]
    for forbidden in ("hermes mcp login", "mcp-tokens", "~/.hermes"):
        assert forbidden not in text
    assert forbidden not in handlers._NEEDS_REAUTH_MSG.format(s="atlassian")


def test_the_mcp_fault_is_published_to_the_canonical_store(health_home):
    from tools.mcp_tool_errors import record_mcp_connection_fault
    record_mcp_connection_fault("atlassian", RuntimeError("HTTP 401 Unauthorized"))
    assert ch.observed_fault("atlassian", ch.current_profile_scope())


def test_a_working_mcp_call_clears_the_fault(health_home):
    """Recovery is not optional: without it a reconnected server stays broken forever."""
    from tools import mcp_tool
    from tools.mcp_tool_errors import record_mcp_connection_fault
    record_mcp_connection_fault("atlassian", RuntimeError("HTTP 401 Unauthorized"))
    assert ch.observed_fault("atlassian", ch.current_profile_scope())
    mcp_tool._reset_server_error("atlassian")
    assert ch.observed_fault("atlassian", ch.current_profile_scope()) is None


# ── P1-3: the Owner-gated release is not a quota wall ──


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ch, "health_store_path", lambda: home / "state" / "connection_health.json")
    kb.init_db()
    return home


def _exited_status(code: int) -> int:
    return (code & 0xFF) << 8


def test_the_owner_gated_exit_is_its_own_sentinel():
    assert kb.KANBAN_NEEDS_OWNER_EXIT_CODE != kb.KANBAN_RATE_LIMIT_EXIT_CODE


def test_the_dispatcher_tells_the_two_walls_apart(kanban_home):
    kbd._record_worker_exit(80001, _exited_status(kb.KANBAN_NEEDS_OWNER_EXIT_CODE))
    kbd._record_worker_exit(80002, _exited_status(kb.KANBAN_RATE_LIMIT_EXIT_CODE))
    assert kbd._classify_worker_exit(80001)[0] == "needs_owner"
    assert kbd._classify_worker_exit(80002)[0] == "rate_limited"
    owner = kbd._classify_dead_worker(80001, "host:w")
    assert owner.needs_owner is True and owner.run_outcome == "needs_owner"
    assert owner.released_without_failure is True
    assert "verify what already happened" in owner.error_text.lower()


def test_an_owner_gated_exit_releases_without_counting_a_failure(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    with kbc.connect() as conn:
        host = kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="owner-gated", assignee="a")
        kb.claim_task(conn, tid, claimer=f"{host}:w0")
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (80100, tid))
        conn.commit()
        kbd._record_worker_exit(80100, _exited_status(kb.KANBAN_NEEDS_OWNER_EXIT_CODE))
        crashed = kbd.detect_crashed_workers(conn)
        assert tid not in crashed
        assert tid in getattr(kbd.detect_crashed_workers, "_last_needs_owner", [])
        task = kb.get_task(conn, tid)
        assert task.status == "ready" and task.consecutive_failures == 0
        outcomes = [r["outcome"] for r in conn.execute(
            "SELECT outcome FROM task_runs WHERE task_id=?", (tid,)).fetchall()]
        assert outcomes == ["needs_owner"]


def _seed_owner_gated_run(conn, *, connection_id="claude-code", api_calls=16):
    tid = kb.create_task(conn, title="blocked", assignee="a")
    kb.claim_task(conn, tid)
    with kb.write_txn(conn, allow_nested=True):
        kb.record_needs_owner_block(
            conn, tid, connection_id=connection_id, reason_code=ch.REASON_REVOKED,
            owner_action=ch.ACTION_REAUTHORIZE, api_calls=api_calls,
            reason="The Claude Code connection is disconnected.")
    run_id = kb.get_task(conn, tid).current_run_id
    conn.execute("UPDATE task_runs SET outcome='needs_owner', status='needs_owner', ended_at=? "
                 "WHERE id=?", (5_000_000, run_id))
    conn.execute("UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, "
                 "claim_expires=NULL, worker_pid=NULL, last_failure_error=? WHERE id=?",
                 ("pid 1 stopped on a disconnected Connection", tid))
    conn.commit()
    return tid


def test_the_task_is_held_while_the_connection_is_still_broken(kanban_home):
    """A quota cooldown would have re-dispatched into a doomed turn every few minutes for days."""
    with kbc.connect() as conn:
        tid = _seed_owner_gated_run(conn)
        _record_revoked("claude-code")
        assert kbd.check_respawn_guard(conn, tid) == "connection_fault"


def test_the_task_is_released_the_moment_the_fault_clears(kanban_home):
    """Keyed on the fault, not a clock: reconnecting is what makes it runnable again."""
    with kbc.connect() as conn:
        tid = _seed_owner_gated_run(conn)
        _record_revoked("claude-code")
        assert kbd.check_respawn_guard(conn, tid) == "connection_fault"
        ch.record_healthy("claude-code")
        # And not re-trapped by ``blocker_auth`` on the stamped Owner-gated text.
        assert kbd.check_respawn_guard(conn, tid) is None


def test_a_task_with_no_stamped_connection_holds_while_any_fault_is_open(kanban_home):
    """Fail closed: a doomed re-run's side effects cost more than a delayed dispatch."""
    with kbc.connect() as conn:
        tid = _seed_owner_gated_run(conn, connection_id="")
        _record_revoked("claude-code")
        assert kbd.check_respawn_guard(conn, tid) == "connection_fault"
        ch.record_healthy("claude-code")
        assert kbd.check_respawn_guard(conn, tid) is None


def test_the_next_worker_is_warned_about_replaying_side_effects(kanban_home):
    """The incident turn hit its 401 at API call #17, after sixteen side-effecting calls."""
    with kbc.connect() as conn:
        tid = _seed_owner_gated_run(conn, api_calls=16)
        stamped = kb.latest_needs_owner_block(conn, tid)
        assert stamped["api_calls"] == 16
        assert stamped["connection_id"] == "claude-code"
        assert stamped["reason_code"] == ch.REASON_REVOKED


def test_the_stamp_carries_no_secret(kanban_home):
    import json
    with kbc.connect() as conn:
        tid = _seed_owner_gated_run(conn)
        payload = json.dumps(kb.latest_needs_owner_block(conn, tid), ensure_ascii=False)
        for marker in ("token", "sk-", "Bearer", "401"):
            assert marker not in payload


# ── The NEEDS_OWNER bridge: Needs You tells the truth ──


@pytest.fixture()
def autonomy_home(tmp_path, monkeypatch):
    home = tmp_path / "autonomy-home"
    (home / "autonomy").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _new_work(home, objective="ship the ticket", key=None):
    from agent.autonomy import store
    import uuid
    started = store.start_work(
        why=objective, outcome=objective, done_contract="the ticket exists",
        idempotency_key=key or uuid.uuid4().hex, hermes_home=home,
        objective=objective, state="working")
    return started["work"]


def _start_work(home, objective="ship the ticket"):
    from agent.autonomy import store
    work = _new_work(home, objective)
    return store.update_work(work["id"], hermes_home=home, state="working",
                             refs={"dispatch": {"pid": os.getpid()}})


def test_the_bridge_parks_the_work_this_process_was_advancing(autonomy_home):
    """Before this, the only writer of ``needs_owner`` was a call the model had to choose to make."""
    from agent.autonomy import store
    from agent.autonomy.kernel import block_work_on_connection
    work = _start_work(autonomy_home)
    parked = block_work_on_connection(
        "The Claude Code connection is disconnected. The Owner must reconnect it.",
        hermes_home=autonomy_home)
    assert parked == [work["id"]]
    after = store.get_work(work["id"], autonomy_home)
    assert after["state"] == "needs_owner"
    assert "reconnect" in (after["waiting_reason"] or "").lower()


def test_the_bridge_leaves_another_process_work_alone(autonomy_home):
    """A broken Connection here does not entitle us to park work still progressing elsewhere."""
    from agent.autonomy import store
    from agent.autonomy.kernel import block_work_on_connection
    work = _new_work(autonomy_home, "someone else's")
    store.update_work(work["id"], hermes_home=autonomy_home, state="working",
                      refs={"dispatch": {"pid": os.getpid() + 99999}})
    assert block_work_on_connection("disconnected", hermes_home=autonomy_home) == []
    assert store.get_work(work["id"], autonomy_home)["state"] == "working"


def test_the_bridge_targets_the_named_continuation(autonomy_home):
    """``HERMES_OWNER_CONTINUATION_ID`` is the exact work a continuation turn is advancing."""
    from agent.autonomy import store
    from agent.autonomy.kernel import block_work_on_connection
    work = _new_work(autonomy_home, "continuation")
    assert block_work_on_connection("disconnected", work_id=work["id"],
                                    hermes_home=autonomy_home) == [work["id"]]
    assert store.get_work(work["id"], autonomy_home)["state"] == "needs_owner"


def test_the_bridge_never_raises_on_a_broken_ledger(autonomy_home):
    from agent.autonomy.kernel import block_work_on_connection
    assert block_work_on_connection("disconnected", work_id="no-such-work",
                                    hermes_home=autonomy_home) == []
