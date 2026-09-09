"""One collaboration run is one owner-visible card — and every card can be opened.

Two defects meet here.

The first: ``message_agent`` takes one target, so a parent asked to brief four
teammates makes four calls with four ``send_id``s and four threads. Nothing above
the send tied them together, so the owner's transcript drew one row per message —
"1 messages with 1 agent", four times over. ``runs_for`` groups by ``run_id`` and
by nothing else. The clock is used adversarially throughout: sends are stamped
hours apart *inside* one run and one second apart *across* two runs, so a test
that passed by accident on timestamp proximity cannot pass here.

The second, and the reason this file also covers paging: scoping a thread to its
run turned ``threads_for`` from a list bounded by the roster into one that grows
with runs × pairs. Behind a fixed cap with no cursor the owner tapped an older
collaboration card and was told the two agents had never messaged each other,
while ``read_thread`` proved the exchange intact. A listing that has been cut
short is not evidence of absence, and the tests below hold the server to that.
"""
import sqlite3
from types import SimpleNamespace

import gateway.a2a_threads as a2a
from gateway.a2a_threads import (
    collaboration_run, read_thread, record_send, reply_send_id, resolve_thread,
    runs_for, send_event_id, thread_id, threads_for,
)
from tools.bot_mode_dm import resolve_collaboration_run


HOUR = 3600.0


def _turn(who, turn):
    """The run a turn anchors on, spelled the way the send site spells it."""
    return collaboration_run("turn", who, turn)


def _agent(*, turn=None, task=None, incoming=None, work=None):
    return SimpleNamespace(
        _current_turn_id=turn or "", _current_task_id=task or "",
        _turn_display_projection=incoming, _owner_continuity_work_id=work,
    )


def _resolve(agent, root, *, sender="joud", send_id="s1"):
    return resolve_collaboration_run(agent, root=root, sender=sender, send_id=send_id)


def _legacy_ledger(root):
    """The shipped schema as it stands on the live server: no `run_id` column.

    Every row in the live ledger predates runs, so every path that has to read
    one is exercised against a table that has never had the column rather than
    against a fresh one with the column left blank.
    """
    path = a2a._db_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("""CREATE TABLE a2a_events (
            event_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, sender TEXT NOT NULL,
            recipient TEXT NOT NULL, body TEXT NOT NULL,
            attachments TEXT NOT NULL DEFAULT '[]', sent_at REAL NOT NULL,
            send_id TEXT NOT NULL DEFAULT '', fanout INTEGER NOT NULL DEFAULT 1)""")
    return conn


def _legacy_send(conn, *, sender, recipient, send_id, body, at):
    event = send_event_id(sender, recipient, send_id)
    with conn:
        conn.execute("INSERT INTO a2a_events VALUES (?,?,?,?,?,'[]',?,?,1)",
                     (event, thread_id(sender, recipient), sender, recipient, body, at, send_id))
    return event


def _fanout(root, *, sender, targets, run, at=0.0, spacing=HOUR):
    """One agent turn reaching several agents — four ``message_agent`` calls."""
    return [
        record_send(root, sender=sender, recipients=[target], body=f"brief {target}",
                    send_id=f"{run}-req-{target}", run_id=run, sent_at=at + i * spacing)[0]
        for i, target in enumerate(targets)
    ]


def _reply(root, request, *, at):
    """The recipient answering, with the reply identity the runtime derives."""
    send = reply_send_id(request.sender, request.recipient, request.send_id)
    return record_send(
        root, sender=request.recipient, recipients=[request.sender],
        body=f"done {request.recipient}", send_id=send, run_id=request.run, sent_at=at,
    )[0]


# --- one identity, one function ---------------------------------------------

def test_there_is_exactly_one_function_that_mints_a_run_id():
    """Every run id in the module comes out of `collaboration_run`.

    Two spellings of "which run is this" is what once put a request and its own
    reply on two cards. The guard is structural rather than a comment: the only
    `run-` literal any module-level helper produces must come from here.
    """
    import inspect
    source = inspect.getsource(a2a)
    # One f-string builds the prefix, and it is inside `collaboration_run`.
    assert source.count('f"run-{') == 1
    assert 'f"run-{' in inspect.getsource(a2a.collaboration_run)
    # Every anchor is declared, and an undeclared one is refused rather than
    # quietly hashed into a namespace nobody is watching.
    assert a2a.RUN_ANCHORS == ("inherited", "work", "owner", "turn", "send", "legacy")
    for anchor in a2a.RUN_ANCHORS:
        assert collaboration_run(anchor, "x", "y").startswith("run-")
    try:
        collaboration_run("topic", "oauth")
    except ValueError:
        pass
    else:  # pragma: no cover - the guard is the test
        raise AssertionError("an unknown anchor must not mint a run")
    # An anchor with a missing part is no run at all, never a partial one.
    assert collaboration_run("turn", "joud", "") == ""


def test_run_identity_prefers_work_then_owner_then_turn_then_send():
    turn = _turn("joud", "sess:task:abcd1234")
    assert turn.startswith("run-")
    assert _turn("joud", "sess:task:abcd1234") == turn
    assert _turn("joud", "sess:task:ffff0000") != turn
    # Anchors are scoped by profile, so two agents in the same turn id are not
    # accidentally one run.
    assert _turn("badr", "sess:task:abcd1234") != turn
    # Nothing above the send: a run of one, still stable and still not blank.
    alone = collaboration_run("send", "deadbeef")
    assert alone.startswith("run-") and alone != turn


def test_send_site_binds_turn_and_inherits_a_peer_request(tmp_path):
    # A parent answering the owner: the turn is the run, and every call in that
    # turn agrees, which is what makes one instruction one card.
    parent = _agent(turn="sess:task:aaaa1111")
    first = _resolve(parent, tmp_path, send_id="s1")
    second = _resolve(parent, tmp_path, send_id="s2")
    assert first == second == _turn("joud", "sess:task:aaaa1111")

    # A child handling that request and messaging a *third* agent stays inside
    # the run that caused it, even though its own turn id is different.
    child = _agent(turn="sess:task:bbbb2222", incoming={"run_id": first})
    assert _resolve(child, tmp_path, sender="badr", send_id="s3") == first

    # The parent's next instruction is the parent's next turn: a new card.
    assert _resolve(_agent(turn="sess:task:cccc3333"), tmp_path, send_id="s4") != first


