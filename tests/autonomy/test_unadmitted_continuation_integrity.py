"""An unadmitted continuation dispatch is transport failure, not Owner speech.

``dispatch["admitted"]`` is stamped by ``bind_turn`` after the native canonical
turn-lease admits the resumed session and before the first model request. Its
absence is therefore native proof that no model turn ran for that dispatch: no
task action was attempted, no external effect exists and nothing was replayed.

Before this contract every such failure wrote an Owner-facing assistant line
claiming "Existing effects need inspection before retry" into the canonical Bot
Chat and parked the task in ``needs_owner``, which ``validate_transition`` only
releases against a NEW source-bound Owner decision. One transport failure became
permanent "Needs you" plus a paragraph the Owner could not act on.
"""

import shlex
import time
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parents[2]

import pytest

from hermes_state import SessionDB
from agent.autonomy import owner_continuity as oc, store


REQUEST = "Deploy the build and report the verified result when it finishes."


def due_work(home, *, session="owner-source"):
    """A registered Owner task whose durable wake condition is already due."""
    db = SessionDB(home / "state.db")
    db.create_session(session, source="desktop")
    mid = db.append_message(session, "user", REQUEST)
    work = oc.register_owner_request(db, session, mid, hermes_home=home)
    oc.wait(work["id"], until="2020-01-01T00:00:00Z", hermes_home=home)
    return db, work


def failing_runner(*args, **kwargs):
    raise RuntimeError("native continuation could not start")


def admitting_runner(home, work_id):
    """Stand in for ``bind_turn``: admit the dispatch, then die mid-turn."""

    def run(*args, **kwargs):
        current = store.get_work(work_id, home)
        dispatch = current["refs"]["dispatch"]
        store.update_work(
            work_id,
            refs={"dispatch": {**dispatch, "admitted": True},
                  "ever_admitted": True, "resume_attempts": 0},
            expected_state="working",
            expected_refs={
                "dispatch": dispatch,
                "pending_owner_result": None,
                "resume_generation": dispatch["generation"],
            },
            hermes_home=home,
        )
        raise RuntimeError("model turn died after admission")

    return run


def owner_rows(db, session="owner-source"):
    """Every Owner-visible assistant line in the canonical transcript."""
    from agent.message_projection import owner_visible

    return [
        row
        for row in db.get_messages(session, include_inactive=True)
        if row.get("role") == "assistant"
        and owner_visible(
            row.get("role"),
            row.get("content"),
            row.get("display_kind"),
            row.get("display_metadata"),
        )
    ]


def test_unadmitted_failure_rearms_the_wait_and_says_nothing_to_the_owner(autonomy_home):
    db, work = due_work(autonomy_home)
    try:
        oc.run_resume(work["id"], 1, hermes_home=autonomy_home, runner=failing_runner)
        current = store.get_work(work["id"], autonomy_home)
        # The responsibility is retained under a fresh generation fence, not escalated.
        assert current["state"] == "waiting"
        assert current["refs"]["resume_generation"] == 2
        assert current["refs"]["resume_attempts"] == 1
        assert not current["refs"].get("pending_owner_result")
        assert current["refs"]["owner_request"] == work["refs"]["owner_request"]
        # Nothing reached the Owner: no result, no decision, no chat line.
        assert oc.deliver_pending(store.get_work(work["id"], autonomy_home), autonomy_home) is False
        assert owner_rows(db) == []
    finally:
        db.close()


def test_unadmitted_failure_escalates_once_at_the_ceiling(autonomy_home):
    db, work = due_work(autonomy_home)
    try:
        # Literal 3, not the constant: against the base build this must fail on
        # the assertion below, not on looking the constant up.
        for generation in (1, 2, 3):
            oc.run_resume(
                work["id"], generation, hermes_home=autonomy_home, runner=failing_runner
            )
        pending = store.get_work(work["id"], autonomy_home)["refs"]["pending_owner_result"]
        # The Owner is told the truth: nothing ran, so there is nothing to inspect.
        assert "that attempt ran no step and replayed nothing" in pending["text"]
        assert "Existing effects need inspection" not in pending["text"]

        assert oc.deliver_pending(store.get_work(work["id"], autonomy_home), autonomy_home)
        settled = store.get_work(work["id"], autonomy_home)
        # The row stays needs_owner so the Owner's next reply can still rebind
        # it -- a terminal state would freeze it and lose the responsibility --
        # but it is marked as raised by the transport, not by a real question.
        assert settled["state"] == "needs_owner"
        # And it KEEPS its badge. Reaching here means the retry budget is spent:
        # Hermes tried three times, could not continue, and has stopped. Only
        # the Owner can restart it, which is a real obligation. The single
        # failure that started this incident never reaches here -- it re-arms.
        assert not settled["refs"].get(oc.UNSTARTED_CONTINUATION)
        assert len(owner_rows(db)) == 1
    finally:
        db.close()


def test_a_single_transport_failure_never_reaches_the_owner_at_all(autonomy_home):
    """The incident, in one test.

    The pre-fix build escalated on the FIRST failed dispatch: an Owner-facing
    paragraph and a permanent badge for a continuation that had run nothing.
    Now that failure re-arms silently, and the Owner learns nothing because
    there is nothing to learn yet.
    """
    from tui_gateway.activity import work_state

    db, work = due_work(autonomy_home)
    try:
        oc.run_resume(work["id"], 1, hermes_home=autonomy_home, runner=failing_runner)
        current = store.get_work(work["id"], autonomy_home)
        assert current["state"] == "waiting"
        assert not current["refs"].get("pending_owner_result")
        assert owner_rows(db) == []
        assert work_state(autonomy_home) != "needs_owner"
    finally:
        db.close()


def test_a_real_owner_question_still_lights_needs_you(autonomy_home):
    """The guard is scoped to the marker; a genuine escalation is untouched."""
    from tui_gateway.activity import work_state

    db, work = due_work(autonomy_home)
    try:
        oc.run_resume(
            work["id"],
            1,
            hermes_home=autonomy_home,
            runner=admitting_runner(autonomy_home, work["id"]),
        )
        assert oc.deliver_pending(store.get_work(work["id"], autonomy_home), autonomy_home)
        settled = store.get_work(work["id"], autonomy_home)
        assert settled["state"] == "needs_owner"
        assert oc.UNSTARTED_CONTINUATION not in settled["refs"]
        assert work_state(autonomy_home) == "needs_owner"
    finally:
        db.close()


def test_an_unstarted_escalation_is_still_rebindable_by_the_owner(autonomy_home):
    """A terminal state would have frozen the row and dropped the obligation."""
    db, work = due_work(autonomy_home)
    try:
        for generation in (1, 2, 3):
            oc.run_resume(
                work["id"], generation, hermes_home=autonomy_home, runner=failing_runner
            )
        assert oc.deliver_pending(store.get_work(work["id"], autonomy_home), autonomy_home)
        # The Owner reads the line and answers it in the canonical chat.
        mid = db.append_message("owner-source", "user", "كمل")
        rebound = oc._bind_owner_decision(db, "owner-source", mid)
        assert rebound is not None
        assert rebound["id"] == work["id"]
        assert rebound["state"] == "working"
        # And the marker is gone with it: the task is moving again.
        assert not rebound["refs"].get(oc.UNSTARTED_CONTINUATION)
    finally:
        db.close()


def test_a_worker_killed_before_admission_reports_the_same_truth(autonomy_home):
    """`_recover_orphan`: the dispatch was SIGKILLed, so `run_resume` never ran."""
    db, work = due_work(autonomy_home)
    try:
        store.update_work(
            work["id"],
            state="working",
            refs={
                "heartbeat_at": time.time() - 1000,
                "dispatch": {"generation": 1, "nonce": "n", "pid": 999999999,
                             "process_started_at": 1},
            },
            hermes_home=autonomy_home,
        )
        store.update_work(work["id"], refs={"pending_owner_result": None},
                          hermes_home=autonomy_home)
        oc._recover_orphan(store.get_work(work["id"], autonomy_home), autonomy_home)
        recovered = store.get_work(work["id"], autonomy_home)
        assert recovered["state"] == "waiting"
        assert not recovered["refs"].get("pending_owner_result")
        assert recovered["refs"]["lifecycle"]["attempts"]["n"]["admitted"] is False
        assert not store.get_work(work["id"], autonomy_home)["refs"].get(
            oc.UNSTARTED_CONTINUATION)
    finally:
        db.close()


