"""A collaboration thread is one *episode*, not two agents' whole history.

Opening the collaboration between abu-saud and faisal used to show every
message they had ever exchanged, because a thread id knew only the pair. These
are the rules that replaced that, each asserted against the real send path
rather than against the derivation in isolation.
"""
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.message_projection import (
    collaboration_summary, native_metadata, projection, record_peer_final, stamp_final,
)
from gateway.a2a_threads import (
    collaboration_run, read_thread, record_send, run_for_request, send_event_id,
    thread_id, threads_for,
)
from tools import bot_mode_dm as dm
from tools import bot_mode_probe


@pytest.fixture
def team(tmp_path, monkeypatch):
    """Two teammates on one install: abu-saud sends, faisal receives."""
    root = tmp_path / ".hermes"
    for name in ("abu-saud", "faisal", "badr", "joud"):
        home = root / "profiles" / name
        home.mkdir(parents=True)
        (home / "profile.yaml").write_text("ui_meta:\n  hermes-bots:\n    shape: cloud\n")
    monkeypatch.setattr(bot_mode_probe, "is_bot_mode_managed", lambda _home=None: True)
    monkeypatch.setattr("tools.bot_mode_probe.is_bot_mode_managed", lambda _home=None: True)
    bot_mode_probe._reset_cache_for_tests()
    return root


def _agent(root, profile="abu-saud", *, turn_id="turn-1", incoming=None, work_id=None):
    return SimpleNamespace(
        _session_db=SimpleNamespace(db_path=str(root / "profiles" / profile / "state.db")),
        session_id=f"{profile}-session",
        _session_title_hint="Bot Chat",
        _bot_mode_all_sessions=True,
        _current_turn_id=turn_id,
        _turn_display_projection=incoming,
        _owner_continuity_work_id=work_id,
        _message_agent_dispatcher=None,
        tools=[], valid_tool_names=set(),
    )


def _send(root, agent, target, message, sent):
    """One real ``message_agent`` call; returns its travelling projection."""
    captured = {}

    def fake_delivery(*_a, **kwargs):
        captured["metadata"] = kwargs["display_metadata"]
        return json.dumps({"status": "sent", "process_id": "p"})

    import unittest.mock as mock
    with mock.patch.object(dm, "_start_delivery", fake_delivery):
        ack = json.loads(dm.message_agent_tool(target=target, message=message, agent=agent))
    assert ack["status"] == "sent", ack
    sent.append(captured["metadata"])
    return projection(captured["metadata"])


def _reply(root, request_projection, text, *, profile):
    """The recipient answering, through the real stamp/record finalizers."""
    child = _agent(root, profile, incoming=request_projection)
    child._persist_user_message_idx = 0
    messages = [
        {"role": "user", "content": "prompt", "display_metadata": {"projection": request_projection}},
        {"role": "assistant", "content": text},
    ]
    stamp_final(child, messages)
    record_peer_final(child, messages)
    return projection(messages[-1]["display_metadata"])


# ── The owner's defect ──────────────────────────────────────────────────────

def test_two_owner_tasks_same_pair_are_two_threads(team):
    """Run A then Run B between the same two agents stay separate episodes."""
    root, sent = team, []
    a = _send(root, _agent(root, turn_id="turn-oauth"), "faisal", "OAuth: review the PKCE flow", sent)
    _reply(root, a, "OAuth reviewed", profile="faisal")
    b = _send(root, _agent(root, turn_id="turn-finance"), "faisal", "Finance: close the Q3 books", sent)
    _reply(root, b, "Books closed", profile="faisal")

    assert a["run_id"] != b["run_id"]
    thread_a = thread_id("abu-saud", "faisal", a["run_id"])
    thread_b = thread_id("abu-saud", "faisal", b["run_id"])
    assert thread_a != thread_b

    assert [e.body for e in read_thread(root, thread=thread_a)] == [
        "OAuth: review the PKCE flow", "OAuth reviewed"]
    assert [e.body for e in read_thread(root, thread=thread_b)] == [
        "Finance: close the Q3 books", "Books closed"]
    # Zero leakage in either direction, which is the whole complaint.
    assert not any("Finance" in e.body for e in read_thread(root, thread=thread_a))
    assert not any("OAuth" in e.body for e in read_thread(root, thread=thread_b))

    # And the transcript shows two episodes at their own positions, not one.
    summaries = threads_for(root, profile="abu-saud")["threads"]
    assert [s["thread_id"] for s in summaries] == [thread_b, thread_a]
    assert {s["run_id"] for s in summaries} == {a["run_id"], b["run_id"]}
    assert all(s["counterpart"] == "faisal" for s in summaries)
    assert all(s["message_count"] == 2 for s in summaries)


