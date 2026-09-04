"""Use native SessionDB pruning only for forward-created cron history."""

import time

from hermes_state import SessionDB
from cron.scheduler import _maybe_prune_cron_sessions


def test_activation_preserves_existing_history_and_disabled_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("old-cron", source="cron")
        db.end_session("old-cron", "cron_complete")
        _maybe_prune_cron_sessions(db, {"cron": {"session_retention_days": 0}})
        assert db.get_meta("cron_session_retention_started_at") is None
        _maybe_prune_cron_sessions(db, {"cron": {"session_retention_days": 30}})
        assert db.get_session("old-cron") is not None
        assert float(db.get_meta("cron_session_retention_started_at")) > 0
    finally:
        db.close()


def test_forward_retention_preserves_canonical_user_active_pinned_and_legacy(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(tmp_path / "state.db")
    now = time.time()
    day = 86400
    try:
        db.set_meta("cron_session_retention_started_at", str(now - 60 * day))
        cases = {
            "legacy": ("cron", 70, True, False),
            "old-future": ("cron", 40, True, False),
            "recent": ("cron", 1, True, False),
            "active": ("cron", 40, False, False),
            "pinned": ("cron", 40, True, True),
            "canonical": ("cli", 40, True, False),
            "misclassified-canonical": ("cron", 40, True, False),
            "interactive": ("telegram", 40, True, False),
        }
        for sid, (source, age, ended, pinned) in cases.items():
            db.create_session(sid, source=source)
            if ended:
                db.end_session(sid, "cron_complete" if source == "cron" else "user_exit")
            db._execute_write(lambda conn, sid=sid, age=age, pinned=pinned: conn.execute(
                "UPDATE sessions SET started_at=?, pinned=? WHERE id=?", (now - age * day, int(pinned), sid)))
        db.set_session_title("misclassified-canonical", "Bot Chat")
        _maybe_prune_cron_sessions(db, {"cron": {"session_retention_days": 30}})
        assert db.get_session("old-future") is None
        for sid in cases.keys() - {"old-future"}:
            assert db.get_session(sid) is not None, sid
        stamp = db.get_meta("cron_session_retention_last_prune")
        _maybe_prune_cron_sessions(db, {"cron": {"session_retention_days": 30}})
        assert db.get_meta("cron_session_retention_last_prune") == stamp
    finally:
        db.close()