def test_a_cli_that_exits_clean_without_admitting_reports_the_same_truth(autonomy_home):
    """`_settle_unfinished`: returncode 0, but `bind_turn` never ran."""
    db, work = due_work(autonomy_home)
    try:
        # A dispatch that ran the CLI to a clean exit without ever admitting.
        store.update_work(
            work["id"],
            state="working",
            refs={
                "resume_attempts": 3,
                "dispatch": {"generation": 1, "nonce": "n", "pid": 1},
            },
            hermes_home=autonomy_home,
        )
        oc._settle_unfinished(
            store.get_work(work["id"], autonomy_home), autonomy_home
        )
        pending = store.get_work(work["id"], autonomy_home)["refs"]["pending_owner_result"]
        assert "existing effects and any pending approval need review" not in pending["text"]
        assert "that attempt ran no step and replayed nothing" in pending["text"]
        assert not store.get_work(work["id"], autonomy_home)["refs"].get(
            oc.UNSTARTED_CONTINUATION)
    finally:
        db.close()


def test_admitted_failure_keeps_the_unverified_outcome_escalation(autonomy_home):
    """A turn that really started may have left effects only the Owner can judge."""
    db, work = due_work(autonomy_home)
    try:
        oc.run_resume(
            work["id"],
            1,
            hermes_home=autonomy_home,
            runner=admitting_runner(autonomy_home, work["id"]),
        )
        pending = store.get_work(work["id"], autonomy_home)["refs"]["pending_owner_result"]
        assert pending["state"] == "needs_owner"
        assert "Existing effects need inspection before retry" in pending["text"]
        assert "no action was replayed" in pending["text"]

        assert oc.deliver_pending(store.get_work(work["id"], autonomy_home), autonomy_home)
        assert store.get_work(work["id"], autonomy_home)["state"] == "needs_owner"
        from agent.message_projection import projection

        native = projection(owner_rows(db)[0]["display_metadata"])
        assert native["purpose"] == "decision"
    finally:
        db.close()


def test_admitted_failure_never_replays_the_task_and_stays_one_logical_message(
    autonomy_home,
):
    """Repeated delivery of one settled outcome is idempotent by work identity."""
    db, work = due_work(autonomy_home)
    try:
        oc.run_resume(
            work["id"],
            1,
            hermes_home=autonomy_home,
            runner=admitting_runner(autonomy_home, work["id"]),
        )
        settled = store.get_work(work["id"], autonomy_home)
        assert oc.deliver_pending(settled, autonomy_home)
        # A reconnecting/redelivering caller replays the SAME pending outcome.
        assert oc.deliver_pending(settled, autonomy_home) is False
        assert oc.deliver_pending(settled, autonomy_home) is False
        rows = owner_rows(db)
        assert len(rows) == 1
        metadata = rows[0]["display_metadata"]
        assert metadata["owner_work_id"] == work["id"]
    finally:
        db.close()


def test_two_distinct_tasks_with_identical_text_stay_two_logical_messages(autonomy_home):
    """Reconciliation is by work identity, never by comparing the prose."""
    db = SessionDB(autonomy_home / "state.db")
    try:
        db.create_session("owner-source", source="desktop")
        works = []
        for _ in range(2):
            mid = db.append_message("owner-source", "user", REQUEST)
            work = oc.register_owner_request(
                db, "owner-source", mid, hermes_home=autonomy_home, force=True
            )
            oc.wait(work["id"], until="2020-01-01T00:00:00Z", hermes_home=autonomy_home)
            oc.run_resume(
                work["id"],
                1,
                hermes_home=autonomy_home,
                runner=admitting_runner(autonomy_home, work["id"]),
            )
            assert oc.deliver_pending(
                store.get_work(work["id"], autonomy_home), autonomy_home
            )
            works.append(work["id"])

        rows = owner_rows(db)
        # Byte-identical outcomes, two different responsibilities: both survive.
        assert len(rows) == 2
        assert rows[0]["content"] == rows[1]["content"]
        assert [row["display_metadata"]["owner_work_id"] for row in rows] == works
        assert rows[0]["id"] != rows[1]["id"]
    finally:
        db.close()


def _stranded_row(autonomy_home, outcome, *, legacy=False):
    """Drive a real row to a stranded escalation through the production path.

    ``legacy`` rewrites it into the shape a PRE-FIX build left behind: the first
    cycle, its counters, and no record of whether the Owner's own turn acted.
    That last absence is the whole difference, and the repair tool must treat
    the two shapes differently.
    """
    db, work = due_work(autonomy_home)
    for generation in (1, 2, 3):
        oc.run_resume(
            work["id"], generation, hermes_home=autonomy_home, runner=failing_runner
        )
    assert oc.deliver_pending(store.get_work(work["id"], autonomy_home), autonomy_home)
    current = store.get_work(work["id"], autonomy_home)
    refs = {oc.UNSTARTED_CONTINUATION: None}
    if legacy:
        refs.update({
            "ever_admitted": None,
            "ever_acted": None,
            "effects_tracked": None,
            "resume_generation": 1,
            "resume_attempts": 1,
            "dispatch": {**current["refs"]["dispatch"], "generation": 1},
        })
    store.update_work(
        work["id"], completion_result=outcome, refs=refs, hermes_home=autonomy_home
    )
    return db, work


def _repair_tool(name="repair_unadmitted"):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        name,
        Path(__file__).resolve().parents[2] / "tools" / "repair_unadmitted_owner_escalations.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_repair_marks_only_stranded_rows_and_is_idempotent(autonomy_home):
    from tui_gateway.activity import work_state

    repair = _repair_tool()
    db, work = _stranded_row(autonomy_home, repair.UNSTARTED_OUTCOMES[0], legacy=True)
    named = ["--adjudicated", work["id"], "--reason", "transcript shows nothing pending"]
    try:
        assert work_state(autonomy_home) == "needs_owner"
        # Nothing is taken automatically, however clean the shape looks.
        assert repair.stranded(autonomy_home)[0] == []
        assert [row["id"] for row in repair.stranded(autonomy_home, {work["id"]})[0]] \
            == [work["id"]]

        assert repair.main(["--root", str(autonomy_home), "--apply", *named]) == 0
        settled = store.get_work(work["id"], autonomy_home)
        # Marked, not deleted, not moved out of needs_owner.
        assert settled["state"] == "needs_owner"
        assert settled["completion_result"] == repair.UNSTARTED_OUTCOMES[0]
        assert settled["refs"]["owner_request"] == work["refs"]["owner_request"]
        assert settled["refs"][repair.MARKER]["repaired_by"]
        assert work_state(autonomy_home) is None
        # The Owner-facing history is untouched.
        assert len(owner_rows(db)) == 1

        # Idempotent: a second pass selects nothing and un-marks nothing.
        assert repair.stranded(autonomy_home, {work["id"]})[0] == []
        assert repair.wrongly_marked(autonomy_home) == []
        assert repair.main(["--root", str(autonomy_home), "--apply", *named]) == 0
        assert store.get_work(work["id"], autonomy_home)["refs"][repair.MARKER]
    finally:
        db.close()


def test_adjudication_cannot_reach_a_row_that_is_not_this_defects_shape(autonomy_home):
    """Naming a row is permission to trust a transcript, not to skip the bound."""
    repair = _repair_tool("repair_adjudication")
    db, work = _stranded_row(autonomy_home, repair.UNSTARTED_OUTCOMES[0], legacy=True)
    try:
        # A real Owner question, delivered: outside the population entirely.
        store.update_work(work["id"],
                          completion_result="Which production credential should I use?",
                          hermes_home=autonomy_home)
        selected, _held = repair.stranded(autonomy_home, {work["id"]})
        assert selected == []
        assert not store.get_work(work["id"], autonomy_home)["refs"].get(repair.MARKER)
    finally:
        db.close()


