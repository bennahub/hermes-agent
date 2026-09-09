"""Opening the collaboration ledger, on the deploy that adds a column to it.

Fifteen profile processes and a gateway restart open this database within the
same second of a deploy, against a table every live row of which predates the
`run_id` column. The three ways that goes wrong are all here: the index that
names a column the table has not got yet, the `ALTER` that two processes both
decide to run, and the older build that has to keep working if the release is
rolled back.
"""
import sqlite3
import threading

import pytest

import gateway.a2a_threads as a2a
from gateway.a2a_threads import (
    _connect, _db_path, read_thread, record_send, runs_for, send_event_id, thread_id,
    threads_for,
)


#: The shipped schema exactly as it stands on the live server — nine columns,
#: no `run_id`, and the indexes the previous build created.
_LIVE_SCHEMA = """
CREATE TABLE a2a_events (
    event_id     TEXT PRIMARY KEY,
    thread_id    TEXT NOT NULL,
    sender       TEXT NOT NULL,
    recipient    TEXT NOT NULL,
    body         TEXT NOT NULL,
    attachments  TEXT NOT NULL DEFAULT '[]',
    sent_at      REAL NOT NULL,
    send_id      TEXT NOT NULL DEFAULT '',
    fanout       INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS a2a_events_thread ON a2a_events(thread_id, sent_at);
CREATE INDEX IF NOT EXISTS a2a_events_sender ON a2a_events(sender, sent_at);
CREATE INDEX IF NOT EXISTS a2a_events_recipient ON a2a_events(recipient, sent_at);
CREATE INDEX IF NOT EXISTS a2a_events_send ON a2a_events(send_id);
"""


def _live_ledger(root, rows=3):
    """A ledger written by the build now in production."""
    path = _db_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(_LIVE_SCHEMA)
    with conn:
        for i in range(rows):
            conn.execute(
                "INSERT INTO a2a_events VALUES (?,?,?,?,?,'[]',?,?,1)",
                (send_event_id("joud", "badr", f"old{i}"), thread_id("joud", "badr"),
                 "joud", "badr", f"row {i}", 100.0 + i, f"old{i}"),
            )
    conn.close()
    return path


def _columns(path):
    conn = sqlite3.connect(path)
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(a2a_events)")]
    finally:
        conn.close()


def test_the_migration_survives_every_process_opening_it_at_once(tmp_path):
    """The deploy itself: sixteen openers, one of which wins the `ALTER`.

    Both processes read `PRAGMA table_info` before either commits, so the loser
    raised `duplicate column name: run_id` out of `_connect`. `record_send` then
    answered "the collaboration request could not be recorded; nothing was
    sent" and `threads_for` turned `a2a.threads` into error 5041 — for exactly
    the deploy that introduced the column. Measured against the unguarded body,
    fifteen of sixteen openers lost.
    """
    path = _live_ledger(tmp_path)
    failures, opened = [], []
    barrier = threading.Barrier(16)

    def open_it():
        try:
            barrier.wait(timeout=30)
            conn = _connect(tmp_path)
            opened.append({r[1] for r in conn.execute("PRAGMA table_info(a2a_events)")})
            conn.close()
        except Exception as exc:  # pragma: no cover - the guard is the test
            failures.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=open_it) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert failures == []
    assert len(opened) == 16
    assert all("run_id" in cols for cols in opened)
    # Exactly one column was added, not sixteen attempts' worth of damage.
    assert _columns(path).count("run_id") == 1


def test_a_migration_that_genuinely_fails_is_still_raised(tmp_path, monkeypatch):
    """Absorbing the race must not absorb a broken migration.

    The recovery re-reads `PRAGMA` and only forgives the failure when the column
    is actually there; a statement that cannot produce it still raises, rather
    than leaving every later query to fail against a column that never arrived.
    """
    _live_ledger(tmp_path)
    monkeypatch.setattr(
        a2a, "_MIGRATIONS",
        (("run_id", "ALTER TABLE a2a_events ADD COLUMN nothing_useful TEXT NOT NULL DEFAULT ''"),
         ("run_id", "ALTER TABLE a2a_events ADD COLUMN nothing_useful TEXT NOT NULL DEFAULT ''")),
    )
    with pytest.raises(sqlite3.OperationalError):
        _connect(tmp_path)