def test_reply_lands_in_the_thread_of_what_it_answers(team):
    root, sent = team, []
    request = _send(root, _agent(root), "faisal", "Please check QA", sent)
    reply = _reply(root, request, "QA is green", profile="faisal")
    assert reply["run_id"] == request["run_id"]
    assert len(read_thread(root, thread=thread_id("abu-saud", "faisal", request["run_id"]))) == 2
    # Never the pair-only thread: that id now only ever names legacy history.
    assert read_thread(root, thread=thread_id("abu-saud", "faisal")) == []


# ── Lifecycle ───────────────────────────────────────────────────────────────

def test_fanout_in_one_turn_is_one_run_with_a_thread_per_peer(team):
    root, sent = team, []
    agent = _agent(root, turn_id="turn-fanout")
    runs = {t: _send(root, agent, t, f"status please, {t}", sent)["run_id"]
            for t in ("faisal", "badr", "joud")}
    assert len(set(runs.values())) == 1
    run = next(iter(runs.values()))
    threads = {thread_id("abu-saud", t, run) for t in runs}
    assert len(threads) == 3
    assert all(len(read_thread(root, thread=t)) == 1 for t in threads)
    assert {s["run_id"] for s in threads_for(root, profile="abu-saud")["threads"]} == {run}


def _completion_wake(request_projection, *, drop_run=False):
    """The projection a completion notification wakes the parent with.

    Built exactly as ``tui_gateway.session_notifications`` builds it: a hidden
    runtime control row carrying the collaboration identity that
    ``process_collaboration_identity`` joined out of the send's own ACK.
    """
    joined = {k: request_projection[k]
              for k in ("send_id", "event_id", "sender", "recipient", "run_id")
              if k in request_projection}
    if drop_run:
        joined.pop("run_id", None)
    return projection(native_metadata(
        "runtime", "internal", "control", source_session_id="abu-saud-session",
        process_id="proc-1", **joined))


def test_followup_after_a_completion_wake_continues_the_same_thread(team):
    """The reply wakes the parent; what it sends next belongs to that episode."""
    root, sent = team, []
    request = _send(root, _agent(root, turn_id="turn-1"), "faisal", "Start the OAuth work", sent)
    _reply(root, request, "Started; need the client id", profile="faisal")
    # A *different* turn, woken by the completion — the only continuity is causal.
    woken = _agent(root, turn_id="turn-2-woken", incoming=_completion_wake(request))
    followup = _send(root, woken, "faisal", "Client id is in the vault", sent)
    assert followup["run_id"] == request["run_id"]
    assert [e.body for e in read_thread(
        root, thread=thread_id("abu-saud", "faisal", request["run_id"]))] == [
        "Start the OAuth work", "Started; need the client id", "Client id is in the vault"]


def test_a_wake_with_no_run_recovers_it_from_the_ledger(team):
    """An in-flight request from the previous build still continues correctly."""
    root, sent = team, []
    request = _send(root, _agent(root, turn_id="turn-1"), "faisal", "Start the OAuth work", sent)
    woken = _agent(root, turn_id="turn-2", incoming=_completion_wake(request, drop_run=True))
    followup = _send(root, woken, "faisal", "and one more thing", sent)
    assert followup["run_id"] == request["run_id"]


def test_completion_wake_carries_the_run_out_of_the_real_ack(team):
    """``process_collaboration_identity`` is where the run joins the wake."""
    from agent.message_projection import process_collaboration_identity
    from hermes_state import SessionDB

    root, sent = team, []
    agent = _agent(root)
    agent.session_id = "source"  # the ACK must be joined within its own lineage
    request = _send(root, agent, "faisal", "anchored ask", sent)
    home = root / "profiles" / "abu-saud"
    with SessionDB(home / "state.db") as db:
        db.create_session("source", source="desktop")
        db.append_message(
            "source", "tool",
            json.dumps({"status": "sent", "process_id": "proc_native",
                        "display_metadata": sent[0]}),
            tool_name="message_agent", tool_call_id="send1")
        joined = process_collaboration_identity(db, "source", "proc_native")
    assert joined["run_id"] == request["run_id"]
    assert joined["send_id"] == request["send_id"]