def test_unmark_restores_every_badge_the_current_predicate_would_not_take(autonomy_home):
    """The complement, not a special case.

    The first version of this pass only looked for recorded effects, so a row
    marked by an earlier, weaker predicate stayed hidden as long as the ledger
    held no process handle -- which is exactly the shape the predicate later
    stopped accepting. A marker the tool would not write today comes off today.
    """
    from tui_gateway.activity import work_state

    repair = _repair_tool("repair_unmark")
    db, work = _stranded_row(autonomy_home, repair.UNSTARTED_OUTCOMES[0])
    try:
        # Exactly what the earlier apply left behind: a marker with no adjudication.
        store.update_work(
            work["id"],
            refs={repair.MARKER: {
                "attempts": 1, "at": time.time(),
                "repaired_by": "repair_unadmitted_owner_escalations",
                "delivery": store.get_work(work["id"], autonomy_home)[
                    "refs"]["owner_delivery"]["message_id"]}},
            hermes_home=autonomy_home,
        )
        assert work_state(autonomy_home) is None
        assert [row for row, _m, _why in repair.wrongly_marked(autonomy_home)] == [work["id"]]
        assert repair.main(["--root", str(autonomy_home), "--apply"]) == 0
        assert not store.get_work(work["id"], autonomy_home)["refs"].get(repair.MARKER)
        assert work_state(autonomy_home) == "needs_owner"
    finally:
        db.close()


def test_repair_will_not_write_over_a_question_raised_after_it_looked(autonomy_home):
    """Selection reads; applying writes. Between them the row can change.

    The Owner replies, the task rebinds, runs, and delivers a REAL question --
    which returns the row to ``needs_owner``. Checking only the state would let
    a marker chosen for the old escalation land on the new question and take its
    badge away.
    """
    repair = _repair_tool("repair_race")
    db, work = _stranded_row(autonomy_home, repair.UNSTARTED_OUTCOMES[0], legacy=True)
    try:
        selected, _ = repair.stranded(autonomy_home, {work["id"]})
        assert [row["id"] for row in selected] == [work["id"]]
        row = selected[0]

        # The row moves on: a new Owner decision and a new delivered question.
        store.update_work(
            work["id"],
            completion_result="Which production credential should I use?",
            refs={"owner_decision": {"session_id": "owner-source", "message_id": "9",
                                     "at": time.time()},
                  "owner_delivery": {"message_id": 99, "source_session_id": "owner-source"}},
            hermes_home=autonomy_home,
        )
        assert store.update_work(
            work["id"],
            refs={repair.MARKER: {"attempts": row["resume_attempts"], "at": time.time(),
                                  "repaired_by": "repair_unadmitted_owner_escalations"}},
            hermes_home=autonomy_home,
            expected_state="needs_owner",
            expected_refs=row["expected_refs"],
        ) is None
    finally:
        db.close()


def test_repair_leaves_a_real_owner_question_alone(autonomy_home):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "repair_unadmitted2",
        Path(__file__).resolve().parents[2] / "tools" / "repair_unadmitted_owner_escalations.py",
    )
    repair = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(repair)

    db, work = due_work(autonomy_home)
    try:
        oc.run_resume(
            work["id"], 1, hermes_home=autonomy_home,
            runner=admitting_runner(autonomy_home, work["id"]),
        )
        assert oc.deliver_pending(store.get_work(work["id"], autonomy_home), autonomy_home)
        # Same sentence, but the dispatch WAS admitted: a real question stands.
        assert repair.stranded(autonomy_home)[0] == []
    finally:
        db.close()



def test_an_ordinary_owner_turn_is_never_reported_as_never_having_run(autonomy_home):
    """The half of the predicate that says a dispatch must EXIST.

    An Owner's own first turn writes no dispatch at all (``bind_turn``'s
    ordinary path), and ``_bind_owner_decision`` clears it on every rebind. That
    turn really ran: it can have sent mail or deployed. Reading its missing
    dispatch as "not admitted" told the Owner their work never happened AND
    suppressed the badge on the one row that genuinely needs them.
    """
    from tui_gateway.activity import work_state

    db, work = due_work(autonomy_home)
    try:
        assert not oc.dispatch_never_started(store.get_work(work["id"], autonomy_home))
        store.update_work(
            work["id"],
            state="working",
            refs={"resume_attempts": 3, "heartbeat_at": time.time() - 1000,
                  "owner_process": {"pid": 999999999, "started_at": 1}},
            hermes_home=autonomy_home,
        )
        current = store.get_work(work["id"], autonomy_home)
        assert current["refs"].get("dispatch") is None

        oc._settle_unfinished(current, autonomy_home, interrupted=True)
        pending = store.get_work(work["id"], autonomy_home)["refs"]["pending_owner_result"]
        assert "that attempt ran no step" not in pending["text"]
        assert "existing effects and any pending approval need review" in pending["text"]

        assert oc.deliver_pending(store.get_work(work["id"], autonomy_home), autonomy_home)
        settled = store.get_work(work["id"], autonomy_home)
        assert oc.UNSTARTED_CONTINUATION not in settled["refs"]
        # A real turn that may have left effects still asks for the Owner.
        assert work_state(autonomy_home) == "needs_owner"
    finally:
        db.close()


def test_a_dead_worker_with_no_dispatch_keeps_the_inspection_warning(autonomy_home):
    """`_recover_orphan` reaches a first-turn row through ``owner_process``."""
    db, work = due_work(autonomy_home)
    try:
        store.update_work(
            work["id"],
            state="working",
            refs={"heartbeat_at": time.time() - 1000,
                  "owner_process": {"pid": 999999999, "started_at": 1}},
            hermes_home=autonomy_home,
        )
        current = store.get_work(work["id"], autonomy_home)
        assert current["refs"].get("dispatch") is None
        oc._recover_orphan(current, autonomy_home)
        pending = store.get_work(work["id"], autonomy_home)["refs"]["pending_owner_result"]
        assert "Existing effects and pending approvals must be inspected" in pending["text"]
        assert "that attempt ran no step" not in pending["text"]
        assert oc.UNSTARTED_CONTINUATION not in store.get_work(work["id"], autonomy_home)["refs"]
    finally:
        db.close()



def test_repair_holds_back_a_row_that_may_have_run_before(autonomy_home):
    """`wait()` clears `dispatch`, so a later cycle looks like a first one.

    A generation or attempt count above the first cycle is the only ledger
    evidence that the row ever returned to `waiting` — which an admitted cycle
    does. Those rows are reported, never de-badged.
    """
    import importlib.util
    from pathlib import Path
    from tui_gateway.activity import work_state

    spec = importlib.util.spec_from_file_location(
        "repair_unadmitted3",
        Path(__file__).resolve().parents[2] / "tools" / "repair_unadmitted_owner_escalations.py",
    )
    repair = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(repair)

    db, work = _stranded_row(autonomy_home, repair.UNSTARTED_OUTCOMES[0])
    try:
        # The abu-saud shape on the live roster: many cycles, so an earlier one
        # could have deployed.
        store.update_work(
            work["id"],
            refs={"resume_generation": 8, "resume_attempts": 7,
                  "dispatch": {**store.get_work(work["id"], autonomy_home)["refs"]["dispatch"], "generation": 8}},
            hermes_home=autonomy_home,
        )
        selected, deferred = repair.stranded(autonomy_home)
        assert selected == []
        assert [row["id"] for row in deferred] == [work["id"]]
        assert repair.main(["--root", str(autonomy_home), "--apply"]) == 0
        settled = store.get_work(work["id"], autonomy_home)
        assert not settled["refs"].get(repair.MARKER)
        assert settled["state"] == "needs_owner"
        assert work_state(autonomy_home) == "needs_owner"
    finally:
        db.close()


def test_a_task_that_already_ran_is_never_told_nothing_changed(autonomy_home, monkeypatch):
    """The sticky admission bit. `wait()` clears `dispatch`; this must not."""
    from tui_gateway.activity import work_state

    db, work = due_work(autonomy_home)
    try:
        # Cycle 1 is admitted through the real bind_turn and really runs.
        _dispatch_only(autonomy_home, work, attempts=1)
        assert oc.ever_admitted(
            _admit_through_bind_turn(autonomy_home, db, work, monkeypatch))
        # It re-arms, which clears `dispatch` entirely.
        store.update_work(work["id"], state="working",
                          refs={"dispatch": None, "pending_owner_result": None,
                                "heartbeat_at": time.time() - 1000,
                                "owner_process": {"pid": 999999999, "started_at": 1}},
                          hermes_home=autonomy_home)
        current = store.get_work(work["id"], autonomy_home)
        assert current["refs"].get("dispatch") is None
        assert oc.dispatch_never_started(current) is False

        oc._recover_orphan(current, autonomy_home)
        pending = store.get_work(work["id"], autonomy_home)["refs"]["pending_owner_result"]
        assert "nothing about it has changed" not in pending["text"]
        assert oc.deliver_pending(store.get_work(work["id"], autonomy_home), autonomy_home)
        settled = store.get_work(work["id"], autonomy_home)
        assert oc.UNSTARTED_CONTINUATION not in settled["refs"]
        assert work_state(autonomy_home) == "needs_owner"
    finally:
        db.close()