def test_a_fanout_is_not_fractured_when_the_turn_has_no_id(tmp_path):
    """The last-resort anchor must not turn one instruction into four cards.

    `_bind_turn_identity` sets the turn and the task together, so this is a
    degenerate agent — but reaching straight past the turn to `("send", …)`
    anchors every `message_agent` call on its own send, and one fan-out becomes
    one card per teammate: the exact rendering runs were built to end.
    """
    agent = _agent(task="task-only")
    runs = {_resolve(agent, tmp_path, send_id=f"send-{i}") for i in range(4)}
    assert len(runs) == 1

    # With neither, the send is the honest last resort and stays a run of one.
    bare = _agent()
    assert len({_resolve(bare, tmp_path, send_id=f"s{i}") for i in range(2)}) == 2


def test_inherited_run_outranks_a_concurrent_work_item(tmp_path):
    """A known, deliberate limitation — pinned so changing it is a decision.

    An agent woken by joud's run that also holds its own work item puts what it
    sends next on the *waking* run's card, even if the message concerns the work
    item. Preferring the work item instead would fracture every A→B→C chain,
    which is anchor 1's whole purpose, to spare this one message; and the two
    cases cannot be told apart without reading the prose the design forbids.
    Bounded to the turn the wake started: the next turn re-anchors on the work.
    """
    waking = _turn("joud", "sess:task:finance")
    agent = _agent(turn="sess:task:badr", incoming={"run_id": waking}, work="work-oauth")
    assert _resolve(agent, tmp_path, sender="badr", send_id="s1") == waking

    # The next turn is not woken by joud, and lands on the work item's run.
    later = _agent(turn="sess:task:badr2", work="work-oauth")
    assert _resolve(later, tmp_path, sender="badr", send_id="s2") == \
        collaboration_run("work", "badr", "work-oauth")


# --- aggregation -------------------------------------------------------------

def test_fanout_to_four_is_one_run_however_far_apart_the_replies_land(tmp_path):
    run = _turn("joud", "sess:task:fanout")
    requests = _fanout(tmp_path, sender="joud", targets=["faisal", "rashid", "badr", "sami"],
                       run=run, at=1000.0)
    # Replies drift in over five hours, and one of them is the last thing that
    # happened in the whole ledger. Under a ninety-second window this is four
    # cards; under the run it is one.
    for i, request in enumerate(requests):
        _reply(tmp_path, request, at=1000.0 + (i + 2) * HOUR)

    runs = runs_for(tmp_path, profile="joud")["runs"]
    assert len(runs) == 1
    card = runs[0]
    assert card["run_id"] == run
    assert card["agent_count"] == 4
    assert card["message_count"] == 8  # four out, four back
    assert card["participants"] == ["faisal", "rashid", "badr", "sami"]
    assert [t["counterpart"] for t in card["threads"]] == card["participants"]
    assert all(t["message_count"] == 2 for t in card["threads"])
    assert card["has_reply"] is True and card["reply_count"] == 4


def test_one_agent_and_thirteen_agents_are_each_a_single_card(tmp_path):
    solo = _turn("joud", "sess:task:solo")
    record_send(tmp_path, sender="joud", recipients=["badr"], body="a",
                send_id="solo-1", run_id=solo, sent_at=10.0)
    thirteen = _turn("joud", "sess:task:thirteen")
    names = [f"agent{i:02d}" for i in range(13)]
    _fanout(tmp_path, sender="joud", targets=names, run=thirteen, at=100.0, spacing=0.05)

    cards = {c["run_id"]: c for c in runs_for(tmp_path, profile="joud")["runs"]}
    assert len(cards) == 2
    assert cards[solo]["agent_count"] == 1 and cards[solo]["message_count"] == 1
    assert cards[thirteen]["agent_count"] == 13 and cards[thirteen]["message_count"] == 13


def test_two_topics_one_second_apart_stay_two_cards_and_two_threads(tmp_path):
    """The mirror of the defect: proximity must not merge unrelated work.

    And under run-scoped thread ids the same pair collaborating twice is two
    *threads*, not one thread listed twice — which is the Owner's original
    complaint, that opening today's work showed months of unrelated history.
    """
    first = _turn("joud", "sess:task:one")
    second = _turn("joud", "sess:task:two")
    _fanout(tmp_path, sender="joud", targets=["badr", "sami"], run=first, at=500.0, spacing=0.1)
    _fanout(tmp_path, sender="joud", targets=["badr", "sami"], run=second, at=501.0, spacing=0.1)

    runs = runs_for(tmp_path, profile="joud")["runs"]
    assert [r["run_id"] for r in runs] == [second, first]  # newest first
    assert all(r["agent_count"] == 2 and r["message_count"] == 2 for r in runs)
    assert len({t["thread_id"] for r in runs for t in r["threads"]}) == 4
    # Opening either one shows that episode and nothing else.
    for run in (first, second):
        thread = resolve_thread(tmp_path, profile="joud", counterpart="badr", run=run)
        assert thread == thread_id("joud", "badr", run)
        assert [e.run for e in read_thread(tmp_path, thread=thread)] == [run]


def test_a_child_talking_to_another_child_stays_in_the_parents_run(tmp_path):
    run = _turn("joud", "sess:task:chain")
    request = record_send(tmp_path, sender="joud", recipients=["badr"], body="look into it",
                          send_id="chain-1", run_id=run, sent_at=10.0)[0]
    # badr, while handling it, asks sami — inherited, not re-derived.
    onward = _resolve(_agent(turn="sess:task:badr", incoming={"run_id": run}),
                      tmp_path, sender="badr", send_id="chain-2")
    assert onward == run
    record_send(tmp_path, sender="badr", recipients=["sami"], body="can you check",
                send_id="chain-2", run_id=onward, sent_at=20.0)
    _reply(tmp_path, request, at=90.0)

    assert [r["run_id"] for r in runs_for(tmp_path, profile="badr")["runs"]] == [run]
    badr = runs_for(tmp_path, profile="badr")["runs"][0]
    assert badr["participants"] == ["joud", "sami"] and badr["message_count"] == 3
    # The owner reading joud's chat sees joud's own side of the same run.
    joud = runs_for(tmp_path, profile="joud")["runs"][0]
    assert joud["run_id"] == run and joud["participants"] == ["badr"]