def test_onward_delegation_is_the_same_run_and_a_new_thread(team):
    """A→B→C: one run, one thread per pair — the root model, exactly."""
    root, sent = team, []
    request = _send(root, _agent(root), "faisal", "Investigate the token error", sent)
    faisal = _agent(root, "faisal", turn_id="faisal-turn", incoming=request)
    onward = _send(root, faisal, "badr", "Need the gateway logs", sent)
    assert onward["run_id"] == request["run_id"]
    run = request["run_id"]
    assert thread_id("abu-saud", "faisal", run) != thread_id("faisal", "badr", run)
    assert len(read_thread(root, thread=thread_id("faisal", "badr", run))) == 1


def test_resumed_work_item_is_the_same_run_a_different_one_is_not(team):
    """Restart-safe continuity: the work id proves it, not the clock."""
    root, sent = team, []
    first = _send(root, _agent(root, turn_id="t1", work_id="work-42"), "faisal", "Ship the migration", sent)
    resumed = _send(root, _agent(root, turn_id="t9-after-restart", work_id="work-42"),
                    "faisal", "Any progress on the migration?", sent)
    other = _send(root, _agent(root, turn_id="t10", work_id="work-99"), "faisal", "Unrelated ask", sent)
    assert first["run_id"] == resumed["run_id"]
    assert other["run_id"] != first["run_id"]
    assert len(read_thread(root, thread=thread_id("abu-saud", "faisal", first["run_id"]))) == 2
    assert len(read_thread(root, thread=thread_id("abu-saud", "faisal", other["run_id"]))) == 1


def test_run_never_comes_from_what_a_message_says(team):
    """Identical prose in two turns is two runs; different prose in one is one."""
    root, sent = team, []
    same_text_1 = _send(root, _agent(root, turn_id="t1"), "faisal", "look at the login bug", sent)
    same_text_2 = _send(root, _agent(root, turn_id="t2"), "faisal", "look at the login bug", sent)
    assert same_text_1["run_id"] != same_text_2["run_id"]

    one_turn = _agent(root, turn_id="t3")
    a = _send(root, one_turn, "faisal", "totally unrelated wording here", sent)
    b = _send(root, one_turn, "badr", "nothing in common with the above", sent)
    assert a["run_id"] == b["run_id"]


# ── Legacy ──────────────────────────────────────────────────────────────────

def test_legacy_rows_keep_their_ids_and_no_new_message_joins_them(team):
    root, sent = team, []
    legacy = record_send(root, sender="abu-saud", recipients=["faisal"],
                         body="old finance discussion", send_id="legacy-1")
    assert legacy[0].run_id == ""
    assert legacy[0].thread_id == thread_id("abu-saud", "faisal")  # unmoved

    fresh = _send(root, _agent(root, turn_id="turn-new"), "faisal", "new OAuth ask", sent)
    assert fresh["run_id"]
    assert [e.body for e in read_thread(root, thread=thread_id("abu-saud", "faisal"))] == [
        "old finance discussion"]
    assert [e.body for e in read_thread(
        root, thread=thread_id("abu-saud", "faisal", fresh["run_id"]))] == ["new OAuth ask"]


def test_a_reply_to_a_legacy_request_stays_with_its_request(team):
    """No run to inherit is not a licence to open a typed thread of one half."""
    root = team
    record_send(root, sender="abu-saud", recipients=["faisal"], body="legacy ask",
                send_id="legacy-send")
    stale = native_metadata(
        "peer", "peer", "collaboration", sender="abu-saud", recipient="faisal",
        send_id="legacy-send", event_id=send_event_id("abu-saud", "faisal", "legacy-send"),
    )
    reply = _reply(root, projection(stale), "legacy answer", profile="faisal")
    assert reply.get("run_id", "") == ""
    assert [e.body for e in read_thread(root, thread=thread_id("abu-saud", "faisal"))] == [
        "legacy ask", "legacy answer"]