def _admit_through_bind_turn(autonomy_home, db, work, monkeypatch):
    """Drive the REAL `bind_turn` admission CAS, not a stand-in.

    The stand-in in this file writes the same refs, so asserting against it
    would only test the stand-in — the trap that hid the last two findings.
    """
    import os
    current = store.get_work(work["id"], autonomy_home)
    dispatch = current["refs"]["dispatch"]
    monkeypatch.setenv("HERMES_OWNER_CONTINUATION_ID", work["id"])
    monkeypatch.setenv("HERMES_OWNER_CONTINUATION_NONCE", dispatch["nonce"])
    agent = SimpleNamespace(_session_db=db, session_id="owner-source")
    oc.bind_turn(agent, {"role": "user", "content": "continue"})
    return store.get_work(work["id"], autonomy_home)


def _dispatch_only(autonomy_home, work, attempts):
    """Put the row in `working` with an unadmitted dispatch, as run_resume does."""
    store.update_work(
        work["id"], state="working",
        refs={"dispatch": {"generation": 1, "nonce": "n", "pid": 1},
              "resume_generation": 1, "resume_attempts": attempts,
              "pending_owner_result": None},
        hermes_home=autonomy_home,
    )
    return store.get_work(work["id"], autonomy_home)


def test_admission_resets_the_transport_retry_budget(autonomy_home, monkeypatch):
    """The budget bounds the TRANSPORT, not the task's lifetime.

    Only an Owner reply used to reset it, so three legitimate resume cycles left
    a long-running task with zero retries for the first real transport failure.
    """
    db, work = due_work(autonomy_home)
    try:
        _dispatch_only(autonomy_home, work, attempts=3)
        admitted = _admit_through_bind_turn(autonomy_home, db, work, monkeypatch)
        assert admitted["refs"]["dispatch"]["admitted"] is True
        assert admitted["refs"]["resume_attempts"] == 0
        assert oc.ever_admitted(admitted)
    finally:
        db.close()


def test_a_dead_continuation_job_is_re_armed_not_escalated(autonomy_home):
    """`reconcile`'s paused/completed-job branch was a fourth producer.

    A one-shot goes `enabled=False, state="completed"` after every run, so this
    fired for a job that failed to start as readily as for one the Owner paused
    — with no retry and no marker, pinning "Needs you" on the first tick.
    """
    from cron.jobs import list_jobs, update_job, use_cron_store
    from tui_gateway.activity import work_state

    db, work = due_work(autonomy_home)
    try:
        assert oc.reconcile(autonomy_home) == 1
        with use_cron_store(autonomy_home):
            job = next(j for j in list_jobs(include_disabled=True)
                       if str(j.get("name", "")).startswith("owner-continuation:"))
            update_job(job["id"], {"enabled": False, "state": "error"})
        before = store.get_work(work["id"], autonomy_home)
        assert int(before["refs"].get("resume_attempts") or 0) == 0

        oc.reconcile(autonomy_home)
        after = store.get_work(work["id"], autonomy_home)
        # Re-armed, not escalated: the dead job is dropped so the next tick can
        # mint a fresh one, and the Owner is told nothing.
        assert not after["refs"].get("pending_owner_result")
        assert after["state"] != "needs_owner"
        assert int(after["refs"]["resume_attempts"]) == 1
        assert after["refs"].get("resume_job") in (None, {})
        assert work_state(autonomy_home) != "needs_owner"
    finally:
        db.close()


def test_a_task_whose_own_turn_acted_is_never_de_badged(autonomy_home):
    """Nothing takes a badge off automatically any more.

    The predicate that used to decide this asked whether the ledger recorded the
    task acting, and three rounds of adversarial review showed that question
    cannot be answered from outside the process that ran the tool. So it is not
    asked: an escalation that reaches the Owner means the retry budget is spent,
    which is an obligation whatever the task did earlier.
    """
    from tui_gateway.activity import work_state

    db, work = due_work(autonomy_home)
    try:
        target = autonomy_home / "artifact.txt"
        target.write_text("deployed", encoding="utf-8")
        oc.verify(work["id"], file=str(target), hermes_home=autonomy_home)
        oc.wait(work["id"], until="2020-01-01T00:00:00Z", hermes_home=autonomy_home)

        for generation in (2, 3, 4):
            oc.run_resume(work["id"], generation, hermes_home=autonomy_home,
                          runner=failing_runner)
        assert oc.deliver_pending(store.get_work(work["id"], autonomy_home), autonomy_home)
        settled = store.get_work(work["id"], autonomy_home)
        assert settled["state"] == "needs_owner"
        assert not settled["refs"].get(oc.UNSTARTED_CONTINUATION)
        assert work_state(autonomy_home) == "needs_owner"
    finally:
        db.close()

def test_no_wording_claims_the_whole_task_is_unchanged(autonomy_home):
    """The ledger cannot support that claim, so no sentence may make it."""
    for admitted in (True, False):
        for ran_before in (True, False):
            text = oc._interrupted_result(admitted, 3, ran_before)
            assert "nothing about it has changed" not in text
            assert "The task is unfinished and nothing" not in text


def test_a_dead_continuation_job_actually_mints_a_fresh_one(autonomy_home):
    """The re-arm has to change the job NAME, or nothing is ever retried.

    `existing` is looked up by `owner-continuation:<id>:<generation>`, so
    clearing `resume_job` alone left it truthy forever and `create_job`
    unreachable: a bounded loop that never retried and then died with no badge.
    """
    from cron.jobs import list_jobs, update_job, use_cron_store

    db, work = due_work(autonomy_home)
    try:
        assert oc.reconcile(autonomy_home) == 1
        with use_cron_store(autonomy_home):
            job = next(j for j in list_jobs(include_disabled=True)
                       if str(j.get("name", "")).startswith("owner-continuation:"))
            update_job(job["id"], {"enabled": False, "state": "error"})

        # Tick 2 re-arms under a NEW generation...
        oc.reconcile(autonomy_home)
        after = store.get_work(work["id"], autonomy_home)
        assert after["refs"]["resume_generation"] == 2
        assert int(after["refs"]["resume_attempts"]) == 1

        # ...and tick 3 mints a live job for it, which is the actual retry.
        assert oc.reconcile(autonomy_home) == 1
        with use_cron_store(autonomy_home):
            names = [j.get("name") for j in list_jobs(include_disabled=True)]
        assert f"owner-continuation:{work['id']}:2" in names
        assert store.get_work(work["id"], autonomy_home)["refs"]["resume_job"]
    finally:
        db.close()


