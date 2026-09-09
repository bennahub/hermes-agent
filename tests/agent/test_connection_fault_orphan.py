"""A Connection fault must never outlive the agent it was recorded against.

The composition defect these pin. Two lanes are individually right and wrong together:
``tools.mcp_tool_errors`` records an MCP fault under ``current_profile_scope()`` unconditionally —
correct, an MCP grant lives in ``HERMES_HOME/mcp-tokens/`` and belongs to the serving profile — and
``connection_health.record_healthy`` pops exactly one key, resolving the scope the way
``record_fault`` did. Delete that profile and the two no longer meet:

  * ``owning_scope`` falls back to ``default`` the moment ``profiles/<name>/auth.json`` is gone;
  * the only clearer for an MCP row is a successful call *running as that profile*;
  * ``delete_profile`` never touched the health store.

So ``badr/acme-mcp`` survived every recovery route there is, stayed invisible to ``observed_fault``
(which reads the ``default`` key), and — because the respawn guard is scope-agnostic and answers
``bool(faults)`` for a task with no stamped connection — held EVERY unstamped ``needs_owner`` task
out of dispatch, permanently, with no Owner-visible cause and nothing in the product to press.

That breaks the Task Completion Guarantee and makes "Needs You truthful" false, so the four things
below are pinned separately: the delete leaves nothing behind, an orphan that got in some other way
is still clearable, the dispatch guard cannot be held by an agent that is gone, and a LIVE profile's
fault is honoured exactly as it was.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from agent import connection_health as ch


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A disposable deployment root: the real store resolution, real profile directories."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    hermes_home = tmp_path / ".hermes"
    (hermes_home / "profiles").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    ch._invalidate_no_faults()
    return hermes_home


def _profile(root: Path, name: str) -> Path:
    profile_dir = root / "profiles" / name
    (profile_dir / "state").mkdir(parents=True, exist_ok=True)
    (profile_dir / "config.yaml").write_text("{}\n", encoding="utf-8")
    (profile_dir / "auth.json").write_text('{"providers": {}, "credential_pool": {}}',
                                           encoding="utf-8")
    return profile_dir


def _mcp_fault(root: Path, profile: str, connection_id: str = "acme-mcp") -> None:
    """Record a fault exactly as ``mcp_tool_errors`` does: scope = the serving profile."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(str(root / "profiles" / profile))
    try:
        assert ch.current_profile_scope() == profile
        block = ch.build_connection_block(
            provider=connection_id, reason_code=ch.REASON_REVOKED).to_dict()
        block["connection_id"] = connection_id
        ch.record_fault(block, scope=ch.current_profile_scope())
    finally:
        reset_hermes_home_override(token)


def _raw_keys(root: Path) -> list[str]:
    """What is physically in the store — not what a reader is willing to see."""
    import json

    path = root / "state" / "connection_health.json"
    if not path.exists():
        return []
    return sorted(json.loads(path.read_text(encoding="utf-8"))["faults"])


# ── (a) the delete leaves nothing behind ──


def test_deleting_a_profile_with_an_open_mcp_fault_leaves_no_row(root):
    """The whole chain, end to end, through the real lifecycle."""
    from hermes_cli.profiles import create_profile, delete_profile

    create_profile("badr", no_alias=True, no_skills=True)
    _mcp_fault(root, "badr")
    assert _raw_keys(root) == ["badr/acme-mcp"]

    with patch("hermes_cli.profiles._cleanup_gateway_service"), \
            patch("hermes_cli.profiles._stop_profile_backends"):
        delete_profile("badr", yes=True)

    assert _raw_keys(root) == []
    assert ch.open_faults() == {}


# ── (b) an orphan that arrived some other way is still clearable ──


def test_an_orphaned_row_is_cleared_by_an_ordinary_recovery(root):
    """A profile can also go without ``delete_profile`` — a hand ``rm -rf``, a restore from a
    backup taken before it existed, a delete that got as far as the tombstone and then failed. The
    recovery contract has to be total, so the next good turn on ANY connection sweeps it."""
    import shutil

    _profile(root, "badr")
    _profile(root, "joud")
    _mcp_fault(root, "badr")
    shutil.rmtree(root / "profiles" / "badr")
    assert _raw_keys(root) == ["badr/acme-mcp"]

    ch.record_healthy("some-other-connection")

    assert _raw_keys(root) == []


def test_every_recovery_route_reaches_an_orphan(root):
    """The four routes the incident tried, each on its own. Before the fix all four left the row
    exactly where it was, which is what made it permanent."""
    import shutil

    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    def _orphan():
        _profile(root, "badr")
        _mcp_fault(root, "badr")
        shutil.rmtree(root / "profiles" / "badr")
        ch._invalidate_no_faults()
        assert _raw_keys(root) == ["badr/acme-mcp"]

    def _as_other_profile():
        _profile(root, "joud")
        token = set_hermes_home_override(str(root / "profiles" / "joud"))
        try:
            ch.record_healthy("acme-mcp", ch.current_profile_scope())
        finally:
            reset_hermes_home_override(token)

    for route in (lambda: ch.record_healthy("acme-mcp"),
                  lambda: ch.note_connection_healthy("acme-mcp"),
                  lambda: ch.record_healthy("acme-mcp", scope="default"),
                  _as_other_profile):
        _orphan()
        route()
        assert _raw_keys(root) == [], f"{route} left the orphan behind"