# --- history and replay ------------------------------------------------------

def test_a_legacy_pair_keeps_its_history_as_one_openable_card(tmp_path):
    """Rows written before runs existed are grouped by the only thing they share.

    They carry no provenance and Hermes will not invent any. What they do have
    is the conversation they are in — and one card per legacy *send* would
    reproduce the "one row per message" rendering this identity exists to end.
    """
    conn = _legacy_ledger(tmp_path)
    for i, who in enumerate(["badr", "sami"]):
        _legacy_send(conn, sender="joud", recipient=who, send_id=f"old{i}",
                     body="old", at=10.0 + i)
    _legacy_send(conn, sender="joud", recipient="badr", send_id="old2",
                 body="and again", at=12.0)
    conn.close()

    runs = runs_for(tmp_path, profile="joud")["runs"]
    assert len(runs) == 2  # two pairs — not one blank-keyed blob, not three sends
    by_agent = {r["participants"][0]: r for r in runs}
    assert by_agent["badr"]["message_count"] == 2
    assert by_agent["sami"]["message_count"] == 1
    # Each card opens, and opening it shows exactly what the card counted.
    for card in runs:
        thread = resolve_thread(tmp_path, profile="joud",
                                counterpart=card["participants"][0], run=card["run_id"])
        assert thread and len(read_thread(tmp_path, thread=thread)) == card["message_count"]

    # New traffic still records a real run against the migrated table.
    run = _turn("joud", "sess:task:after")
    written = record_send(tmp_path, sender="joud", recipients=["badr"], body="new",
                          send_id="new1", run_id=run, sent_at=99.0)[0]
    assert written.run_id == run
    # …in its own thread, so a legacy thread never absorbs a typed message.
    assert written.thread_id != thread_id("joud", "badr")


def test_replay_keeps_the_original_run_and_the_wire_carries_it(tmp_path):
    run = _turn("joud", "sess:task:replay")
    first = record_send(tmp_path, sender="joud", recipients=["badr"], body="same",
                        send_id="dup", run_id=run, sent_at=10.0)
    # A retry under a different run must not fork the event it already wrote.
    again = record_send(tmp_path, sender="joud", recipients=["badr"], body="same",
                        send_id="dup", run_id=_turn("joud", "other"), sent_at=99.0)
    assert [e.event_id for e in again] == [e.event_id for e in first]
    assert again[0].run_id == run
    assert first[0].event_id == send_event_id("joud", "badr", "dup")
    assert first[0].to_dict()["run_id"] == run
    assert len(runs_for(tmp_path, profile="joud")["runs"]) == 1


def test_the_real_reply_path_carries_the_run_end_to_end(tmp_path):
    """The runtime's own reply finalizer, not a hand-written reply id.

    ``stamp_final`` types the recipient's answer and ``record_peer_final``
    indexes it; between them they are the only way a reply reaches the ledger.
    If the run did not survive that pair, every answer would open a card of its
    own and the fan-out would come apart the moment anyone replied.
    """
    from agent.message_projection import native_metadata, projection, record_peer_final, stamp_final

    home = tmp_path / "profiles" / "badr"
    home.mkdir(parents=True)
    run = _turn("joud", "sess:task:live")
    request = record_send(tmp_path, sender="joud", recipients=["badr"], body="please check QA",
                          send_id="live-1", run_id=run, sent_at=10.0)[0]
    incoming = native_metadata(
        "peer", "peer", "collaboration", sender="joud", recipient="badr",
        send_id="live-1", event_id=request.event_id, run_id=run, source_session_id="s",
    )
    agent = SimpleNamespace(
        _turn_display_projection=projection(incoming), _persist_user_message_idx=0,
        _owner_continuity_work_id=None,
        _session_db=SimpleNamespace(db_path=home / "state.db"),
    )
    messages = [{"role": "user", "content": "…", "display_metadata": incoming},
                {"role": "assistant", "content": "QA is clean."}]
    stamp_final(agent, messages)
    reply = projection(messages[-1]["display_metadata"])
    assert reply["run_id"] == run
    assert reply["send_id"] == reply_send_id("joud", "badr", "live-1")
    written = record_peer_final(agent, messages)[0]
    assert written.run_id == run
    # The reply lands in the request's own thread, not merely its own run.
    assert written.thread_id == request.thread_id

    card = runs_for(tmp_path, profile="joud")["runs"]
    assert len(card) == 1 and card[0]["message_count"] == 2 and card[0]["agent_count"] == 1


def test_a_blank_run_request_answered_through_stamp_final_is_one_card(tmp_path):
    """The real reply path, against a request recorded before runs existed.

    Every row in the live ledger is blank, so this is what the first replies
    after the deploy do. The request's run is blank, the reply inherits blank,
    and both rows sit in the pair's legacy thread — so a reader that anchored a
    blank row on its *send* gave one exchange two ids and drew it as two cards.
    Anchoring on the thread they share is what keeps it one.
    """
    from agent.message_projection import native_metadata, projection, record_peer_final, stamp_final

    conn = _legacy_ledger(tmp_path)
    event = _legacy_send(conn, sender="joud", recipient="badr", send_id="legacysend",
                         body="please check QA", at=10.0)
    conn.close()

    home = tmp_path / "profiles" / "badr"
    home.mkdir(parents=True)
    # The envelope as it was written before runs existed: no `run_id` at all.
    incoming = native_metadata(
        "peer", "peer", "collaboration", sender="joud", recipient="badr",
        send_id="legacysend", event_id=event, source_session_id="s",
    )
    assert "run_id" not in projection(incoming)
    agent = SimpleNamespace(
        _turn_display_projection=projection(incoming), _persist_user_message_idx=0,
        _owner_continuity_work_id=None,
        _session_db=SimpleNamespace(db_path=home / "state.db"),
    )
    messages = [{"role": "user", "content": "…", "display_metadata": incoming},
                {"role": "assistant", "content": "QA is clean."}]
    stamp_final(agent, messages)
    written = record_peer_final(agent, messages)[0]

    events = read_thread(tmp_path, thread=thread_id("joud", "badr"))
    assert [e.body for e in events] == ["please check QA", "QA is clean."]
    assert [e.run_id for e in events] == ["", ""]      # still untouched history
    assert len({e.run for e in events}) == 1           # and still one run
    assert written.thread_id == thread_id("joud", "badr")

    cards = runs_for(tmp_path, profile="joud")["runs"]
    assert len(cards) == 1
    assert cards[0]["message_count"] == 2 and cards[0]["agent_count"] == 1
    assert cards[0]["has_reply"] is True