def _run_cli_write_through_a_shell(work_id, home):
    """Exactly how production reaches `note_cli_write`: shell -> CLI process."""
    import subprocess, sys, textwrap

    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(_REPO_ROOT)!r})
        from agent.autonomy.owner_continuity import note_cli_write
        note_cli_write({work_id!r}, hermes_home={str(home)!r})
    """)
    subprocess.run(["/bin/sh", "-c", f"{sys.executable} -c {shlex.quote(script)}"],
                   check=True, capture_output=True, timeout=60)


def _tool_agent(work_id):
    return SimpleNamespace(
        _owner_continuity_work_id=work_id,
        _owner_continuity_acted=False,
        _owner_continuity_source=None,
    )


def test_a_dead_job_re_arms_an_event_that_had_already_fired(autonomy_home):
    """The generation fences the job AND stamps the event that arrived under it.

    Bumping one without the other stranded a signalled event: ``resume_due``
    stopped matching it, no replacement job was ever minted, and ``signal_event``
    refuses to re-signal, so nothing could repair it before the deadline.
    """
    from cron.jobs import list_jobs, update_job, use_cron_store

    db, work = due_work(autonomy_home)
    try:
        deadline = "2030-01-01T00:00:00Z"
        oc.wait(work["id"], event="cron:job-7", deadline=deadline, hermes_home=autonomy_home)
        oc.signal_event(work["id"], "cron:job-7", hermes_home=autonomy_home)
        signalled = store.get_work(work["id"], autonomy_home)
        assert signalled["refs"]["resume_event"]["generation"] == signalled["refs"]["resume_generation"]

        assert oc.reconcile(autonomy_home) == 1
        with use_cron_store(autonomy_home):
            job = next(j for j in list_jobs(include_disabled=True)
                       if str(j.get("name", "")).startswith("owner-continuation:"))
            update_job(job["id"], {"enabled": False, "state": "error"})

        before = int(signalled["refs"]["resume_generation"])
        oc.reconcile(autonomy_home)
        after = store.get_work(work["id"], autonomy_home)
        assert after["refs"]["resume_generation"] == before + 1
        # The event travels with it, or the retry can never become due again.
        assert after["refs"]["resume_event"]["generation"] == before + 1
        assert oc.resume_due(after)
        assert oc.reconcile(autonomy_home) == 1
    finally:
        db.close()


def test_a_dead_job_does_not_charge_a_native_turn_that_holds_the_source(autonomy_home):
    """A second probe only narrows the window; the lease is what closes it.

    A native turn can take the source lease between the probe and the write, and
    it changes nothing the re-arm's CAS watches -- so the re-arm has to hold the
    same lease that turn would need.
    """
    import os, uuid
    from cron.jobs import list_jobs, update_job, use_cron_store

    db, work = due_work(autonomy_home)
    try:
        assert oc.reconcile(autonomy_home) == 1
        with use_cron_store(autonomy_home):
            job = next(j for j in list_jobs(include_disabled=True)
                       if str(j.get("name", "")).startswith("owner-continuation:"))
            update_job(job["id"], {"enabled": False, "state": "error"})

        holder = f"native-turn:{os.getpid()}:{uuid.uuid4().hex}"
        assert db.try_acquire_session_turn_lease(
            "owner-source", holder, ttl_seconds=30, patience_s=0
        )
        try:
            # Idle to the loop's own probe, busy by the time the re-arm writes.
            real = oc._source_has_active_turn
            oc._source_has_active_turn = lambda *a, **k: False
            try:
                oc.reconcile(autonomy_home)
            finally:
                oc._source_has_active_turn = real
        finally:
            db.release_session_turn_lease("owner-source", holder)

        after = store.get_work(work["id"], autonomy_home)
        assert after["refs"]["resume_generation"] == 1
        assert int(after["refs"].get("resume_attempts") or 0) == 0
    finally:
        db.close()


def test_automatic_recovery_from_an_unfinished_turn_still_terminates(autonomy_home):
    """Admission resets the TRANSPORT budget, so recovery needs one of its own.

    Settle -> wait two minutes -> resume -> admit -> end unfinished -> settle is
    a closed loop, and every admission put ``resume_attempts`` back to zero. The
    Owner would never be asked, and the row would never rest.
    """
    db, work = due_work(autonomy_home)
    try:
        # A literal bound, so a build without the ceiling fails on the LOOP
        # rather than on a missing constant.
        for _ in range(8):
            current = store.get_work(work["id"], autonomy_home)
            if current["refs"].get("pending_owner_result"):
                break
            # Every admitted cycle puts the transport budget back to zero.
            store.update_work(
                work["id"], state="working",
                refs={"resume_attempts": 0, "dispatch": None},
                hermes_home=autonomy_home,
            )
            oc._settle_unfinished(
                store.get_work(work["id"], autonomy_home), autonomy_home
            )
        else:
            raise AssertionError("automatic unfinished-turn recovery never terminated")

        pending = store.get_work(work["id"], autonomy_home)
        assert int(pending["refs"]["unfinished_settlements"]) == oc.UNFINISHED_SETTLEMENTS
        assert oc.deliver_pending(pending, autonomy_home)
        assert store.get_work(work["id"], autonomy_home)["state"] == "needs_owner"
    finally:
        db.close()


def _projection_tool():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "project_notices",
        Path(__file__).resolve().parents[2] / "tools" / "project_unstarted_continuation_notices.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_notice_that_never_ran_stops_reading_as_the_agents_own_answer(autonomy_home):
    """It stays in the transcript; it stops speaking for the agent.

    The line is the last thing an Owner sees on a chat whose real last answer
    was "no action required", and it is indistinguishable from the agent's own
    words -- in the timeline and in every sidebar preview. Typing it through the
    projection that already exists for runtime notices is the whole repair: the
    row keeps its content, its timestamp and its place.
    """
    from agent.message_projection import owner_preview, owner_visible, projection

    tool = _projection_tool()
    db, work = due_work(autonomy_home)
    try:
        for generation in (1, 2, 3):
            oc.run_resume(work["id"], generation, hermes_home=autonomy_home,
                          runner=failing_runner)
        assert oc.deliver_pending(store.get_work(work["id"], autonomy_home), autonomy_home)
        before = owner_rows(db)
        assert len(before) == 1
        # The projection follows the repair tool's marker, which is what proves
        # a row was this defect's. Set it the way that tool would.
        store.update_work(work["id"], hermes_home=autonomy_home,
                          refs={oc.UNSTARTED_CONTINUATION: {"attempts": 1}})

        assert tool.main(["--root", str(autonomy_home), "--apply"]) == 0

        rows = db.get_messages("owner-source", include_inactive=True)
        notice = [r for r in rows if r.get("role") == "assistant"][-1]
        # Nothing was deleted or rewritten.
        assert notice["content"] == before[0]["content"]
        value = projection(notice.get("display_metadata"))
        assert value["origin"] == "runtime" and value["purpose"] == "progress"
        assert value["work_id"] == work["id"]
        assert not owner_visible("assistant", notice["content"], notice.get("display_kind"),
                                 notice.get("display_metadata"))
        assert not owner_preview("assistant", notice["content"], notice.get("display_kind"),
                                 notice.get("display_metadata"))
        assert owner_rows(db) == []

        # Idempotent: a second pass finds nothing left to type.
        assert tool.main(["--root", str(autonomy_home), "--apply"]) == 0
    finally:
        db.close()


def test_projection_only_touches_what_the_delivery_receipt_proves(autonomy_home):
    """The receipt is the authority -- not the sentence, and not the row id."""
    from agent.message_projection import owner_visible

    tool = _projection_tool()
    db, work = due_work(autonomy_home)
    try:
        for generation in (1, 2, 3):
            oc.run_resume(work["id"], generation, hermes_home=autonomy_home,
                          runner=failing_runner)
        assert oc.deliver_pending(store.get_work(work["id"], autonomy_home), autonomy_home)
        # An agent line with the SAME words, delivered by nothing.
        store.update_work(work["id"], hermes_home=autonomy_home,
                          refs={oc.UNSTARTED_CONTINUATION: {"attempts": 1}})
        mid = db.append_message(
            "owner-source", "assistant",
            "The continuation was interrupted before a verified outcome. Existing "
            "effects need inspection before retry; no action was replayed.",
        )
        assert tool.main(["--root", str(autonomy_home), "--apply"]) == 0

        impostor = [r for r in db.get_messages("owner-source", include_inactive=True)
                    if (r.get("_row_id") or r.get("id")) == mid][0]
        assert owner_visible("assistant", impostor["content"], impostor.get("display_kind"),
                             impostor.get("display_metadata"))
    finally:
        db.close()


def test_a_legacy_row_still_gets_its_transport_retries(autonomy_home):
    """Provenance decides what may be CLAIMED, never whether to retry.

    A dispatch that never started ran no step whatever the row did earlier, and
    the continuation prompt forbids replaying an uncertain action. Gating the
    retry on the same predicate as the badge gave every row opened before the
    recorder existed zero retries and an immediate escalation.
    """
    from cron.jobs import list_jobs, update_job, use_cron_store

    db, work = due_work(autonomy_home)
    try:
        store.update_work(work["id"], refs={"effects_tracked": None},
                          hermes_home=autonomy_home)
        assert oc.reconcile(autonomy_home) == 1
        with use_cron_store(autonomy_home):
            job = next(j for j in list_jobs(include_disabled=True)
                       if str(j.get("name", "")).startswith("owner-continuation:"))
            update_job(job["id"], {"enabled": False, "state": "error"})

        oc.reconcile(autonomy_home)
        after = store.get_work(work["id"], autonomy_home)
        assert after["refs"]["resume_generation"] == 2
        assert not after["refs"].get("pending_owner_result")
    finally:
        db.close()


def test_apply_does_not_overwrite_an_adjudication_made_after_it_looked(autonomy_home):
    repair = _repair_tool("repair_apply_race")
    db, work = _stranded_row(autonomy_home, repair.UNSTARTED_OUTCOMES[0], legacy=True)
    try:
        selected, _ = repair.stranded(autonomy_home, {work["id"]})
        row = selected[0]
        # Another run records a human decision in between.
        store.update_work(
            work["id"],
            refs={repair.MARKER: {"attempts": 1, "at": time.time(),
                                  "repaired_by": repair.REPAIRED_BY,
                                  "adjudicated": True, "reason": "read the transcript"}},
            hermes_home=autonomy_home,
        )
        assert store.update_work(
            work["id"],
            refs={repair.MARKER: {"attempts": row["resume_attempts"], "at": time.time(),
                                  "repaired_by": repair.REPAIRED_BY}},
            hermes_home=autonomy_home,
            expected_state="needs_owner",
            expected_refs=row["expected_refs"],
        ) is None
        kept = store.get_work(work["id"], autonomy_home)["refs"][repair.MARKER]
        assert kept["adjudicated"] is True and kept["reason"] == "read the transcript"
    finally:
        db.close()


def test_an_unfinished_task_that_never_dispatched_is_outside_adjudication(autonomy_home):
    """The sentence is shared; the defect is a CONTINUATION that failed."""
    repair = _repair_tool("repair_no_dispatch")
    db, work = _stranded_row(autonomy_home, repair.UNSTARTED_OUTCOMES[1])
    try:
        store.update_work(work["id"], refs={"dispatch": None}, hermes_home=autonomy_home)
        selected, _held = repair.stranded(autonomy_home, {work["id"]})
        assert selected == []
        assert repair.main(["--root", str(autonomy_home), "--apply",
                            "--adjudicated", work["id"],
                            "--reason", "believed stale"]) == 0
        assert not store.get_work(work["id"], autonomy_home)["refs"].get(repair.MARKER)
    finally:
        db.close()


def test_a_receipt_without_a_content_hash_proves_nothing(autonomy_home):
    """The receipt is the authority; without its hash there is no authority."""
    from agent.message_projection import owner_visible

    tool = _projection_tool()
    db, work = due_work(autonomy_home)
    try:
        for generation in (1, 2, 3):
            oc.run_resume(work["id"], generation, hermes_home=autonomy_home,
                          runner=failing_runner)
        assert oc.deliver_pending(store.get_work(work["id"], autonomy_home), autonomy_home)
        delivery = dict(store.get_work(work["id"], autonomy_home)["refs"]["owner_delivery"])
        delivery.pop("content_sha256", None)
        store.update_work(work["id"], hermes_home=autonomy_home,
                          refs={"owner_delivery": delivery,
                                oc.UNSTARTED_CONTINUATION: {"attempts": 1}})

        assert tool.main(["--root", str(autonomy_home), "--apply"]) == 0
        rows = db.get_messages("owner-source", include_inactive=True)
        notice = [r for r in rows if r.get("role") == "assistant"][-1]
        assert owner_visible("assistant", notice["content"], notice.get("display_kind"),
                             notice.get("display_metadata"))
    finally:
        db.close()


def test_projection_follows_the_notice_when_compaction_moves_it(autonomy_home):
    """Compaction re-inserts the carried row under a NEW id.

    Addressing by (session, id) typed the superseded copy and left the live one
    speaking for the agent, while a second pass reported success.
    """
    from agent.message_projection import owner_visible, projection

    tool = _projection_tool()
    db, work = due_work(autonomy_home)
    try:
        for generation in (1, 2, 3):
            oc.run_resume(work["id"], generation, hermes_home=autonomy_home,
                          runner=failing_runner)
        assert oc.deliver_pending(store.get_work(work["id"], autonomy_home), autonomy_home)
        store.update_work(work["id"], hermes_home=autonomy_home,
                          refs={oc.UNSTARTED_CONTINUATION: {"attempts": 1}})
        rows = db.get_messages("owner-source", include_inactive=True)
        original = [r for r in rows if r.get("role") == "assistant"][-1]

        # Exactly what compaction leaves behind: the same row, new physical id,
        # carrying its own envelope.
        import json as _json
        moved = db.append_message(
            "owner-source", "assistant", original["content"],
            display_metadata=_json.dumps(original["display_metadata"])
            if isinstance(original["display_metadata"], (dict, list))
            else original["display_metadata"],
        )
        assert tool.main(["--root", str(autonomy_home), "--apply"]) == 0

        after = db.get_messages("owner-source", include_inactive=True)
        carried = [r for r in after if (r.get("_row_id") or r.get("id")) == moved][0]
        value = projection(carried.get("display_metadata"))
        assert value["origin"] == "runtime" and value["purpose"] == "progress"
        assert not owner_visible("assistant", carried["content"], carried.get("display_kind"),
                                 carried.get("display_metadata"))
    finally:
        db.close()




def test_a_spent_retry_budget_is_an_obligation_the_repair_tool_will_not_take(autonomy_home):
    """The provenance boundary, and it is provable rather than inferred.

    The pre-fix build escalated on the FIRST failed dispatch. This one re-arms
    and can only escalate once the budget is spent -- so an escalation carrying
    `resume_attempts <= 1` is one the defect authored, and a higher count is
    Hermes reporting that it gave up. No effects oracle, and the sentence is
    never the authority.
    """
    from tui_gateway.activity import work_state

    repair = _repair_tool("repair_spent_budget")
    db, work = _stranded_row(autonomy_home, repair.UNSTARTED_OUTCOMES[0])
    try:
        assert store.get_work(work["id"], autonomy_home)["refs"]["resume_attempts"] == 3
        selected, held = repair.stranded(autonomy_home)
        assert selected == []
        assert [row["id"] for row in held] == [work["id"]]
        assert "gave up" in held[0]["reason"]

        assert repair.main(["--root", str(autonomy_home), "--apply"]) == 0
        assert not store.get_work(work["id"], autonomy_home)["refs"].get(repair.MARKER)
        assert work_state(autonomy_home) == "needs_owner"
    finally:
        db.close()


def test_projection_will_not_cross_into_another_conversation(autonomy_home):
    """Matching content and a copied work id are not a licence to reach.

    Selection follows the delivery receipt's own conversation lineage. An
    external review put the same body and the same work id in an unrelated chat
    and watched the previous version hide both it and its compacted copy.
    """
    from agent.message_projection import owner_visible

    tool = _projection_tool()
    db, work = due_work(autonomy_home)
    try:
        for generation in (1, 2, 3):
            oc.run_resume(work["id"], generation, hermes_home=autonomy_home,
                          runner=failing_runner)
        assert oc.deliver_pending(store.get_work(work["id"], autonomy_home), autonomy_home)
        store.update_work(work["id"], hermes_home=autonomy_home,
                          refs={oc.UNSTARTED_CONTINUATION: {"attempts": 1}})
        rows = db.get_messages("owner-source", include_inactive=True)
        notice = [r for r in rows if r.get("role") == "assistant"][-1]

        import json as _json
        db.create_session("someone-else", source="desktop")
        stranger = db.append_message(
            "someone-else", "assistant", notice["content"],
            display_metadata=_json.dumps(notice["display_metadata"])
            if isinstance(notice["display_metadata"], (dict, list))
            else notice["display_metadata"],
        )
        assert tool.main(["--root", str(autonomy_home), "--apply"]) == 0

        other = [r for r in db.get_messages("someone-else", include_inactive=True)
                 if (r.get("_row_id") or r.get("id")) == stranger][0]
        assert owner_visible("assistant", other["content"], other.get("display_kind"),
                             other.get("display_metadata"))
    finally:
        db.close()


def test_a_legacy_notice_with_no_envelope_is_still_repaired(autonomy_home):
    """The receipt's own address still identifies one, and the hash proves it."""
    from agent.message_projection import owner_visible

    tool = _projection_tool()
    db, work = due_work(autonomy_home)
    try:
        for generation in (1, 2, 3):
            oc.run_resume(work["id"], generation, hermes_home=autonomy_home,
                          runner=failing_runner)
        assert oc.deliver_pending(store.get_work(work["id"], autonomy_home), autonomy_home)
        store.update_work(work["id"], hermes_home=autonomy_home,
                          refs={oc.UNSTARTED_CONTINUATION: {"attempts": 1}})

        # Exactly what a pre-envelope build left: the delivered text, no envelope.
        delivery = store.get_work(work["id"], autonomy_home)["refs"]["owner_delivery"]
        import sqlite3 as _sqlite3
        with _sqlite3.connect(str(autonomy_home / "state.db")) as conn:
            conn.execute(
                "UPDATE messages SET display_metadata=NULL WHERE session_id=? AND id=?",
                ("owner-source", int(delivery["message_id"])),
            )

        assert tool.main(["--root", str(autonomy_home), "--apply"]) == 0
        row = [r for r in db.get_messages("owner-source", include_inactive=True)
               if (r.get("_row_id") or r.get("id")) == int(delivery["message_id"])][0]
        assert not owner_visible("assistant", row["content"], row.get("display_kind"),
                                 row.get("display_metadata"))
    finally:
        db.close()