def test_the_forward_migration_is_idempotent_and_moves_nothing(tmp_path):
    """Opening it again is a no-op, and no row's identity changes."""
    path = _live_ledger(tmp_path, rows=5)
    before = sqlite3.connect(path).execute(
        "SELECT event_id, thread_id, sent_at FROM a2a_events ORDER BY event_id").fetchall()

    for _ in range(3):
        _connect(tmp_path).close()

    conn = sqlite3.connect(path)
    after = conn.execute(
        "SELECT event_id, thread_id, sent_at FROM a2a_events ORDER BY event_id").fetchall()
    runs = [r[0] for r in conn.execute("SELECT run_id FROM a2a_events")]
    indexes = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='a2a_events'")}
    conn.close()

    assert after == before                      # ids and timestamps untouched
    assert runs == [""] * 5                     # backfilled empty, never guessed
    assert "a2a_events_run" in indexes          # and the new index did get built
    assert _columns(path).count("run_id") == 1
    # The rows still read, and still under the ids they were written with.
    assert len(read_thread(tmp_path, thread=thread_id("joud", "badr"))) == 5


def test_the_index_is_created_after_the_column_exists(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` is a no-op against an existing database.

    An index over `run_id` in the same `executescript` as the table therefore
    named a column that table had not got, and every existing deployment failed
    to open its own ledger. The order is table, then `ALTER`, then indexes.
    """
    path = _live_ledger(tmp_path)
    conn = sqlite3.connect(path)
    existing = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='a2a_events'")}
    conn.close()
    assert "a2a_events_run" not in existing     # the live database has no such index

    _connect(tmp_path).close()                  # must not raise

    conn = sqlite3.connect(path)
    assert {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='a2a_events'"
    )} >= existing | {"a2a_events_run"}
    conn.close()


def test_the_previous_build_can_still_read_and_write_a_migrated_ledger(tmp_path):
    """Rollback safety. The release may be reverted; the database will not be.

    The old build's `INSERT` names nine columns. `NOT NULL DEFAULT ''` is what
    lets that statement keep working against a ten-column table — and what makes
    the row it writes read back as legacy rather than as a broken one.
    """
    _live_ledger(tmp_path, rows=2)
    run = a2a.collaboration_run("turn", "joud", "sess:task:new")
    record_send(tmp_path, sender="joud", recipients=["badr"], body="after the deploy",
                send_id="new-1", run_id=run, sent_at=500.0)
    path = _db_path(tmp_path)

    # Now the previous build opens it: nine-column INSERT, no `run_id` anywhere.
    old = sqlite3.connect(path)
    old.executescript(_LIVE_SCHEMA.replace("CREATE TABLE a2a_events",
                                           "CREATE TABLE IF NOT EXISTS a2a_events"))
    with old:
        old.execute(
            """INSERT INTO a2a_events(event_id, thread_id, sender, recipient, body,
                                      attachments, sent_at, send_id, fanout)
               VALUES (?,?,?,?,?,'[]',?,?,1)""",
            (send_event_id("joud", "badr", "rolled-back"), thread_id("joud", "badr"),
             "joud", "badr", "written by the old build", 600.0, "rolled-back"),
        )
    # It reads every row too, including the one the new build wrote.
    assert old.execute("SELECT COUNT(*) FROM a2a_events").fetchone()[0] == 4
    assert [r[0] for r in old.execute(
        "SELECT body FROM a2a_events ORDER BY sent_at")] == [
        "row 0", "row 1", "after the deploy", "written by the old build"]
    old.close()

    # And the new build reads what the old build wrote, as legacy history.
    legacy = read_thread(tmp_path, thread=thread_id("joud", "badr"))
    assert [e.body for e in legacy] == ["row 0", "row 1", "written by the old build"]
    assert [e.run_id for e in legacy] == ["", "", ""]
    assert len({e.run for e in legacy}) == 1     # one legacy conversation, one card

    # Both eras are listed, neither has eaten the other.
    cards = {c["run_id"]: c for c in runs_for(tmp_path, profile="joud")["runs"]}
    assert len(cards) == 2 and cards[run]["message_count"] == 1
    assert len(threads_for(tmp_path, profile="joud")["threads"]) == 2


def test_a_reader_opened_on_an_unmigrated_table_does_not_raise(tmp_path):
    """`_row` tolerates a database an older build wrote rather than assuming."""
    path = _live_ledger(tmp_path, rows=1)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM a2a_events").fetchone()
    conn.close()
    assert "run_id" not in row.keys()
    event = a2a._row(row)
    assert event.run_id == "" and event.run.startswith("run-")