def test_every_surface_names_a_legacy_conversation_the_same_way(tmp_path):
    """One identity for one thing, on all four surfaces.

    A client must never be handed two spellings of the same conversation and
    asked to reconcile them — that is how a request and its reply ended up on
    two cards in the first place. For a message delivered before runs existed,
    the transcript projection, the run card, the thread listing and the thread
    itself must agree.
    """
    from agent.message_projection import collaboration_summary, native_metadata

    conn = _legacy_ledger(tmp_path)
    event = _legacy_send(conn, sender="joud", recipient="badr", send_id="legacysend",
                         body="from before runs", at=10.0)
    conn.close()

    # The envelope as it was written then: sender and recipient, but no run.
    summary = collaboration_summary(native_metadata(
        "peer", "peer", "collaboration", sender="joud", recipient="badr",
        send_id="legacysend", event_id=event, source_session_id="s"))
    card = runs_for(tmp_path, profile="joud")["runs"][0]
    listed = threads_for(tmp_path, profile="joud")["threads"][0]
    opened = read_thread(tmp_path, thread=thread_id("joud", "badr"))[0]

    assert summary["thread_id"] == listed["thread_id"] == thread_id("joud", "badr")
    assert (summary["run_id"] == card["run_id"] == listed["run_id"]
            == opened.run == opened.to_dict()["run_id"])
    assert a2a._RUN_ID.match(summary["run_id"])       # never the empty string
    assert opened.run_id == ""                        # while the row stays legacy
    assert resolve_thread(tmp_path, profile="joud", counterpart="badr",
                          run=summary["run_id"]) == summary["thread_id"]


# --- what the server says, so the client does not guess ----------------------

def test_the_server_says_whether_anyone_answered(tmp_path):
    """`has_reply` is direction, not arithmetic.

    "More messages than threads" is a different question and gets this case
    wrong: two `message_agent` calls to one teammate and one to another, with
    nobody answering, is three messages across two threads — and the client told
    the owner an exchange had happened when none had.
    """
    run = _turn("joud", "sess:task:twice")
    for i in range(2):
        record_send(tmp_path, sender="joud", recipients=["badr"], body=f"and also {i}",
                    send_id=f"twice-{i}", run_id=run, sent_at=10.0 + i)
    record_send(tmp_path, sender="joud", recipients=["sami"], body="you too",
                send_id="twice-sami", run_id=run, sent_at=12.0)

    card = runs_for(tmp_path, profile="joud")["runs"][0]
    assert card["message_count"] == 3 and card["agent_count"] == 2
    assert card["message_count"] > len(card["threads"])  # the old rule said "replied"
    assert card["has_reply"] is False and card["reply_count"] == 0
    assert [t["has_reply"] for t in card["threads"]] == [False, False]

    # badr answers; sami still has not.
    record_send(tmp_path, sender="badr", recipients=["joud"], body="done",
                send_id=reply_send_id("joud", "badr", "twice-0"), run_id=run, sent_at=99.0)
    card = runs_for(tmp_path, profile="joud")["runs"][0]
    assert card["has_reply"] is True and card["reply_count"] == 1
    assert {t["counterpart"]: t["has_reply"] for t in card["threads"]} == {
        "badr": True, "sami": False,
    }
    # And the same answer where a pairwise summary is read on its own.
    assert {t["counterpart"]: t["has_reply"]
            for t in threads_for(tmp_path, profile="joud")["threads"]} == {
        "badr": True, "sami": False,
    }


# --- the listing must never be the only route to a thread --------------------

def test_every_run_stays_openable_when_the_listing_is_paged_past_it(tmp_path):
    """The P1 this file exists for: 120 episodes, 8 peers, one page.

    Run scoping made `a2a.threads` grow with runs × pairs. Behind a fixed cap
    with no cursor only the newest handful were reachable, the client looked for
    an older card's thread, did not find it in the page, and rendered "no
    messages between abu-saud and faisal" over an intact exchange. Asserting
    history does not exist is worse than showing too much of it.
    """
    runs = []
    for episode in range(120):
        run = _turn("abu-saud", f"sess:task:{episode:03d}")
        runs.append(run)
        _fanout(tmp_path, sender="abu-saud", targets=[f"peer{i}" for i in range(8)],
                run=run, at=1000.0 + episode * 100, spacing=0.5)

    # One page is a page, and it says so rather than looking like the whole set.
    page = threads_for(tmp_path, profile="abu-saud", limit=100)
    assert len(page["threads"]) == 100 and page["next_cursor"]

    # Paging reaches every thread, exactly once, with no cursor loop.
    seen, cursor, pages = [], None, 0
    while True:
        page = threads_for(tmp_path, profile="abu-saud", limit=100, cursor=cursor)
        seen.extend(t["thread_id"] for t in page["threads"])
        cursor, pages = page["next_cursor"], pages + 1
        if not cursor:
            break
        assert pages < 50, "cursor did not advance"
    assert len(seen) == 120 * 8 == len(set(seen))
    assert len({t for t in seen}) == len(set(seen))

    # And the oldest run — far past any first page — opens directly, which is
    # the route that makes the paging safe.
    oldest = runs[0]
    thread = resolve_thread(tmp_path, profile="abu-saud", counterpart="peer3", run=oldest)
    assert thread == thread_id("abu-saud", "peer3", oldest)
    assert [e.body for e in read_thread(tmp_path, thread=thread)] == ["brief peer3"]