def test_nothing_is_repaired_without_a_human_naming_it(autonomy_home):
    """A low attempt count is a guard, not provenance.

    Orphan recovery reaches the Owner at attempt one too, wearing one of these
    same sentences, and every counter here is a mutable ledger field rather than
    authenticated build identity. An external review used the supported CLI to
    produce exactly that row and watched the previous predicate take it.
    """
    from tui_gateway.activity import work_state

    repair = _repair_tool("repair_named_only")
    db, work = _stranded_row(autonomy_home, repair.UNSTARTED_OUTCOMES[0], legacy=True)
    try:
        selected, held = repair.stranded(autonomy_home)
        assert selected == []
        assert [row["id"] for row in held] == [work["id"]]
        assert "--adjudicated" in held[0]["reason"]

        assert repair.main(["--root", str(autonomy_home), "--apply"]) == 0
        assert not store.get_work(work["id"], autonomy_home)["refs"].get(repair.MARKER)
        assert work_state(autonomy_home) == "needs_owner"
    finally:
        db.close()


def test_a_row_whose_continuation_was_ever_admitted_is_held(autonomy_home):
    """Admission is sticky and needs no attribution, so it is a hard guard.

    A human can still name such a row -- a finished effect and an abandoned one
    are identical in the ledger, and only the transcript separates them -- but
    nothing reaches it without one.
    """
    repair = _repair_tool("repair_admitted")
    db, work = _stranded_row(autonomy_home, repair.UNSTARTED_OUTCOMES[0], legacy=True)
    try:
        store.update_work(work["id"], refs={"ever_admitted": True},
                          hermes_home=autonomy_home)
        selected, held = repair.stranded(autonomy_home)
        assert selected == []
        assert [row["id"] for row in held] == [work["id"]]
        assert "records this task acting" in held[0]["reason"]
        assert repair.main(["--root", str(autonomy_home), "--apply"]) == 0
        assert not store.get_work(work["id"], autonomy_home)["refs"].get(repair.MARKER)
    finally:
        db.close()