def test_observed_fault_never_reported_the_orphan_and_still_does_not(root):
    """It never could: the row is keyed ``badr/…`` and Connections reads ``default/…``. Pinned so
    the sweep is not mistaken for the thing that made it invisible — that was always true, and is
    exactly why an orphan had no Owner-visible cause."""
    import shutil

    _profile(root, "badr")
    _mcp_fault(root, "badr")
    shutil.rmtree(root / "profiles" / "badr")

    assert ch.observed_fault("acme-mcp") is None


# ── (c) the dispatch guard cannot be held open by an agent that is gone ──


def test_the_respawn_guard_is_not_held_by_a_deleted_profile(root):
    """Both fallbacks. A task stamped with that connection, and — the wider harm — every task with
    no stamp at all, which the guard holds while ANY fault is open."""
    import shutil

    from hermes_cli import kanban_db_dispatch as dispatch

    _profile(root, "badr")
    _mcp_fault(root, "badr")
    shutil.rmtree(root / "profiles" / "badr")
    assert _raw_keys(root) == ["badr/acme-mcp"]

    blocks = {"stamped": {"connection_id": "acme-mcp"}, "unstamped": {}}
    with patch.object(dispatch._kb, "latest_needs_owner_block",
                      lambda conn, task_id: blocks[task_id]):
        assert dispatch._connection_fault_still_open(None, "stamped") is False
        assert dispatch._connection_fault_still_open(None, "unstamped") is False


# ── (d) a LIVE profile's fault is honoured exactly as today ──


def test_a_live_profiles_fault_still_holds_and_still_needs_its_own_recovery(root):
    """The sweep may not become a way to clear a real fault from the wrong scope."""
    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    _profile(root, "badr")
    _profile(root, "joud")
    _mcp_fault(root, "badr")

    # A good turn as another profile, and a default-scope clear, both leave it alone.
    token = set_hermes_home_override(str(root / "profiles" / "joud"))
    try:
        ch.record_healthy("acme-mcp", ch.current_profile_scope())
    finally:
        reset_hermes_home_override(token)
    ch.record_healthy("acme-mcp", scope="default")
    assert _raw_keys(root) == ["badr/acme-mcp"]

    # It is still open state: the guard holds, and the fault is readable in its own scope.
    with patch.object(dispatch._kb, "latest_needs_owner_block",
                      lambda conn, task_id: {"connection_id": "acme-mcp"}):
        assert dispatch._connection_fault_still_open(None, "t") is True
    assert ch.observed_fault("acme-mcp", scope="badr")["reason_code"] == ch.REASON_REVOKED

    # And only ``badr``'s own recovery clears it.
    token = set_hermes_home_override(str(root / "profiles" / "badr"))
    try:
        ch.record_healthy("acme-mcp", ch.current_profile_scope())
    finally:
        reset_hermes_home_override(token)
    assert _raw_keys(root) == []


def test_an_unreadable_profiles_root_keeps_every_row(root):
    """Fail-safe in the KEEP direction. An over-visible fault is a reconnect prompt the Owner can
    dismiss; a wrongly-dropped one is a broken connection reading healthy, which is the outage this
    module exists to prevent — so every uncertainty answers 'live'."""
    _profile(root, "badr")
    _mcp_fault(root, "badr")

    def _boom(_name):
        raise OSError("profiles root unreadable")

    with patch("hermes_cli.profiles.profile_exists", _boom):
        assert ch.open_faults() and "badr/acme-mcp" in ch.open_faults()
        ch.record_healthy("acme-mcp", scope="default")

    assert _raw_keys(root) == ["badr/acme-mcp"]


def test_the_global_scope_is_never_treated_as_an_orphan(root):
    """``default`` is the row Connections always renders and has no profile directory of its own."""
    block = ch.build_connection_block(
        provider="claude-code", reason_code=ch.REASON_REVOKED).to_dict()
    block["connection_id"] = "claude-code"
    ch.record_fault(block, scope="default")

    assert "default/claude-code" in ch.open_faults()
    assert ch.observed_fault("claude-code")["reason_code"] == ch.REASON_REVOKED


def test_forget_scope_takes_only_that_scopes_rows(root):
    """The delete-time prune is narrow: one agent's rows, never another's and never the global."""
    _profile(root, "badr")
    _profile(root, "joud")
    _mcp_fault(root, "badr", "acme-mcp")
    _mcp_fault(root, "joud", "acme-mcp")
    _mcp_fault(root, "badr", "other-mcp")
    assert _raw_keys(root) == ["badr/acme-mcp", "badr/other-mcp", "joud/acme-mcp"]

    assert ch.forget_scope("badr") == 2

    assert _raw_keys(root) == ["joud/acme-mcp"]
    assert ch.forget_scope("default") == 0