def test_a_run_behind_a_long_pair_history_still_opens_whole(tmp_path):
    """Issue 2's P1, and under run-scoped threads it stops being reachable.

    The narrowing was a list comprehension over rows SQLite had already
    truncated, so once a pair passed the five-hundredth event every run that
    began after it read as empty: a card saying "8 messages with 4 agents"
    opened on "nothing exchanged yet". Keying the thread on the run means the
    cap applies to the run's own events and never to the pair's, so there is no
    filter left to apply after a `LIMIT` and no way to reintroduce one.
    """
    old = _turn("joud", "sess:task:old")
    for i in range(600):
        record_send(tmp_path, sender="joud", recipients=["badr"], body=f"old {i}",
                    send_id=f"old-{i}", run_id=old, sent_at=1000.0 + i)
    recent = _turn("joud", "sess:task:recent")
    for i in range(3):
        record_send(tmp_path, sender="joud", recipients=["badr"], body=f"new {i}",
                    send_id=f"new-{i}", run_id=recent, sent_at=9000.0 + i)

    # Two runs on one pair are two threads, so the long one cannot crowd out
    # the short one — the cap it hits is its own.
    behind = resolve_thread(tmp_path, profile="joud", counterpart="badr", run=recent)
    assert behind == thread_id("joud", "badr", recent) != thread_id("joud", "badr", old)
    assert [e.body for e in read_thread(tmp_path, thread=behind)] == ["new 0", "new 1", "new 2"]
    assert {c["run_id"]: c["message_count"]
            for c in runs_for(tmp_path, profile="joud")["runs"]}[recent] == 3
    # And the long run is bounded by its own size at the cap the clients send.
    ahead = resolve_thread(tmp_path, profile="joud", counterpart="badr", run=old)
    assert len(read_thread(tmp_path, thread=ahead, limit=500)) == 500
    assert len(read_thread(tmp_path, thread=ahead, limit=600)) == 600


def test_the_gateway_separates_an_empty_run_from_an_unknown_one(tmp_path, monkeypatch):
    """The contract-level half of the same defect.

    A client must be able to tell "this run holds no messages" from "this run
    was not on the page I read". Conflating them is what let a paging boundary
    be rendered as a claim about history.
    """
    from tui_gateway import server as gateway_server

    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: str(tmp_path))
    run = _turn("joud", "sess:task:known")
    record_send(tmp_path, sender="joud", recipients=["badr"], body="hello",
                send_id="k1", run_id=run, sent_at=10.0)

    def call(rid, name, params):
        return gateway_server.handle_request(
            {"jsonrpc": "2.0", "id": rid, "method": name, "params": params})["result"]

    known = call(1, "a2a.thread", {"profile": "joud", "counterpart": "badr", "run_id": run})
    assert known["resolved"] is True and known["event_count"] == 1
    assert known["thread_id"] == thread_id("joud", "badr", run)
    assert known["run_id"] == run

    # A run this ledger has no record of is *not* an empty conversation.
    unknown = call(2, "a2a.thread", {"profile": "joud", "counterpart": "badr",
                                     "run_id": _turn("joud", "sess:task:never")})
    assert unknown["resolved"] is False and unknown["events"] == []
    assert unknown["thread_id"] == ""

    # Nor is a thread id that names nothing — stale, or discarded before it was
    # ever sent. Answering `resolved: true` there would be the same claim about
    # two agents' history, made through the other door.
    stale = call(5, "a2a.thread", {"thread_id": thread_id("joud", "badr", _turn("joud", "gone"))})
    assert stale["resolved"] is False and stale["events"] == []

    # And the listing hands back a cursor field even when there is one page,
    # so a client can tell "no more" from "this server does not page".
    listing = call(3, "a2a.threads", {"profile": "joud"})
    assert listing["next_cursor"] is None and len(listing["threads"]) == 1
    assert listing["threads"][0]["has_reply"] is False

    runs = call(4, "a2a.runs", {"profile": "joud"})["runs"]
    assert [r["run_id"] for r in runs] == [run]
    assert runs[0]["reply_count"] == 0


def test_the_run_listing_pages_on_the_same_contract(tmp_path):
    """Both listings page, so neither can quietly end while history remains.

    A run list grows with how much work the team does, exactly as the thread
    list does. Leaving one of the two capped without a cursor would keep the
    shape of the defect in the contract even after the other was fixed.
    """
    made = []
    for episode in range(25):
        run = _turn("joud", f"sess:task:{episode:03d}")
        made.append(run)
        _fanout(tmp_path, sender="joud", targets=["badr", "sami"], run=run,
                at=1000.0 + episode * 100, spacing=0.5)

    seen, cursor, pages = [], None, 0
    while True:
        page = runs_for(tmp_path, profile="joud", limit=10, cursor=cursor)
        seen += [r["run_id"] for r in page["runs"]]
        cursor, pages = page["next_cursor"], pages + 1
        if not cursor:
            break
        assert pages < 20, "cursor did not advance"
    assert len(seen) == 25 == len(set(seen))
    assert seen == list(reversed(made))          # newest first, across pages
    # The last page says so rather than looking like a short one.
    assert runs_for(tmp_path, profile="joud", limit=50)["next_cursor"] is None


