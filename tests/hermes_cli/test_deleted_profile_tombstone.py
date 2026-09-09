"""Deleted named profiles must stay gone until explicitly recreated.

A live serve/logging process can mkdir ``profiles/<name>/logs`` after
``hermes profile delete`` removes the tree. That empty shell then
reappears in ``hermes profile list`` and Desktop Bot Mode. These tests
lock the tombstone + no-mkdir contract without depending on Desktop.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli.config import ensure_hermes_home
from hermes_cli.profiles import (
    backfill_profile_envs,
    create_profile,
    delete_profile,
    list_profiles,
    profile_exists,
    profiles_to_serve,
    resolve_profile_env,
    set_active_profile,
)
from hermes_constants import named_profile_home
from hermes_logging import setup_logging


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_home = tmp_path / ".hermes"
    default_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    return tmp_path


def _named_homes(tmp_path: Path) -> list[str]:
    return [info.name for info in list_profiles() if not info.is_default]


def _delete(name: str) -> None:
    with patch("hermes_cli.profiles._cleanup_gateway_service"), patch(
        "hermes_cli.profiles._stop_profile_backends"
    ):
        delete_profile(name, yes=True)


class TestDeletedProfileTombstone:
    def test_delete_then_logging_setup_does_not_recreate_home(self, profile_env, monkeypatch):
        profile_dir = create_profile("worker", no_alias=True, no_skills=True)
        with patch("hermes_cli.profiles._cleanup_gateway_service"), patch(
            "hermes_cli.profiles._stop_profile_backends"
        ):
            delete_profile("worker", yes=True)

        assert not profile_dir.exists()
        assert "worker" not in _named_homes(profile_env)

        monkeypatch.setenv("HERMES_HOME", str(profile_dir))
        with pytest.raises(FileNotFoundError, match="Named profile home does not exist"):
            setup_logging(hermes_home=profile_dir, force=True)

        assert not profile_dir.exists()
        monkeypatch.setenv("HERMES_HOME", str(profile_env / ".hermes"))
        assert "worker" not in _named_homes(profile_env)

    def test_empty_shell_after_delete_is_not_listed_or_served(self, profile_env):
        profile_dir = create_profile("worker", no_alias=True, no_skills=True)
        with patch("hermes_cli.profiles._cleanup_gateway_service"), patch(
            "hermes_cli.profiles._stop_profile_backends"
        ):
            delete_profile("worker", yes=True)

        # Simulate a stale mkdir that only rebuilds the directory itself.
        profile_dir.mkdir(parents=True)
        (profile_dir / "state.db").write_bytes(b"")

        assert "worker" not in _named_homes(profile_env)
        served = [name for name, _ in profiles_to_serve(True)]
        assert "worker" not in served

    def test_tombstoned_home_is_not_bootstrapped(self, profile_env, monkeypatch):
        profile_dir = create_profile("worker", no_alias=True, no_skills=True)
        with patch("hermes_cli.profiles._cleanup_gateway_service"), patch(
            "hermes_cli.profiles._stop_profile_backends"
        ):
            delete_profile("worker", yes=True)
        profile_dir.mkdir(parents=True)

        monkeypatch.setenv("HERMES_HOME", str(profile_dir))
        with pytest.raises(FileNotFoundError, match="Named profile home does not exist"):
            ensure_hermes_home()
        assert not (profile_dir / "sessions").exists()

    def test_create_after_delete_clears_tombstone(self, profile_env):
        create_profile("worker", no_alias=True, no_skills=True)
        with patch("hermes_cli.profiles._cleanup_gateway_service"), patch(
            "hermes_cli.profiles._stop_profile_backends"
        ):
            delete_profile("worker", yes=True)

        recreated = create_profile("worker", no_alias=True, no_skills=True)
        assert recreated.is_dir()
        assert "worker" in _named_homes(profile_env)

    def test_profile_exists_is_false_for_tombstoned_shell(self, profile_env):
        profile_dir = create_profile("worker", no_alias=True, no_skills=True)
        assert profile_exists("worker") is True
        _delete("worker")
        profile_dir.mkdir(parents=True)

        assert profile_exists("worker") is False
        with pytest.raises(FileNotFoundError, match="does not exist"):
            set_active_profile("worker")
        with pytest.raises(FileNotFoundError, match="does not exist"):
            resolve_profile_env("worker")

    def test_backfill_skips_tombstoned_directory(self, profile_env):
        profile_dir = create_profile("worker", no_alias=True, no_skills=True)
        _delete("worker")
        profile_dir.mkdir(parents=True)
        (profile_env / ".hermes" / ".env").write_text(
            "OPENROUTER_API_KEY=root-key\n", encoding="utf-8"
        )

        backfilled = backfill_profile_envs(quiet=True)

        assert "worker" not in backfilled
        assert not (profile_dir / ".env").exists()

    def test_create_after_delete_replaces_empty_shell(self, profile_env):
        profile_dir = create_profile("worker", no_alias=True, no_skills=True)
        _delete("worker")
        profile_dir.mkdir(parents=True)
        (profile_dir / "logs").mkdir()

        recreated = create_profile("worker", no_alias=True, no_skills=True)
        assert recreated.is_dir()
        assert "worker" in _named_homes(profile_env)

    @pytest.mark.parametrize("leftover", ["config.yaml", ".env"])
    def test_create_after_delete_refuses_when_identity_files_remain(
        self, profile_env, leftover
    ):
        profile_dir = create_profile("worker", no_alias=True, no_skills=True)
        _delete("worker")
        profile_dir.mkdir(parents=True)
        leftover_path = profile_dir / leftover
        leftover_path.write_text("keep-me\n", encoding="utf-8")

        with pytest.raises(FileExistsError, match="already exists"):
            create_profile("worker", no_alias=True, no_skills=True)

        assert leftover_path.read_text(encoding="utf-8") == "keep-me\n"
        assert leftover_path.exists()


class TestNamedProfileHome:
    def test_logs_under_named_profile_resolve_to_profile_home(self, tmp_path):
        # tmp_path acts as a real Hermes home (Docker/custom layout): it
        # carries a home marker file, so profiles/ under it is canonical.
        (tmp_path / "config.yaml").write_text("{}\n", encoding="utf-8")
        worker = tmp_path / "profiles" / "worker"
        assert named_profile_home(worker / "logs") == worker
        assert named_profile_home(worker) == worker

    def test_dot_hermes_layout_resolves_without_markers(self, tmp_path):
        worker = tmp_path / ".hermes" / "profiles" / "worker"
        assert named_profile_home(worker / "logs") == worker
        assert named_profile_home(worker) == worker

    def test_default_home_with_profiles_in_path_is_not_named(self, tmp_path):
        default_home = tmp_path / "foo" / "profiles" / "notahome" / ".hermes"
        assert named_profile_home(default_home) is None
        assert named_profile_home(default_home / "logs") is None

    def test_unrelated_profiles_dir_is_not_named(self, tmp_path):
        # Review point 1 regression: a custom home like
        # /srv/profiles/buildcache must NOT be treated as a named profile —
        # its parent is not a Hermes home, so logging must keep mkdir-ing.
        custom_home = tmp_path / "srv" / "profiles" / "buildcache"
        assert named_profile_home(custom_home) is None
        assert named_profile_home(custom_home / "logs") is None

    def test_unrelated_profiles_dir_still_mkdirs(self, tmp_path):
        from hermes_constants import mkdir_under_hermes_home

        custom_home = tmp_path / "srv" / "profiles" / "buildcache"
        log_dir = mkdir_under_hermes_home(custom_home / "logs")
        assert log_dir.is_dir()

    def test_setup_logging_under_unrelated_profiles_dir_succeeds(self, tmp_path):
        # End-to-end shape of the point-1 regression: setup_logging on a
        # non-profile custom home whose path contains a 'profiles' segment
        # must create the log dir instead of raising FileNotFoundError.
        custom_home = tmp_path / "srv" / "profiles" / "buildcache"
        custom_home.mkdir(parents=True)
        log_dir = setup_logging(hermes_home=custom_home, force=True)
        assert log_dir == custom_home / "logs"
        assert log_dir.is_dir()

    def test_tombstone_dir_marks_profiles_root(self, tmp_path):
        # A profiles/.deleted directory is only ever created by
        # `hermes profile delete` — its presence alone anchors recognition,
        # so tombstones are honored even when root markers are missing.
        profiles_dir = tmp_path / "opt" / "profiles"
        (profiles_dir / ".deleted").mkdir(parents=True)
        worker = profiles_dir / "worker"
        assert named_profile_home(worker / "logs") == worker


class TestDeletedProfileNeverTakesConversationContentBack:
    """A turn in flight for the profile being deleted keeps writing after the delete.

    Its next write raises ``StateDbReplacedError`` — "state.db was replaced under a live
    process" is exactly what a delete looks like from inside a turn — and the handlers for
    that error divert the pending messages to disk under the ACTIVE ``HERMES_HOME``, which is
    still the deleted profile's home. Both diversion paths used a bare ``mkdir(parents=True)``,
    so a fragment of the permanently-deleted conversation was written back under the deleted
    profile, recreating its tree behind ``rmtree``.
    """

    def test_jsonl_divert_refuses_a_deleted_profile_home(self, profile_env, monkeypatch):
        from hermes_state import divert_session_transcript_jsonl

        profile_dir = create_profile("worker", no_alias=True, no_skills=True)
        _delete("worker")
        monkeypatch.setenv("HERMES_HOME", str(profile_dir))

        with pytest.raises(FileNotFoundError):
            divert_session_transcript_jsonl("sess-1", [{"role": "user", "content": "secret"}])
        assert not (profile_dir / "sessions").exists()
        assert not profile_dir.exists()

    def test_pending_spool_refuses_a_deleted_profile_home(self, profile_env, monkeypatch):
        from gateway.shutdown_flush import spool_dropped_transcript_message

        profile_dir = create_profile("worker", no_alias=True, no_skills=True)
        _delete("worker")
        monkeypatch.setenv("HERMES_HOME", str(profile_dir))

        assert spool_dropped_transcript_message("sess-1", {"content": "secret"}) is None
        assert not (profile_dir / "pending_messages").exists()
        assert not profile_dir.exists()

    def test_a_live_profile_still_diverts_normally(self, profile_env, monkeypatch):
        """The guard must only bite on a deleted home — the divert is a real data-loss
        backstop for every other cause of a replaced state.db."""
        from gateway.shutdown_flush import spool_dropped_transcript_message
        from hermes_state import divert_session_transcript_jsonl

        profile_dir = create_profile("worker", no_alias=True, no_skills=True)
        monkeypatch.setenv("HERMES_HOME", str(profile_dir))

        path = divert_session_transcript_jsonl("sess-1", [{"role": "user", "content": "kept"}])
        assert path == profile_dir / "sessions" / "sess-1.jsonl"
        assert spool_dropped_transcript_message("sess-1", {"content": "kept"}) is not None


class _Event:
    """The ``MessageEvent``-shaped value the pending/overflow slots actually hold."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.session_id = "sess-1"


