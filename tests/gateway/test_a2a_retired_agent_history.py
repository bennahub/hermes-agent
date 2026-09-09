"""A reused slug must not inherit the deleted agent's conversations.

The composition defect. A thread id keys on the agent NAME and the legacy blank-run thread survives
a delete by design (the ledger is append-only and lives at the OWNER's root, where ``rmtree`` never
reaches). A name is a slug the Owner can mint again — so a recreated ``badr`` resolved the deleted
``badr``'s thread with ``joud``, read *"here is the client list"* out of it, and had it listed as
one of its own cards, right after the delete confirmation said "Their conversations … go with them."

The same wave already took the opposite stance one file over: ``agent_computer.service``
``_release_identity_ownership`` strikes a deleted profile's name from every ``BrowserIdentity``
precisely so "a recreated slug does not inherit the deleted agent's browser profile". The reasoning
is the same one and it now reaches both.

**What is retired is the name's claim, not the history.** Nothing rewrites, moves or deletes an
``a2a_events`` row: the counterpart really did have that exchange, its own view of it is untouched,
and the Owner addressing a thread id directly still gets the whole thread. What changes is only
what the next holder of the slug can reach.
"""

from __future__ import annotations

import gateway.a2a_threads as a2a
from gateway.a2a_threads import (
    collaboration_run, read_thread, record_send, resolve_thread, retire_agent, runs_for,
    thread_id, threads_for,
)


SECRET = "here is the client list"


def _history(root, *, at=100.0, run=None):
    """The deleted agent's side of a conversation, in the era the defect was reported in."""
    record_send(root, sender="badr", recipients=["joud"], body=SECRET,
                send_id="s-1", sent_at=at, run_id=run)
    record_send(root, sender="joud", recipients=["badr"], body="thanks",
                send_id="s-2", sent_at=at + 1, run_id=run)


# ── the defect ──


def test_a_recreated_slug_cannot_reach_the_deleted_agents_thread(tmp_path):
    _history(tmp_path)
    assert resolve_thread(tmp_path, profile="badr", counterpart="joud", run="")

    retire_agent(tmp_path, "badr", at=200.0)

    assert resolve_thread(tmp_path, profile="badr", counterpart="joud", run="") == ""
    assert runs_for(tmp_path, profile="badr")["runs"] == []
    assert threads_for(tmp_path, profile="badr")["threads"] == []


def test_a_recreated_slug_cannot_reach_a_typed_run_either(tmp_path):
    """Not only the legacy blank-run era: a run-scoped thread keys on the same pair of names."""
    run = collaboration_run("turn", "badr", "turn-1")
    _history(tmp_path, run=run)
    assert resolve_thread(tmp_path, profile="badr", counterpart="joud", run=run)

    retire_agent(tmp_path, "badr", at=200.0)

    assert resolve_thread(tmp_path, profile="badr", counterpart="joud", run=run) == ""
    assert runs_for(tmp_path, profile="badr")["runs"] == []


def test_a_new_message_does_not_reopen_the_deleted_agents_side_of_the_thread(tmp_path):
    """The case a listing filter alone would miss. A legacy thread keys on the pair, so the
    deleted agent and its replacement share ONE thread id with ``joud`` — the new agent resolves
    it the moment ``joud`` writes, and everything behind that message would open with it."""
    _history(tmp_path)
    retire_agent(tmp_path, "badr", at=200.0)

    record_send(tmp_path, sender="joud", recipients=["badr"], body="welcome aboard",
                send_id="s-3", sent_at=300.0)

    thread = resolve_thread(tmp_path, profile="badr", counterpart="joud", run="")
    assert thread == thread_id("badr", "joud")  # the same id, necessarily
    assert [e.body for e in read_thread(tmp_path, thread=thread, profile="badr")] == [
        "welcome aboard"]
    card = runs_for(tmp_path, profile="badr")["runs"]
    assert len(card) == 1 and card[0]["message_count"] == 1


# ── what a retirement may not take ──


def test_the_ledger_is_untouched(tmp_path):
    """Append-only is protected behaviour: the audit value is the reason these rows exist."""
    _history(tmp_path)
    before = a2a._connect(tmp_path).execute(
        "SELECT event_id, thread_id, sender, recipient, body, sent_at FROM a2a_events "
        "ORDER BY event_id").fetchall()

    retire_agent(tmp_path, "badr", at=200.0)

    after = a2a._connect(tmp_path).execute(
        "SELECT event_id, thread_id, sender, recipient, body, sent_at FROM a2a_events "
        "ORDER BY event_id").fetchall()
    assert [tuple(r) for r in after] == [tuple(r) for r in before]


def test_the_counterparts_own_history_is_untouched(tmp_path):
    """``joud`` really did have that exchange. Hiding it from ``joud`` would be rewriting
    ``joud``'s history to describe something that happened to somebody else."""
    _history(tmp_path)
    retire_agent(tmp_path, "badr", at=200.0)

    thread = resolve_thread(tmp_path, profile="joud", counterpart="badr", run="")
    assert thread
    assert [e.body for e in read_thread(tmp_path, thread=thread, profile="joud")] == [
        SECRET, "thanks"]
    assert len(runs_for(tmp_path, profile="joud")["runs"]) == 1
    assert len(threads_for(tmp_path, profile="joud")["threads"]) == 1