def test_paging_is_keyset_so_a_new_message_cannot_hide_a_row(tmp_path):
    """An OFFSET would drop a row when the list reorders between pages."""
    for episode in range(6):
        run = _turn("joud", f"sess:task:{episode}")
        record_send(tmp_path, sender="joud", recipients=[f"peer{episode}"], body="hi",
                    send_id=f"s{episode}", run_id=run, sent_at=100.0 + episode)

    first = threads_for(tmp_path, profile="joud", limit=3)
    assert first["next_cursor"]
    # The oldest thread gets a new message and jumps to the front of the list.
    oldest = first["threads"][-1]
    record_send(tmp_path, sender="peer3", recipients=["joud"], body="late",
                send_id="late", run_id=_turn("joud", "sess:task:3"), sent_at=999.0)

    rest = threads_for(tmp_path, profile="joud", limit=3, cursor=first["next_cursor"])
    # The reordering cannot pull an unseen row above the mark and lose it.
    assert set(t["thread_id"] for t in rest["threads"]).isdisjoint(
        t["thread_id"] for t in first["threads"])
    assert len(first["threads"]) + len(rest["threads"]) == 6
    assert oldest["thread_id"] in {t["thread_id"] for t in first["threads"]}

    # An unreadable mark reads from the top rather than raising at the client.
    assert (threads_for(tmp_path, profile="joud", limit=3, cursor="not-a-cursor")
            == threads_for(tmp_path, profile="joud", limit=3))

    # A mark from the *other* listing is unreadable too, rather than paging
    # from a comparison between two unrelated strings. The two listings sort on
    # different second halves, so accepting one another's marks would be a
    # wrong page — the quietest kind of wrong.
    runs_mark = runs_for(tmp_path, profile="joud", limit=1)["next_cursor"]
    assert runs_mark
    assert (threads_for(tmp_path, profile="joud", limit=3, cursor=runs_mark)
            == threads_for(tmp_path, profile="joud", limit=3))
    threads_mark = threads_for(tmp_path, profile="joud", limit=1)["next_cursor"]
    assert (runs_for(tmp_path, profile="joud", limit=3, cursor=threads_mark)
            == runs_for(tmp_path, profile="joud", limit=3))


# --- what one listing is allowed to cost -------------------------------------

def test_a_legacy_ledger_is_not_read_whole_to_draw_a_page(tmp_path, monkeypatch):
    """A blank `run_id` must not group the entire pre-run ledger into one entry.

    It did, and that one entry then named every row it held, so the second query
    returned the whole ledger: tens of thousands of rows built into as many
    dicts to draw fifty cards, on every chat open, for as long as history is
    blank — which, right after the deploy, is all of it.
    """
    conn = _legacy_ledger(tmp_path)
    for i in range(480):
        _legacy_send(conn, sender="joud", recipient=f"agent{i % 12:02d}",
                     send_id=f"old-{i}", body=f"row {i}", at=1000.0 + i)
    conn.close()

    built = []
    real = a2a._row
    monkeypatch.setattr(a2a, "_row", lambda row: built.append(1) or real(row))
    cards = runs_for(tmp_path, profile="joud", limit=3)["runs"]

    assert len(cards) == 3
    # Three pairs of forty rows each — the page's own cost, not the ledger's.
    assert all(c["message_count"] == 40 and c["agent_count"] == 1 for c in cards)
    assert len(built) == 120, f"materialised {len(built)} rows to return 3 cards"
    assert len(built) < 480
    assert len({c["run_id"] for c in cards}) == 3


def test_the_listing_ceiling_shortens_a_page_but_never_loses_a_run(tmp_path, monkeypatch):
    """What the flat ceiling does when a page's runs together exceed it.

    The oldest events are the ones dropped, so the oldest runs on that page come
    back empty and fall out of it. The cursor is taken from the last card that
    *did* materialise, so the next page resumes above them — a page comes up
    short rather than a run going missing. Asserted with a tiny ceiling, because
    reaching the real one takes ten thousand events across one page.
    """
    monkeypatch.setattr(a2a, "MAX_LISTING_EVENTS", 4)
    made = []
    for episode in range(6):
        run = _turn("joud", f"sess:task:{episode}")
        made.append(run)
        for k in range(3):
            record_send(tmp_path, sender="joud", recipients=["badr"], body=f"m{episode}.{k}",
                        send_id=f"s{episode}-{k}", run_id=run, sent_at=100.0 + episode * 10 + k)

    seen, cursor, pages = [], None, 0
    while True:
        page = runs_for(tmp_path, profile="joud", limit=6, cursor=cursor)
        assert len(page["runs"]) < 6, "the ceiling should be shortening these pages"
        seen += [r["run_id"] for r in page["runs"]]
        cursor, pages = page["next_cursor"], pages + 1
        if not cursor:
            break
        assert pages < 20, "cursor did not advance"
    # Short pages, more of them — and every run still reached, exactly once.
    assert pages > 1
    assert seen == list(reversed(made)) == list(dict.fromkeys(seen))


def test_a_cards_count_does_not_depend_on_how_many_cards_were_asked_for(tmp_path):
    """The ceiling on a listing is flat, not a share of the page.

    Divided by `limit` it would mean the same run answers "two hundred messages"
    to a caller asking for one card and "two hundred and sixty" to a caller
    asking for fifty — the same short count `runs_for` was written to stop
    reporting, reintroduced through the bound added to it.
    """
    run = _turn("joud", "sess:task:long")
    for i in range(260):
        record_send(tmp_path, sender="joud", recipients=["badr"], body=f"m{i}",
                    send_id=f"long-{i}", run_id=run, sent_at=1000.0 + i)

    assert runs_for(tmp_path, profile="joud", limit=1)["runs"][0]["message_count"] == 260
    assert runs_for(tmp_path, profile="joud", limit=50)["runs"][0]["message_count"] == 260
    assert a2a.MAX_LISTING_EVENTS >= 260


