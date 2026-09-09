"""``profiles.delete``: the ws door onto the one canonical agent lifecycle.

Deleting an agent from a phone has to be the same act as ``hermes profile delete`` and the
dashboard's ``DELETE /api/profiles/<name>`` — one lifecycle, not three — and it has to reach
the owner's other devices without a relaunch. These tests lock both halves: the handler adds
no deletion semantics of its own, and it announces the change.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import tui_gateway.server as srv
from hermes_cli.profiles import create_profile
from hermes_constants import named_profile_is_deleted


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return hermes_home


@pytest.fixture
def broadcasts(monkeypatch):
    """Capture the global fan-out instead of writing frames at a transport-less server."""
    seen: list[tuple[str, dict | None]] = []
    monkeypatch.setattr(
        srv, "_broadcast_global_event", lambda event, payload=None: seen.append((event, payload)))
    return seen


def _delete(name):
    # The gateway service and stray backends are process-level side effects; the tombstone
    # suite patches them the same way so the test stays about the contract, not about launchd.
    with patch("hermes_cli.profiles._cleanup_gateway_service"), patch(
        "hermes_cli.profiles._stop_profile_backends"
    ):
        return srv._methods["profiles.delete"]("delete", {"name": name})


def _roster() -> list[str]:
    rows = srv._methods["profiles.list"]("list", {"include_sessions": False})["result"]["profiles"]
    return [row["name"] for row in rows]


def test_delete_removes_the_agent_from_the_roster_and_tombstones_its_home(home, broadcasts):
    profile_dir = create_profile("faisal", no_alias=True, no_skills=True)
    assert "faisal" in _roster()

    result = _delete("faisal")["result"]

    assert result["ok"] is True
    assert result["name"] == "faisal"
    assert not profile_dir.exists()
    assert named_profile_is_deleted(profile_dir)
    assert "faisal" not in _roster()


def test_delete_announces_the_change_so_the_owners_other_device_drops_the_row(home, broadcasts):
    create_profile("faisal", no_alias=True, no_skills=True)

    _delete("faisal")

    assert ("profiles.changed", {"deleted": "faisal"}) in broadcasts


def test_delete_refuses_the_default_profile(home, broadcasts):
    result = _delete("default")

    assert result["error"]["code"] == 4068
    assert "default" in result["error"]["message"]
    assert broadcasts == []
    assert "default" in _roster()


def test_delete_reports_an_unknown_agent_as_a_client_error(home, broadcasts):
    result = _delete("never-existed")

    assert result["error"]["code"] == 4068
    assert broadcasts == []


def test_delete_requires_a_name(home, broadcasts):
    result = srv._methods["profiles.delete"]("delete", {"name": "   "})

    assert result["error"]["code"] == 4067
    assert broadcasts == []


def test_a_deleted_agent_does_not_come_back_when_a_stale_process_remakes_its_directory(
    home, broadcasts
):
    """Reconnect/relaunch acceptance: the tombstone, not the client, is what keeps it gone."""
    profile_dir = create_profile("faisal", no_alias=True, no_skills=True)
    _delete("faisal")

    profile_dir.mkdir(parents=True)
    (profile_dir / "logs").mkdir()

    assert "faisal" not in _roster()


def test_deleting_one_agent_leaves_the_others_alone(home, broadcasts):
    create_profile("faisal", no_alias=True, no_skills=True)
    keeper = create_profile("majed", no_alias=True, no_skills=True)

    _delete("faisal")

    roster = _roster()
    assert "faisal" not in roster
    assert "majed" in roster
    assert keeper.is_dir()
    assert not named_profile_is_deleted(keeper)


def test_delete_stops_a_turn_this_gateway_is_running_for_the_deleted_profile(home, broadcasts):
    """The gateway servicing ``profiles.delete`` can be running the deleted profile's OWN turn:
    ``compute_host`` binds that profile's ``HERMES_HOME`` and acquires its ``state.db`` inside
    this process. ``_stop_gateway_process`` knows only ``gateway.pid`` and the backend sweep
    skips this process and its ancestors, so nothing else can see that holder — and left
    running it writes straight through the ``rmtree`` (POSIX) or fails it outright (Windows,
    leaving the profile tombstoned but present)."""
    import threading

    profile_dir = create_profile("faisal", no_alias=True, no_skills=True)
    interrupted: list[str] = []
    session = {"profile_home": str(profile_dir), "running": True,
               "history_lock": threading.Lock(), "session_key": "k1"}

    def _fake_interrupt(sid, sess, *, request_id=None):
        interrupted.append(sid)
        sess["running"] = False  # what a real interrupt achieves, without a live agent
        return True

    with patch.dict(srv._sessions, {"sid-1": session}, clear=False), \
            patch.object(srv, "_interrupt_session_turn", _fake_interrupt, create=True):
        result = _delete("faisal")["result"]

    assert result["ok"] is True
    assert interrupted == ["sid-1"]
    assert not profile_dir.exists()


def test_delete_does_not_touch_a_turn_running_for_a_different_profile(home, broadcasts):
    import threading

    create_profile("faisal", no_alias=True, no_skills=True)
    keeper_dir = create_profile("majed", no_alias=True, no_skills=True)
    interrupted: list[str] = []
    session = {"profile_home": str(keeper_dir), "running": True,
               "history_lock": threading.Lock(), "session_key": "k2"}

    with patch.dict(srv._sessions, {"sid-2": session}, clear=False), \
            patch.object(srv, "_interrupt_session_turn",
                         lambda sid, sess, **kw: interrupted.append(sid), create=True):
        _delete("faisal")

    assert interrupted == []
    assert session["running"] is True


# ── no server filesystem path reaches the Owner or the wire ──────────────────────────────
#
# The canonical lifecycle is a CLI first, and it says what an operator at a shell needs: the
# ``rmtree`` failure names the directory and the raw OS error, and refusing to delete the default
# names ``~/.hermes`` and tells you to run ``hermes uninstall``. Forwarded verbatim those reach an
# Owner holding a phone, who can act on neither — and a server path on the wire is a disclosure
# whatever the client does with it. That the Apple client ignores the payload, and that the REST
# twin already returned ``path``, mitigate; neither makes it right in a handler this wave added.


def _paths_in(frame) -> list[str]:
    """Every filesystem-path-shaped run of text anywhere in a JSON-RPC frame, at any depth."""
    import json
    import re

    return re.findall(r"~/[\w.-]+|(?:/[\w.-]+){2,}|[A-Za-z]:\\[\w\\.-]+", json.dumps(frame))


def test_a_successful_delete_puts_no_path_on_the_wire(home, broadcasts):
    profile_dir = create_profile("faisal", no_alias=True, no_skills=True)

    frame = _delete("faisal")

    assert frame["result"] == {"ok": True, "name": "faisal"}
    assert _paths_in(frame) == []
    assert str(profile_dir) not in str(frame)


def test_a_failed_delete_reports_the_state_without_the_paths(home, broadcasts, caplog):
    """The ``rmtree`` failure is the one that carries two absolute paths and a raw OS error."""
    import logging

    create_profile("faisal", no_alias=True, no_skills=True)
    boom = RuntimeError(
        f"Could not remove profile directory {home / 'profiles' / 'faisal'}: "
        f"[Errno 39] Directory not empty. 'faisal' is tombstoned but still on disk; remove "
        f"{home / 'profiles' / 'faisal'} by hand before reusing the name.")

    with caplog.at_level(logging.WARNING), \
            patch("hermes_cli.profiles.delete_profile", side_effect=boom):
        frame = srv._methods["profiles.delete"]("delete", {"name": "faisal"})

    assert frame["error"]["code"] == 5067
    assert _paths_in(frame) == []
    assert "Errno 39" not in frame["error"]["message"]
    # Actionable without them: which agent, what state it is in, where the rest of it went.
    assert "faisal" in frame["error"]["message"]
    assert "server log" in frame["error"]["message"]
    assert "Errno 39" in caplog.text  # the operator-facing detail is relocated, not lost
    assert broadcasts == []


def test_refusing_the_default_agent_sends_no_path_and_no_cli_prose(home, broadcasts):
    """The CLI's refusal names ``~/.hermes`` and says to run ``hermes uninstall``. A phone has no
    shell to run it in."""
    frame = _delete("default")

    assert frame["error"]["code"] == 4068
    assert _paths_in(frame) == []
    assert "hermes uninstall" not in frame["error"]["message"]
    assert "default" in frame["error"]["message"]
    assert "default" in _roster()


def test_an_unknown_agent_still_names_only_what_the_client_sent(home, broadcasts):
    frame = _delete("never-existed")

    assert frame["error"]["code"] == 4068
    assert _paths_in(frame) == []
