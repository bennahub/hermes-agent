"""Owner-needed requests land in canonical Bot Chat without a live-owner lock."""

from __future__ import annotations

import sqlite3

import pytest

from agent.autonomy import kernel, store
from agent.autonomy.owner_projection import (
    BOT_CHAT_TITLE,
    append_bot_chat_notice,
    session_is_unread,
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    hermes_home = tmp_path / "profiles" / "badr"
    hermes_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _init_state_db(hermes_home / "state.db")
    return hermes_home


def _init_state_db(path, *, with_bot_chat=False, last_role="assistant"):
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            started_at REAL NOT NULL,
            title TEXT,
            hidden INTEGER DEFAULT 0,
            last_read_at REAL,
            last_activity_at REAL,
            message_count INTEGER DEFAULT 0
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            timestamp REAL NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            display_kind TEXT
        );
        """
    )
    if with_bot_chat:
        conn.execute(
            "INSERT INTO sessions (id, source, started_at, title, hidden, last_read_at, last_activity_at, message_count) "
            "VALUES (?, 'cli', 1, ?, 1, 100.0, 50.0, 1)",
            ("bot-existing", BOT_CHAT_TITLE),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, active) VALUES (?, ?, 'hello', 2, 1)",
            ("bot-existing", last_role),
        )
    conn.commit()
    conn.close()


def test_request_owner_writes_bot_chat_and_marks_unread(home):
    started = kernel.begin_work(
        why="production write needs a decision",
        outcome="owner chooses deploy window",
        done_contract="owner decision recorded",
        idempotency_key="prod:window",
        hermes_home=home,
    )
    work_id = started["work"]["id"]
    first = kernel.request_owner(work_id, "Production deploy window", hermes_home=home)
    assert first["ok"] is True
    assert first["work"]["state"] == "needs_owner"
    assert first["projection"]["projected"] is True
    assert first["projection"]["live_owner_taken"] is False
    session_id = first["projection"]["session_id"]

    conn = sqlite3.connect(str(home / "state.db"))
    titles = [row[0] for row in conn.execute("SELECT title FROM sessions")]
    assert titles.count(BOT_CHAT_TITLE) == 1
    roles = [row[0] for row in conn.execute("SELECT role FROM messages WHERE session_id=? ORDER BY timestamp", (session_id,))]
    texts = [row[0] for row in conn.execute("SELECT content FROM messages WHERE session_id=? ORDER BY timestamp", (session_id,))]
    conn.close()
    assert roles[-1] == "assistant"
    assert "needs you" in texts[-1].lower()
    assert session_is_unread(home, session_id) is True

    replay = kernel.request_owner(work_id, "Production deploy window again", hermes_home=home)
    assert replay["projection"]["duplicate"] is True
    conn = sqlite3.connect(str(home / "state.db"))
    count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    conn.close()
    assert count == len(texts)


def test_projection_reuses_existing_bot_chat(home):
    (home / "state.db").unlink()
    _init_state_db(home / "state.db", with_bot_chat=True)
    result = append_bot_chat_notice("Badr needs you — architecture choice", hermes_home=home, profile="badr")
    assert result["ok"] is True
    assert result["session_id"] == "bot-existing"
    conn = sqlite3.connect(str(home / "state.db"))
    titles = list(conn.execute("SELECT id, title FROM sessions"))
    conn.close()
    assert len(titles) == 1


def test_failed_projection_releases_claim_for_retry(home, monkeypatch):
    started = kernel.begin_work(
        why="secret missing",
        outcome="owner supplies credential",
        done_contract="credential present",
        idempotency_key="secret:1",
        hermes_home=home,
    )
    monkeypatch.setattr(
        "agent.autonomy.owner_projection.append_bot_chat_notice",
        lambda *a, **k: {"ok": False, "error": "state_db_missing"},
    )
    first = kernel.request_owner(started["work"]["id"], "missing deploy key", hermes_home=home)
    assert first["work"]["state"] == "needs_owner"
    assert first["projection"]["ok"] is False
    monkeypatch.setattr(
        "agent.autonomy.owner_projection.append_bot_chat_notice",
        lambda *a, **k: {"ok": True, "session_id": "x", "live_owner_taken": False},
    )
    second = kernel.request_owner(started["work"]["id"], "missing deploy key", hermes_home=home)
    assert second["projection"].get("projected") is True


def test_work_start_binds_jira_ref(home):
    started = kernel.begin_work(
        why="ci path filter",
        outcome="restore PR lanes",
        done_contract="workflows valid",
        idempotency_key="ci:path-filter",
        refs={"jira": "BWM-999"},
        hermes_home=home,
    )
    assert started["work"]["refs"]["jira"] == "BWM-999"
    bound = kernel.bind_tracking(started["work"]["id"], jira="BWM-1000", hermes_home=home)
    assert bound["work"]["refs"]["jira"] == "BWM-1000"