def test_a_legacy_ledger_past_the_ceiling_still_reports_a_true_count(tmp_path, monkeypatch):
    """The ceiling may bound work. It may not quietly become the answer.

    A legacy-only ledger does *not* behave the way the typed one does when the
    ceiling bites. Legacy keys are roster-bounded — one per pair — so every key
    still materialises, none fall out, and the "a page comes up short and the
    cursor picks the rest up" recovery never fires. The card simply reported
    however many of its messages happened to be under the ceiling and said the
    listing was complete: twelve thousand events across fourteen pairs answered
    ten thousand, with `next_cursor: null` next to it.
    """
    monkeypatch.setattr(a2a, "MAX_LISTING_EVENTS", 20)
    conn = _legacy_ledger(tmp_path)
    peers = [f"agent{i:02d}" for i in range(7)]
    for i in range(70):
        peer = peers[i % len(peers)]
        sender, recipient = ("joud", peer) if i % 2 == 0 else (peer, "joud")
        _legacy_send(conn, sender=sender, recipient=recipient,
                     send_id=f"old-{i}", body=f"row {i}", at=1000.0 + i)
    conn.close()

    page = runs_for(tmp_path, profile="joud", limit=50)
    # Every pair is still one card, and the listing really is complete...
    assert len(page["runs"]) == len(peers)
    assert page["next_cursor"] is None
    # ...so the counts next to that have to be the ledger's, not the scan's.
    assert sorted(r["message_count"] for r in page["runs"]) == [10] * len(peers)
    assert sum(t["message_count"] for r in page["runs"] for t in r["threads"]) == 70
    # The first event of each pair is far below the ceiling's cut at 1050.
    assert sorted(r["started_at"] for r in page["runs"]) == [1000.0 + i for i in range(7)]
    # Both directions are in every pair, which only the whole thread can say.
    assert all(r["has_reply"] and r["reply_count"] == 1 for r in page["runs"])


def test_a_card_that_is_drawn_describes_every_thread_it_holds(tmp_path, monkeypatch):
    """The ceiling drops the oldest events; it must not drop a card's agents.

    A run whose newest exchange fills the ceiling on its own was drawn with only
    the peers that fitted under it — "1 agent" over a fan-out to two, and the
    older exchange missing from the list the card exists to open. Whether the
    card is on this page is the ceiling's business; what the card says about
    itself is the ledger's.
    """
    monkeypatch.setattr(a2a, "MAX_LISTING_EVENTS", 5)
    run = _turn("joud", "sess:task:wide")
    record_send(tmp_path, sender="joud", recipients=["badr"], body="early",
                send_id="w-badr", run_id=run, sent_at=100.0)
    for i in range(8):
        record_send(tmp_path, sender="joud", recipients=["faisal"], body=f"late {i}",
                    send_id=f"w-faisal-{i}", run_id=run, sent_at=200.0 + i)

    card = runs_for(tmp_path, profile="joud", limit=5)["runs"][0]
    assert card["run_id"] == run and card["message_count"] == 9
    assert card["agent_count"] == 2 and sorted(card["participants"]) == ["badr", "faisal"]
    assert card["started_at"] == 100.0
    dropped = next(t for t in card["threads"] if t["counterpart"] == "badr")
    assert dropped["thread_id"] == thread_id("joud", "badr", run)
    assert dropped["message_count"] == 1 and dropped["last_at"] == 100.0
    # Its last message is fetched by seek rather than left blank or guessed.
    assert dropped["last_text"] == "early" and dropped["last_sender"] == "joud"


def test_one_page_of_both_kinds_of_key_stays_under_the_variable_ceiling(tmp_path, monkeypatch):
    """What a full page actually binds, so the comment beside it can say it.

    `recorded` and `legacy` are disjoint halves of one page of keys, so the two
    legs together bind `limit` + 4 however the page is split between them — and
    each statement binds one more of its own. 505 at `limit=500`, which the
    comment used to round down to 504 and which SQLite's historical
    999-variable ceiling is clear of either way.
    """
    class _Spy:
        def __init__(self, conn, log):
            self._conn, self._log = conn, log

        def execute(self, sql, params=()):
            self._log.append(len(params))
            return self._conn.execute(sql, params)

        def __getattr__(self, item):
            return getattr(self._conn, item)

    bound: list[int] = []
    real = a2a._connect
    monkeypatch.setattr(a2a, "_connect", lambda home: _Spy(real(home), bound))

    conn = real(tmp_path)
    with conn:
        conn.executemany(
            """INSERT INTO a2a_events(event_id, thread_id, sender, recipient, body,
                                      attachments, sent_at, send_id, fanout, run_id)
               VALUES (?,?,?,?,'x','[]',?,?,1,?)""",
            [(send_event_id("joud", peer, f"s{i}"), thread_id("joud", peer, run),
              "joud", peer, 1000.0 + i, f"s{i}", run)
             for i, (peer, run) in enumerate(
                 # 250 legacy pairs — one key each, blank run — and 250 typed
                 # runs with one peer, which is the other half of a 500-key page.
                 [(f"peer{n:03d}", "") for n in range(250)]
                 + [("badr", _turn("joud", f"sess:task:{n}")) for n in range(250)])])
    conn.close()

    bound.clear()
    page = runs_for(tmp_path, profile="joud", limit=500)
    assert len(page["runs"]) == 500
    assert max(bound) == 505 < 999


# --- what may be carried through as an identity ------------------------------

def test_an_inherited_run_is_carried_through_only_in_its_own_shape(tmp_path):
    """`inherited` arrives inside an agent's envelope, so it is checked.

    Accepted verbatim it would reach a database column, the wire and a SwiftUI
    view id at whatever length and character set the sender chose.
    """
    canonical = _turn("joud", "sess:task:real")
    assert collaboration_run("inherited", canonical) == canonical  # no fork

    for hostile in ("run-" + "x" * 4000, "run-", "run-NOTHEXNOTHEXNOTHEXNOT",
                    "run-0123456789abcdef01234567extra", "legacy:sneaky", "run-\n0123"):
        bounded = collaboration_run("inherited", hostile)
        assert a2a._RUN_ID.match(bounded), bounded
        # Stable, so a chain that inherits it does not fork on the next hop.
        assert collaboration_run("inherited", bounded) == bounded

    # And the column is normalised at the one door into it.
    written = record_send(tmp_path, sender="joud", recipients=["badr"], body="x",
                          send_id="hostile-1", run_id="run-" + "x" * 4000, sent_at=5.0)[0]
    assert a2a._RUN_ID.match(written.run_id)
    assert runs_for(tmp_path, profile="joud")["runs"][0]["run_id"] == written.run_id
    # The send site admits it too, not only the ledger.
    admitted = _resolve(_agent(turn="t", incoming={"run_id": "run-" + "x" * 4000}), tmp_path)
    assert a2a._RUN_ID.match(admitted)