def test_the_owner_addressing_a_thread_id_directly_still_reads_all_of_it(tmp_path):
    """The audit route. A thread id and nothing else is the Owner reading their own ledger, and
    the honest answer to "what does this thread hold" is all of it."""
    _history(tmp_path)
    retire_agent(tmp_path, "badr", at=200.0)

    events = read_thread(tmp_path, thread=thread_id("badr", "joud"))

    assert [e.body for e in events] == [SECRET, "thanks"]


# ── nothing changes for a name that was never retired ──


def test_an_agent_that_was_never_deleted_reads_exactly_what_it_read_before(tmp_path):
    _history(tmp_path)

    assert [e.body for e in read_thread(
        tmp_path, thread=thread_id("badr", "joud"), profile="badr")] == [SECRET, "thanks"]
    assert len(runs_for(tmp_path, profile="badr")["runs"]) == 1
    assert len(threads_for(tmp_path, profile="badr")["threads"]) == 1
    assert resolve_thread(tmp_path, profile="badr", counterpart="joud", run="")


def test_no_floor_means_no_view_at_all(tmp_path):
    """The cost of the retirement rule is paid only by a slug that was actually deleted."""
    _history(tmp_path)

    conn = a2a._connect_as(tmp_path, "badr")
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM temp.sqlite_master WHERE name = 'a2a_events'"
        ).fetchone()[0] == 0
    finally:
        conn.close()

    retire_agent(tmp_path, "badr", at=200.0)
    conn = a2a._connect_as(tmp_path, "badr")
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM temp.sqlite_master WHERE name = 'a2a_events'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


# ── the retirement record itself ──


def test_retiring_twice_keeps_the_newest_floor(tmp_path):
    """A slug can be created and deleted repeatedly; each retirement is appended, and the floor
    is the newest of them — never an older one that would reopen the previous holder's side."""
    _history(tmp_path)
    retire_agent(tmp_path, "badr", at=200.0)
    record_send(tmp_path, sender="joud", recipients=["badr"], body="second badr",
                send_id="s-3", sent_at=300.0)
    retire_agent(tmp_path, "badr", at=400.0)
    record_send(tmp_path, sender="joud", recipients=["badr"], body="third badr",
                send_id="s-4", sent_at=500.0)

    thread = resolve_thread(tmp_path, profile="badr", counterpart="joud", run="")

    assert [e.body for e in read_thread(tmp_path, thread=thread, profile="badr")] == ["third badr"]
    rows = a2a._connect(tmp_path).execute(
        "SELECT agent, retired_at FROM a2a_retirements ORDER BY retired_at").fetchall()
    assert [tuple(r) for r in rows] == [("badr", 200.0), ("badr", 400.0)]


def test_retiring_one_agent_does_not_floor_another(tmp_path):
    _history(tmp_path)
    record_send(tmp_path, sender="sami", recipients=["joud"], body="unrelated",
                send_id="s-9", sent_at=100.0)

    retire_agent(tmp_path, "badr", at=200.0)

    assert len(threads_for(tmp_path, profile="sami")["threads"]) == 1
    assert len(threads_for(tmp_path, profile="joud")["threads"]) == 2


def test_a_deployment_with_no_ledger_is_not_given_one(tmp_path):
    """Nothing to inherit, so nothing to retire — and no empty database created to say so."""
    assert retire_agent(tmp_path, "badr") == 0.0
    assert not (tmp_path / "a2a_threads.db").exists()


def test_the_name_is_normalised_the_way_a_thread_end_is(tmp_path):
    """``@Badr`` and ``badr`` are one agent to `record_send`; they must be one to the floor."""
    _history(tmp_path)

    retire_agent(tmp_path, "@Badr", at=200.0)

    assert resolve_thread(tmp_path, profile="badr", counterpart="joud", run="") == ""


def test_the_floor_outlives_recreating_the_profile(tmp_path):
    """Where the retirement may NOT live. ``profiles/.deleted/<name>`` is the obvious record of a
    deleted agent and it is exactly wrong for this: ``create_profile`` clears that tombstone, so a
    floor kept there would vanish at the moment the slug is reused — which is the one moment it has
    to hold. It lives in the ledger, beside the rows it floors."""
    from hermes_constants import (clear_named_profile_deleted, mark_named_profile_deleted,
                                  named_profile_is_deleted)

    _history(tmp_path)
    profile_dir = tmp_path / "profiles" / "badr"
    profile_dir.mkdir(parents=True)
    retire_agent(tmp_path, "badr", at=200.0)
    mark_named_profile_deleted(profile_dir)

    clear_named_profile_deleted(profile_dir)  # what recreating the slug does

    assert not named_profile_is_deleted(profile_dir)
    assert threads_for(tmp_path, profile="badr")["threads"] == []
    assert resolve_thread(tmp_path, profile="badr", counterpart="joud", run="") == ""