def test_run_for_request_reads_the_ledger_and_never_guesses(team):
    root, sent = team, []
    request = _send(root, _agent(root), "faisal", "anchored", sent)
    assert run_for_request(root, sender="abu-saud", recipient="faisal",
                           send_id=request["send_id"]) == request["run_id"]
    assert run_for_request(root, sender="abu-saud", recipient="faisal", send_id="never") == ""
    assert run_for_request(root, sender="", recipient="faisal", send_id="x") == ""


def test_pre_run_database_migrates_without_moving_a_single_row(team):
    """A ledger written by the previous build opens, reads and keeps its ids."""
    root = team
    root.mkdir(parents=True, exist_ok=True)
    db = root / "a2a_threads.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """CREATE TABLE a2a_events (
               event_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, sender TEXT NOT NULL,
               recipient TEXT NOT NULL, body TEXT NOT NULL,
               attachments TEXT NOT NULL DEFAULT '[]', sent_at REAL NOT NULL,
               send_id TEXT NOT NULL DEFAULT '', fanout INTEGER NOT NULL DEFAULT 1);"""
    )
    conn.execute(
        "INSERT INTO a2a_events VALUES (?,?,?,?,?,?,?,?,?)",
        ("old-event", thread_id("abu-saud", "faisal"), "abu-saud", "faisal",
         "written before runs existed", "[]", 1.0, "old-send", 1),
    )
    conn.commit()
    conn.close()

    events = read_thread(root, thread=thread_id("abu-saud", "faisal"))
    assert [(e.event_id, e.run_id, e.body) for e in events] == [
        ("old-event", "", "written before runs existed")]
    # The *stored* run stays blank — nothing rewrites history — while the
    # listing names the run a client opens the thread by. A legacy thread's run
    # is its own thread, so the two cannot disagree and no client has to know
    # which side of the migration a row came from.
    from gateway.a2a_threads import _legacy_run, resolve_thread
    summary = threads_for(root, profile="abu-saud")["threads"][0]
    assert summary["run_id"] == _legacy_run(thread_id("abu-saud", "faisal"))
    assert resolve_thread(root, profile="abu-saud", counterpart="faisal",
                          run=summary["run_id"]) == thread_id("abu-saud", "faisal")


# ── The identity other surfaces consume ─────────────────────────────────────

def test_collaboration_summary_names_the_same_run_and_thread(team):
    """The aggregation card and the thread it opens agree by construction."""
    root, sent = team, []
    request = _send(root, _agent(root), "faisal", "one ask", sent)
    summary = collaboration_summary(sent[0])
    assert summary["run_id"] == request["run_id"]
    assert summary["thread_id"] == thread_id("abu-saud", "faisal", request["run_id"])
    assert read_thread(root, thread=summary["thread_id"])[0].body == "one ask"


def test_the_wire_names_the_run_on_both_reads(team, monkeypatch):
    """The two RPCs a client uses to open a collaboration both say which one."""
    from tui_gateway import server as gateway_server

    root, sent = team, []
    a = _send(root, _agent(root, turn_id="turn-oauth"), "faisal", "OAuth ask", sent)
    b = _send(root, _agent(root, turn_id="turn-finance"), "faisal", "Finance ask", sent)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: str(root))

    listing = gateway_server.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "a2a.threads",
         "params": {"profile": "abu-saud"}})["result"]["threads"]
    assert {row["run_id"] for row in listing} == {a["run_id"], b["run_id"]}
    assert [row["counterpart"] for row in listing] == ["faisal", "faisal"]

    older = next(row for row in listing if row["run_id"] == a["run_id"])
    opened = gateway_server.handle_request(
        {"jsonrpc": "2.0", "id": 2, "method": "a2a.thread",
         "params": {"thread_id": older["thread_id"]}})["result"]
    assert opened["run_id"] == a["run_id"]
    assert [event["text"] for event in opened["events"]] == ["OAuth ask"]
    assert all(event["run_id"] == a["run_id"] for event in opened["events"])


def test_run_derivation_is_typed_and_rejects_an_unknown_anchor():
    assert collaboration_run("turn", "abu-saud", "t1") == collaboration_run("turn", "abu-saud", "t1")
    assert collaboration_run("turn", "abu-saud", "t1") != collaboration_run("work", "abu-saud", "t1")
    assert collaboration_run("work", "abu-saud", "") == ""
    with pytest.raises(ValueError):
        collaboration_run("topic", "oauth")