def _planted_payloads(profile_dir: Path, needle: str) -> list[Path]:
    """Every file anywhere under the profile home whose bytes contain *needle*."""
    if not profile_dir.exists():
        return []
    return [f for f in profile_dir.rglob("*")
            if f.is_file() and needle in f.read_text(encoding="utf-8", errors="replace")]


class TestShutdownFlushNeverResurrectsADeletedProfile:
    """``_get_flush_dir`` was the last ungated writer into a deleted profile home.

    ``hermes -p <name> chat`` is interactive, so ``_profile_bound_backend_pids`` never sweeps
    it — ``_BACKEND_TOKENS`` is serve/dashboard/gateway only. The Owner deletes the profile
    from the dashboard, is told the deletion cannot be undone, and when that chat later exits,
    its shutdown flush wrote the in-memory transcript back into the removed tree: a bare
    ``flush_dir.mkdir(parents=True)`` recreated ``profiles/<name>/pending_messages/`` with
    the conversation in it.
    """

    def test_an_interactive_chat_is_not_one_of_the_swept_backends(self):
        """The premise of the whole class: nothing stops the writer, so the write is gated."""
        from hermes_cli.profiles import _BACKEND_TOKENS

        assert "chat" not in _BACKEND_TOKENS and "tui" not in _BACKEND_TOKENS

    def test_pending_flush_refuses_a_deleted_profile_home(self, profile_env, monkeypatch):
        from gateway.shutdown_flush import flush_pending_to_file

        profile_dir = create_profile("faisal", no_alias=True, no_skills=True)
        _delete("faisal")
        monkeypatch.setenv("HERMES_HOME", str(profile_dir))

        assert flush_pending_to_file({"agent:main:tg:1": _Event("PLANTED")}, reason="shutdown") == 0
        assert not (profile_dir / "pending_messages").exists()
        assert not profile_dir.exists()

    def test_overflow_flush_refuses_a_deleted_profile_home(self, profile_env, monkeypatch):
        from gateway.shutdown_flush import flush_overflow_to_file

        profile_dir = create_profile("faisal", no_alias=True, no_skills=True)
        _delete("faisal")
        monkeypatch.setenv("HERMES_HOME", str(profile_dir))

        assert flush_overflow_to_file({"agent:main:tg:1": [_Event("PLANTED")]}) == 0
        assert not (profile_dir / "pending_messages").exists()
        assert not profile_dir.exists()

    def test_agent_history_flush_refuses_a_deleted_profile_home(self, profile_env, monkeypatch):
        from gateway.shutdown_flush import flush_agent_history_to_file

        profile_dir = create_profile("faisal", no_alias=True, no_skills=True)
        _delete("faisal")
        monkeypatch.setenv("HERMES_HOME", str(profile_dir))

        # Swallows by contract — shutdown must not block on a best-effort backup.
        flush_agent_history_to_file("sess-1", [{"role": "user", "content": "PLANTED"}])
        assert not (profile_dir / "pending_messages").exists()
        assert not profile_dir.exists()

    def test_transcript_spool_drain_refuses_a_deleted_profile_home(self, profile_env, monkeypatch):
        from gateway.shutdown_flush import drain_transcript_spool

        profile_dir = create_profile("faisal", no_alias=True, no_skills=True)
        _delete("faisal")
        monkeypatch.setenv("HERMES_HOME", str(profile_dir))

        # A read path, but it reached the same mkdir on the way to the glob.
        assert drain_transcript_spool("sess-1", lambda _m: None) == (0, 0)
        assert not (profile_dir / "pending_messages").exists()
        assert not profile_dir.exists()

    def test_recover_pending_refuses_a_deleted_profile_home(self, profile_env, monkeypatch):
        from gateway.shutdown_flush import recover_pending_to_db

        profile_dir = create_profile("faisal", no_alias=True, no_skills=True)
        _delete("faisal")
        monkeypatch.setenv("HERMES_HOME", str(profile_dir))

        # Its one caller runs it through gateway.run._best_effort, which swallows.
        with pytest.raises(FileNotFoundError):
            recover_pending_to_db()
        assert not (profile_dir / "pending_messages").exists()
        assert not profile_dir.exists()

    def test_the_whole_shutdown_flush_writes_nothing_back_after_a_real_delete(
            self, profile_env, monkeypatch):
        """End to end, in the order the reviewer reproduced: three payloads carrying planted
        conversation text recreated ``profiles/faisal/pending_messages/`` behind ``rmtree``."""
        from gateway.shutdown_flush import (
            flush_agent_history_to_file,
            flush_overflow_to_file,
            flush_pending_to_file,
        )

        profile_dir = create_profile("faisal", no_alias=True, no_skills=True)
        _delete("faisal")
        monkeypatch.setenv("HERMES_HOME", str(profile_dir))

        flush_pending_to_file({"agent:main:tg:1": _Event("PLANTED-PENDING")}, reason="shutdown")
        flush_overflow_to_file({"agent:main:tg:1": [_Event("PLANTED-OVERFLOW")]})
        flush_agent_history_to_file("sess-1", [{"role": "user", "content": "PLANTED-HISTORY"}])

        assert not profile_dir.exists()
        for needle in ("PLANTED-PENDING", "PLANTED-OVERFLOW", "PLANTED-HISTORY"):
            assert _planted_payloads(profile_dir, needle) == []

    def test_a_live_profile_still_flushes_every_path_normally(self, profile_env, monkeypatch):
        """The gate must cost a LIVE home nothing: under FTS5 corruption these payloads are
        the only surviving copy of the conversation."""
        from gateway.shutdown_flush import (
            drain_transcript_spool,
            flush_agent_history_to_file,
            flush_overflow_to_file,
            flush_pending_to_file,
            recover_pending_to_db,
            spool_dropped_transcript_message,
        )

        profile_dir = create_profile("faisal", no_alias=True, no_skills=True)
        monkeypatch.setenv("HERMES_HOME", str(profile_dir))

        assert flush_pending_to_file({"agent:main:tg:1": _Event("KEPT-PENDING")}) == 1
        assert flush_overflow_to_file({"agent:main:tg:1": [_Event("KEPT-OVERFLOW")]}) == 1
        flush_agent_history_to_file("sess-1", [{"role": "user", "content": "KEPT-HISTORY"}])

        flush_dir = profile_dir / "pending_messages"
        assert flush_dir.is_dir()
        for needle in ("KEPT-PENDING", "KEPT-OVERFLOW", "KEPT-HISTORY"):
            assert len(_planted_payloads(profile_dir, needle)) == 1

        # ...and the read half still finds and replays what it spooled.
        assert spool_dropped_transcript_message(
            "sess-1", {"role": "user", "content": "KEPT-SPOOL"}) is not None
        replayed_messages: list = []
        assert drain_transcript_spool("sess-1", replayed_messages.append) == (1, 0)
        assert replayed_messages == [{"role": "user", "content": "KEPT-SPOOL"}]

        # ...and the startup recovery half still reads that same directory back into the DB.
        class _FakeDb:
            def __init__(self) -> None:
                self.rows: list = []

            def append_message(self, **kwargs):
                self.rows.append(kwargs)

        db = _FakeDb()
        assert recover_pending_to_db(db) == 2  # the agent-history snapshot is operator-only
        assert {r["content"] for r in db.rows} == {"KEPT-PENDING", "KEPT-OVERFLOW"}

    def test_the_default_home_is_untouched_by_the_gate(self, profile_env, monkeypatch):
        """``assert_named_profile_home_live`` is a no-op outside ``profiles/<name>`` — the
        default install must keep flushing whether or not any profile was ever deleted."""
        from gateway.shutdown_flush import flush_pending_to_file

        default_home = profile_env / ".hermes"
        monkeypatch.setenv("HERMES_HOME", str(default_home))

        assert flush_pending_to_file({"agent:main:tg:1": _Event("KEPT")}) == 1
        assert len(list((default_home / "pending_messages").glob("*.json"))) == 1