def test_a_later_orphan_still_says_an_earlier_cycle_ran(autonomy_home):
    """The wording distinction has to survive the cycle that lost the dispatch.

    Both recovery paths passed `ran_before=False` unconditionally, so a row whose
    EARLIER cycle was admitted got the reassuring sentence instead of the one
    that tells the Owner existing effects still need checking.
    """
    db, work = due_work(autonomy_home)
    try:
        store.update_work(
            work["id"], state="working",
            refs={"ever_admitted": True, "resume_generation": 9,
                  "resume_attempts": 3,
                  "dispatch": {"generation": 9, "nonce": "n", "pid": 999999999,
                               "started_at": 1, "process_started_at": 1},
                  "heartbeat_at": time.time() - 1000},
            hermes_home=autonomy_home,
        )
        current = store.get_work(work["id"], autonomy_home)
        assert oc.dispatch_never_started(current) and oc.ever_admitted(current)

        oc._recover_orphan(current, autonomy_home)
        pending = store.get_work(work["id"], autonomy_home)["refs"]["pending_owner_result"]
        assert "Earlier steps of this task did run" in pending["text"]
        assert "this attempt ran no step" in pending["text"]
    finally:
        db.close()


def test_a_marker_from_the_removed_mechanism_is_revoked_on_sight(autonomy_home):
    """Its evidence is exactly what that mechanism failed to record.

    Asking for a recorded effect before revoking one of its markers preserves
    the conclusion it got wrong: the effects it missed are the ones the ledger
    never recorded. No build writes a runtime marker any more, so one that is
    not this tool's comes off, and the row is visible until a human says
    otherwise.
    """
    from tui_gateway.activity import work_state

    repair = _repair_tool("repair_removed_mechanism")
    db, work = _stranded_row(autonomy_home, repair.UNSTARTED_OUTCOMES[0], legacy=True)
    try:
        # No process, no verification, no children, no admission -- exactly the
        # row the deleted attribution left behind.
        store.update_work(work["id"], hermes_home=autonomy_home,
                          refs={repair.MARKER: {
                              "attempts": 1, "at": time.time(),
                              "delivery": store.get_work(work["id"], autonomy_home)[
                                  "refs"]["owner_delivery"]["message_id"]}})
        assert work_state(autonomy_home) is None

        assert [row for row, _m, _w in repair.wrongly_marked(autonomy_home)] == [work["id"]]
        assert repair.main(["--root", str(autonomy_home), "--apply"]) == 0
        assert not store.get_work(work["id"], autonomy_home)["refs"].get(repair.MARKER)
        assert work_state(autonomy_home) == "needs_owner"
    finally:
        db.close()


def test_losing_the_lease_mid_write_does_not_strand_a_signalled_wake(autonomy_home):
    """Compensation has to undo the event with the generation it migrated.

    Restoring the generation alone left an already-signalled event stamped for a
    generation that no longer exists: `resume_due` stopped matching it,
    `signal_event` refuses to re-signal, and the task waited out its deadline
    looking merely patient.
    """
    import os, uuid
    from cron.jobs import list_jobs, update_job, use_cron_store

    db, work = due_work(autonomy_home)
    try:
        oc.wait(work["id"], event="cron:job-9", deadline="2030-01-01T00:00:00Z",
                hermes_home=autonomy_home)
        oc.signal_event(work["id"], "cron:job-9", hermes_home=autonomy_home)
        before = store.get_work(work["id"], autonomy_home)
        assert oc.resume_due(before)

        assert oc.reconcile(autonomy_home) == 1
        with use_cron_store(autonomy_home):
            job = next(j for j in list_jobs(include_disabled=True)
                       if str(j.get("name", "")).startswith("owner-continuation:"))
            update_job(job["id"], {"enabled": False, "state": "error"})

        # The lease is lost across the write: held at the check, gone after it.
        real = oc._still_holds_lease
        calls = []

        def lost_after_the_write(db_, sid, holder):
            calls.append(sid)
            return len(calls) == 1

        oc._still_holds_lease = lost_after_the_write
        try:
            oc.reconcile(autonomy_home)
        finally:
            oc._still_holds_lease = real

        after = store.get_work(work["id"], autonomy_home)
        assert after["refs"]["resume_generation"] == before["refs"]["resume_generation"]
        assert int(after["refs"].get("resume_attempts") or 0) == 0
        # And the wake still matches, which is the whole point.
        assert after["refs"]["resume_event"] == before["refs"]["resume_event"]
        assert oc.resume_due(after)
    finally:
        db.close()


def test_a_repeated_sentence_is_not_an_identity(autonomy_home):
    """Two messages can carry the same words. Only the receipt names one.

    Reaching a compaction-carried notice by "the only row in this lineage with
    this text" hid a second, unrelated assistant message that merely repeated
    the sentence. A visible duplicate is a smaller harm than hiding Owner-facing
    speech, so the text match is not admitted at all.
    """
    from agent.message_projection import owner_visible

    tool = _projection_tool()
    db, work = due_work(autonomy_home)
    try:
        for generation in (1, 2, 3):
            oc.run_resume(work["id"], generation, hermes_home=autonomy_home,
                          runner=failing_runner)
        assert oc.deliver_pending(store.get_work(work["id"], autonomy_home), autonomy_home)
        store.update_work(work["id"], hermes_home=autonomy_home,
                          refs={oc.UNSTARTED_CONTINUATION: {"attempts": 1}})
        rows = db.get_messages("owner-source", include_inactive=True)
        notice = [r for r in rows if r.get("role") == "assistant"][-1]

        echo = db.append_message("owner-source", "assistant", notice["content"])
        assert tool.main(["--root", str(autonomy_home), "--apply"]) == 0

        other = [r for r in db.get_messages("owner-source", include_inactive=True)
                 if (r.get("_row_id") or r.get("id")) == echo][0]
        assert owner_visible("assistant", other["content"], other.get("display_kind"),
                             other.get("display_metadata"))
    finally:
        db.close()