def test_a_thread_payload_cannot_be_spelled_two_ways(tmp_path):
    """`thread_id` must not let one payload mean two different questions.

    NUL-joining run, a and b made `thread_id("a\\x00b", "c")` and
    `thread_id("b", "c", run="a")` the same id. Agent names cannot contain NUL
    today, so it was unreachable — and closing it by construction costs a line.
    """
    assert thread_id("a\x00b", "c") != thread_id("b", "c", run="a")
    # The legacy payload is the one thing that may never move.
    import hashlib
    for left, right in (("abu-saud", "faisal"), ("", "x"), ("  ", "\x00"),
                        ("جود", "بدر"), ("@Joud", "BADR")):
        a, b = sorted([a2a._norm(left), a2a._norm(right)])
        expected = hashlib.sha256(f"{a}\x00{b}".encode()).hexdigest()[:24]
        assert thread_id(left, right) == f"a2a-{expected}"
        assert thread_id(left, right, "") == f"a2a-{expected}"


def test_the_gateway_never_echoes_a_run_it_would_not_have_stored(tmp_path, monkeypatch):
    """A run leaves this server in the one shape a run has.

    `a2a.thread` answers `resolved: false` with the run the caller asked about,
    and the client contract makes that field a view identity and an
    accessibility identifier. Echoed raw, two hundred characters of a caller's
    own choosing round-tripped verbatim through a field every other surface
    guarantees is `run-<24 hex>` — self-echo, but the guarantee is the point.
    """
    from tui_gateway import server as gateway_server
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: str(tmp_path))

    def call(rid, params):
        return gateway_server.handle_request(
            {"jsonrpc": "2.0", "id": rid, "method": "a2a.thread", "params": params})["result"]

    hostile = "X" * 200
    # Neither door echoes it: the one that could not resolve a pair and a run...
    unknown = call(1, {"profile": "joud", "counterpart": "badr", "run_id": hostile})
    assert unknown["resolved"] is False and unknown["run_id"] != hostile
    assert a2a._RUN_ID.match(unknown["run_id"])
    # ...nor the one handed a thread id that names nothing, where the caller's
    # run is what the answer falls back to.
    stale = call(2, {"thread_id": thread_id("joud", "badr", _turn("joud", "gone")),
                     "run_id": hostile})
    assert stale["resolved"] is False and stale["run_id"] != hostile
    assert a2a._RUN_ID.match(stale["run_id"])
    # The same door the ledger's own column goes through, so a hostile run that
    # was written and one that was only asked about are spelled the same way.
    assert unknown["run_id"] == stale["run_id"] == collaboration_run("inherited", hostile)

    # A canonical run is unchanged — inheriting must not fork an episode — and
    # a run nobody asked about stays absent rather than becoming a digest of "".
    run = _turn("joud", "sess:task:real")
    record_send(tmp_path, sender="joud", recipients=["badr"], body="hi",
                send_id="r1", run_id=run, sent_at=1.0)
    assert call(3, {"profile": "joud", "counterpart": "badr", "run_id": run})["run_id"] == run
    assert call(4, {"profile": "joud", "counterpart": "nobody"})["run_id"] == ""


def test_a_collaboration_card_admits_the_run_it_reads():
    """The one read of a run id that did not go through the one door.

    Every writer of a projection admits before storing, so today's rows are
    canonical — but this card's `run_id` and `thread_id` are what a client opens
    a conversation *by*, and a stored blob written by an older build is not a
    guarantee. Admitting on the way out makes it one by construction instead of
    by who happened to write the row.
    """
    from agent.message_projection import collaboration_summary

    def card(**extra):
        return collaboration_summary({"projection": dict(
            {"version": 1, "origin": "peer", "audience": "peer",
             "purpose": "collaboration", "send_id": "s1",
             "sender": "joud", "recipient": "badr"}, **extra)})

    hostile = "X" * 200
    typed = card(run_id=hostile)
    assert typed["run_id"] != hostile and a2a._RUN_ID.match(typed["run_id"])
    assert typed["run_id"] == collaboration_run("inherited", hostile)
    # And the thread the client opens is derived from the admitted run, so the
    # card and `a2a.thread` name one conversation rather than two.
    assert typed["thread_id"] == thread_id("joud", "badr", typed["run_id"])
    # A canonical run is carried through untouched.
    run = _turn("joud", "sess:task:real")
    assert card(run_id=run)["run_id"] == run
    # And a blank one still reads as the thread's own legacy run, which is what
    # keeps a pre-run exchange one card.
    legacy = card()
    assert legacy["thread_id"] == thread_id("joud", "badr")
    assert legacy["run_id"] == a2a._legacy_run(legacy["thread_id"])


def test_a_thread_is_one_shape_wherever_it_is_listed(tmp_path):
    """`a2a.runs[].threads[]` and `a2a.threads[]` describe the same object.

    They carried seven fields and eight — `run_id` on the second only — so a
    client reading a thread had to remember which listing handed it over to know
    whether the field would be there. A thread belongs to exactly one run, so
    the value costs nothing on either.
    """
    run = _turn("joud", "sess:task:shape")
    request = _fanout(tmp_path, sender="joud", targets=["badr"], run=run)[0]
    _reply(tmp_path, request, at=HOUR * 3)

    listed = threads_for(tmp_path, profile="joud")["threads"][0]
    inside = runs_for(tmp_path, profile="joud")["runs"][0]["threads"][0]
    assert set(listed) == set(inside)
    assert listed == inside
    assert inside["run_id"] == run

    # Legacy rows answer with their own legacy run on both surfaces too.
    conn = _legacy_ledger(tmp_path / "old")
    _legacy_send(conn, sender="joud", recipient="faisal", send_id="o1", body="x", at=5.0)
    conn.close()
    old_listed = threads_for(tmp_path / "old", profile="joud")["threads"][0]
    old_inside = runs_for(tmp_path / "old", profile="joud")["runs"][0]["threads"][0]
    assert old_listed == old_inside
    assert old_inside["run_id"] == a2a._legacy_run(thread_id("joud", "faisal"))