def test_a_restored_badge_gets_its_explanation_back(autonomy_home):
    """A badge without the sentence that explains it is half a repair."""
    from agent.message_projection import owner_visible

    tool = _projection_tool()
    db, work = due_work(autonomy_home)
    try:
        for generation in (1, 2, 3):
            oc.run_resume(work["id"], generation, hermes_home=autonomy_home,
                          runner=failing_runner)
        assert oc.deliver_pending(store.get_work(work["id"], autonomy_home), autonomy_home)
        store.update_work(work["id"], hermes_home=autonomy_home,
                          refs={oc.UNSTARTED_CONTINUATION: {"attempts": 1}})
        assert tool.main(["--root", str(autonomy_home), "--apply"]) == 0
        assert owner_rows(db) == []

        # The marker is revoked; the notice must come back with it.
        store.update_work(work["id"], hermes_home=autonomy_home,
                          refs={oc.UNSTARTED_CONTINUATION: None})
        assert tool.main(["--root", str(autonomy_home), "--apply"]) == 0
        restored = owner_rows(db)
        assert len(restored) == 1
        assert owner_visible("assistant", restored[0]["content"],
                             restored[0].get("display_kind"),
                             restored[0].get("display_metadata"))
    finally:
        db.close()


def test_a_malformed_marker_suppresses_nothing_and_is_still_revocable(autonomy_home):
    """A marker that names no delivery cannot be shown to be about any outcome.

    Suppression is now bound to the delivery a decision was made about, so a
    hand-edited or imported marker hides nothing at all -- the safe side of that
    doubt -- and cleanup can still remove it so the row stops carrying a claim
    no build makes.
    """
    from tui_gateway.activity import work_state

    repair = _repair_tool("repair_malformed")
    db, work = _stranded_row(autonomy_home, repair.UNSTARTED_OUTCOMES[0], legacy=True)
    try:
        store.update_work(work["id"], hermes_home=autonomy_home,
                          refs={repair.MARKER: "legacy-marker"})
        assert work_state(autonomy_home) == "needs_owner"
        assert [r for r, _m, _w in repair.wrongly_marked(autonomy_home)] == [work["id"]]
        assert repair.main(["--root", str(autonomy_home), "--apply"]) == 0
        assert not store.get_work(work["id"], autonomy_home)["refs"].get(repair.MARKER)
        assert work_state(autonomy_home) == "needs_owner"
    finally:
        db.close()

def test_a_falsy_marker_hides_a_badge_just_as_well(autonomy_home):
    """`work_state` suppresses on presence, so presence is what cleanup tests."""
    from tui_gateway.activity import work_state

    repair = _repair_tool("repair_falsy")
    db, work = _stranded_row(autonomy_home, repair.UNSTARTED_OUTCOMES[0], legacy=True)
    try:
        for value in (False, 0, "", [], "legacy-marker"):
            store.update_work(work["id"], hermes_home=autonomy_home,
                              refs={repair.MARKER: value})
            # Whether or not this shape currently suppresses, it is a marker no
            # build writes and cleanup must be able to remove it.
            assert [r for r, _m, _w in repair.wrongly_marked(autonomy_home)] \
                == [work["id"]], f"{value!r} must be revocable"
            assert repair.main(["--root", str(autonomy_home), "--apply"]) == 0
            assert work_state(autonomy_home) == "needs_owner"
    finally:
        db.close()


def test_the_inverse_pass_only_touches_the_row_its_receipt_names(autonomy_home):
    """Its first version checked the work id and nothing else.

    An unrelated internal runtime message carrying the same work id, in another
    conversation, became Owner-facing agent speech.
    """
    from agent.message_projection import native_metadata, owner_visible

    tool = _projection_tool()
    db, work = due_work(autonomy_home)
    try:
        for generation in (1, 2, 3):
            oc.run_resume(work["id"], generation, hermes_home=autonomy_home,
                          runner=failing_runner)
        assert oc.deliver_pending(store.get_work(work["id"], autonomy_home), autonomy_home)
        store.update_work(work["id"], hermes_home=autonomy_home,
                          refs={oc.UNSTARTED_CONTINUATION: {"attempts": 1}})
        assert tool.main(["--root", str(autonomy_home), "--apply"]) == 0

        import json as _json
        db.create_session("elsewhere", source="desktop")
        stranger = db.append_message(
            "elsewhere", "assistant", "an unrelated internal note",
            display_metadata=_json.dumps(native_metadata(
                "runtime", "owner", "progress", work_id=work["id"])),
        )
        store.update_work(work["id"], hermes_home=autonomy_home,
                          refs={oc.UNSTARTED_CONTINUATION: None})
        assert tool.main(["--root", str(autonomy_home), "--apply"]) == 0

        other = [r for r in db.get_messages("elsewhere", include_inactive=True)
                 if (r.get("_row_id") or r.get("id")) == stranger][0]
        assert not owner_visible("assistant", other["content"],
                                 other.get("display_kind"),
                                 other.get("display_metadata"))
    finally:
        db.close()


def test_an_adjudication_does_not_cover_the_next_question(autonomy_home):
    """A decision is about ONE delivery, and it expires with it.

    Without that fence an old suppression kept applying to whatever the row said
    next: a task adjudicated as settled could post a genuinely new question
    through the ordinary CLI and stay dark. That is this incident's own failure
    mode, reintroduced by its repair.
    """
    from tui_gateway.activity import work_state

    repair = _repair_tool("repair_next_question")
    db, work = _stranded_row(autonomy_home, repair.UNSTARTED_OUTCOMES[0], legacy=True)
    try:
        assert repair.main(["--root", str(autonomy_home), "--apply",
                            "--adjudicated", work["id"],
                            "--reason", "transcript shows nothing pending"]) == 0
        assert work_state(autonomy_home) is None

        # The task comes back with something genuinely new to ask.
        store.update_work(
            work["id"], hermes_home=autonomy_home,
            completion_result="A new approval is required before deleting the backup.",
            refs={"owner_delivery": {"message_id": 4242,
                                     "source_session_id": "owner-source"}},
        )
        assert work_state(autonomy_home) == "needs_owner"
        assert [r for r, _m, _w in repair.wrongly_marked(autonomy_home)] == [work["id"]]
    finally:
        db.close()


def test_the_inverse_pass_follows_a_carried_envelope_too(autonomy_home):
    """Compaction gives the active copy a new id; the forward pass allows for
    that and the inverse has to as well, or the badge comes back without it."""
    from agent.message_projection import owner_visible

    tool = _projection_tool()
    db, work = due_work(autonomy_home)
    try:
        for generation in (1, 2, 3):
            oc.run_resume(work["id"], generation, hermes_home=autonomy_home,
                          runner=failing_runner)
        assert oc.deliver_pending(store.get_work(work["id"], autonomy_home), autonomy_home)
        store.update_work(work["id"], hermes_home=autonomy_home,
                          refs={oc.UNSTARTED_CONTINUATION: {"attempts": 1}})
        assert tool.main(["--root", str(autonomy_home), "--apply"]) == 0

        rows = db.get_messages("owner-source", include_inactive=True)
        typed = [r for r in rows if r.get("role") == "assistant"][-1]
        import json as _json
        carried = db.append_message(
            "owner-source", "assistant", typed["content"],
            display_metadata=_json.dumps(typed["display_metadata"])
            if isinstance(typed["display_metadata"], (dict, list))
            else typed["display_metadata"],
        )
        store.update_work(work["id"], hermes_home=autonomy_home,
                          refs={oc.UNSTARTED_CONTINUATION: None})
        assert tool.main(["--root", str(autonomy_home), "--apply"]) == 0

        row = [r for r in db.get_messages("owner-source", include_inactive=True)
               if (r.get("_row_id") or r.get("id")) == carried][0]
        assert owner_visible("assistant", row["content"], row.get("display_kind"),
                             row.get("display_metadata"))
    finally:
        db.close()
