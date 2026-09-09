#!/usr/bin/env python3
"""Retract Owner escalations raised by a continuation dispatch that never ran.

``run_resume`` used to answer any pre-admission dispatch failure with
``terminal="needs_owner"``. ``tui_gateway.activity.work_state`` reports
``needs_owner`` when ANY row holds it, and ``validate_transition`` only releases
that state against a NEW source-bound Owner decision, so each of those rows pins
"Needs you" on its agent indefinitely -- for an inspection the Owner cannot
perform, because ``dispatch["admitted"]`` proves no model turn ran and therefore
no effect exists.

The code fix stops new ones. This settles the rows already stranded.

What it does NOT do: it deletes and rewrites nothing, and it does not move the
row out of ``needs_owner``. The assistant line stays in the canonical transcript
exactly as written, ``completion_result`` and the full ``refs`` provenance are
kept, and the row stays rebindable -- a terminal state would clear the badge too,
but ``store.update_work`` freezes a terminal Owner row and ``_bind_owner_decision``
only scans ``needs_owner``, so the Owner replying "كمل" would bind nothing and the
responsibility would be lost.

All it adds is the ``continuation_unstarted`` marker that
``tui_gateway.activity.work_state`` now skips, through ``store.update_work`` so
the existing state machine validates the write rather than a hand-written UPDATE
bypassing it.

Selection is deliberately narrow. A row qualifies only when all of these hold:

  * state is ``needs_owner``
  * it is a source-bound Owner task (``refs["owner_request"]``)
  * a dispatch exists and was never admitted
  * its outcome is exactly the legacy interrupted sentence
  * that outcome was already durably delivered (``refs["owner_delivery"]``), so
    settling the row withholds nothing the Owner had not already been told
  * nothing is mid-flight (no ``pending_owner_result``, no ``owner_stop``)

Idempotent: a second run selects nothing. Dry run unless ``--apply``.

    python3 tools/repair_unadmitted_owner_escalations.py --root ~/.hermes
    python3 tools/repair_unadmitted_owner_escalations.py --root ~/.hermes --apply
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

from agent.autonomy.owner_continuity import UNADMITTED_RESUME_ATTEMPTS
from agent.autonomy.owner_continuity import UNSTARTED_CONTINUATION as MARKER

#: Stamped into every marker this tool writes. The runtime writes its own marker
#: for a continuation that provably never started; that one is not this tool's.
REPAIRED_BY = "repair_unadmitted_owner_escalations"

#: Every sentence the three pre-fix escalation sites could produce for a
#: dispatch that never reached ``bind_turn``. Matching the exact text keeps the
#: population bounded to escalations this defect actually authored.
UNSTARTED_OUTCOMES = (
    "The continuation was interrupted before a verified outcome. Existing effects "
    "need inspection before retry; no action was replayed.",
    "This task has not reached a verified outcome. I stopped automatic continuation; "
    "existing effects and any pending approval need review before another attempt.",
    "The worker stopped before a verified outcome. I retained this task and its prior "
    "evidence. Existing effects and pending approvals must be inspected before retrying.",
)


def profile_homes(root: Path):
    """The default profile plus every named agent, in a stable order."""
    homes = []
    if (root / "autonomy" / "work.db").is_file():
        homes.append(("default", root))
    profiles = root / "profiles"
    if profiles.is_dir():
        for child in sorted(profiles.iterdir()):
            if (child / "autonomy" / "work.db").is_file():
                homes.append((child.name, child))
    return homes


def _dispatch_is_a_recorded_unadmitted_attempt(dispatch, refs):
    """A dispatch that provably ran and provably never reached ``bind_turn``.

    An empty dict is not that. It satisfies "no ``admitted`` key" without
    recording that any dispatch ever happened, and an external review confirmed
    it walked straight through the old gate. Require the identity the dispatch
    writer actually stamps, and require it to belong to the CURRENT generation --
    a dispatch left over from an earlier cycle says nothing about this one.
    """
    if not isinstance(dispatch, dict) or dispatch.get("admitted"):
        return False
    if not (dispatch.get("nonce") and dispatch.get("pid") is not None):
        return False
    return dispatch.get("generation") == refs.get("resume_generation")


def _records_an_effect(refs):
    """An explicitly recorded effect. Absence proves nothing; presence is fact."""
    return any(refs.get(k) for k in ("ever_admitted", "native_process",
                                     "native_process_completion", "verification",
                                     "native_children"))


def _is_this_defects_escalation(result, refs):
    """A delivered, source-bound escalation authored by the continuation defect.

    The outer bound on what a human may adjudicate. It does not decide anything
    on its own -- the structural conditions in ``_selectable`` still apply to the
    automatic path -- but nothing outside this shape can be named at all.
    """
    source = refs.get("owner_request")
    delivery = refs.get("owner_delivery")
    return bool(
        (result or "").strip() in UNSTARTED_OUTCOMES
        # A continuation failure means a continuation was dispatched. Without a
        # recorded dispatch this is an ordinary unfinished Owner task wearing
        # the same sentence, and naming it would reach past this defect.
        and isinstance(refs.get("dispatch"), dict)
        and isinstance(source, dict)
        and isinstance(delivery, dict)
        and delivery.get("message_id")
        and delivery.get("source_session_id", delivery.get("session_id"))
        == source.get("session_id")
        and not refs.get("pending_owner_result")
        and not refs.get("owner_stop")
    )


def _selectable(result, refs):
    """(ok, reason). Never True. Every condition here is a GUARD.

    This once claimed that ``resume_attempts <= 1`` proved a row came from the
    pre-fix build. It does not: orphan recovery escalates at attempt one too,
    wearing one of these same sentences, and every counter here is a mutable
    ledger field rather than authenticated build identity. A HIGH count does
    establish the opposite direction -- Hermes reporting that it gave up, which
    is a real Owner obligation and keeps its badge -- so it stays as a guard.

    Nothing is selected automatically. The reasons returned here are what a
    human reads before naming a row with ``--adjudicated``.
    """
    dispatch = refs.get("dispatch")
    delivery = refs.get("owner_delivery")
    source = refs.get("owner_request")
    if (result or "").strip() not in UNSTARTED_OUTCOMES:
        return False, "outcome is not a pre-fix continuation-transport escalation"
    if not isinstance(source, dict):
        return False, "not a source-bound Owner task"
    if not _dispatch_is_a_recorded_unadmitted_attempt(dispatch, refs):
        return False, "no recorded unadmitted dispatch for the current generation"
    if not (isinstance(delivery, dict) and delivery.get("message_id")
            and delivery.get("source_session_id", delivery.get("session_id"))
            == source.get("session_id")):
        return False, "the outcome was never durably delivered to this source"
    if refs.get("pending_owner_result") or refs.get("owner_stop"):
        return False, "something is mid-flight on this row"
    if refs.get("resume_attempts") is None:
        return False, "no attempt count was ever recorded"
    if int(refs["resume_attempts"]) > 1:
        return False, ("the retry budget was spent before this escalation, so Hermes"
                       f" gave up rather than never starting (>{1} of"
                       f" {UNADMITTED_RESUME_ATTEMPTS} attempts)")
    if _records_an_effect(refs):
        # A guard on the AUTOMATIC path. `--adjudicated` deliberately outranks
        # it, because a human reading the transcript can see what this cannot.
        return False, "the ledger records this task acting"
    # Everything above is a GUARD, not a verdict. A low attempt count was read as
    # provenance for one build and it is not: orphan recovery reaches the Owner
    # at attempt one too, wearing one of these same sentences, and every counter
    # here is a mutable field rather than authenticated build identity. So this
    # tool no longer decides anything by itself. It presents what it can prove
    # about the shape, and waits for a human to name the row.
    return False, ("this row has the shape, but only the canonical transcript can"
                   " say whether anything is owed; name it with --adjudicated")


def stranded(home: Path, adjudicated=()):
    """Read-only selection; never opens the store for write to look."""
    path = home / "autonomy" / "work.db"
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        rows = conn.execute(
            "SELECT id, completion_result, refs_json, updated_at FROM work WHERE state='needs_owner'"
        ).fetchall()
    selected, deferred = [], []
    for work_id, result, refs_json, updated_at in rows:
        try:
            refs = json.loads(refs_json or "{}")
        except ValueError:
            continue
        if refs.get(MARKER):
            continue  # idempotency: already repaired
        ok, reason = _selectable(result, refs)
        delivery = refs.get("owner_delivery") or {}
        adjudicable = _is_this_defects_escalation(result, refs)
        if not ok and work_id in adjudicated and adjudicable:
            # A human read the canonical transcript for this exact row and
            # recorded what it shows. That is evidence this tool cannot derive
            # -- a completed effect reported in the chat looks, in the ledger,
            # exactly like an unfinished one -- so it outranks the automatic
            # gate. It is accepted only per-row, only by name, only inside this
            # defect's delivered-escalation population, and only with a stated
            # reason, which is written into the marker.
            ok, reason, human = True, "", True
        else:
            human = work_id in adjudicated and ok
        if ok:
            selected.append({
                "id": work_id,
                "updated_at": updated_at,
                "resume_attempts": refs.get("resume_attempts"),
                "delivered_message_id": delivery.get("message_id"),
                "source_session_id": (refs.get("owner_request") or {}).get("session_id"),
                "adjudicated": human,
                # Finding 6: the badge may only be taken off the evidence that
                # was actually inspected. An Owner reply that rebinds this row
                # and a task that then delivers a REAL question both return it
                # to `needs_owner`, so state alone is not a safe precondition.
                "expected_refs": {
                    "owner_delivery": refs.get("owner_delivery"),
                    "owner_decision": refs.get("owner_decision"),
                    "dispatch": refs.get("dispatch"),
                    "resume_generation": refs.get("resume_generation"),
                    "resume_attempts": refs.get("resume_attempts"),
                    "pending_owner_result": None,
                    # Including the marker as it was READ (absent). A run that
                    # applied a human adjudication in between must not have it
                    # replaced by a marker chosen before that decision existed.
                    MARKER: refs.get(MARKER),
                    # The badge may only come off the EVIDENCE that was read.
                    # A command recorded between selection and apply changes the
                    # answer, and the row must be left alone when it does.
                    "ever_acted": refs.get("ever_acted"),
                    "ever_admitted": refs.get("ever_admitted"),
                    "effects_tracked": refs.get("effects_tracked"),
                    "verification": refs.get("verification"),
                    "native_process": refs.get("native_process"),
                    "native_process_completion": refs.get("native_process_completion"),
                    "native_children": refs.get("native_children"),
                },
            })
        elif ((result or "").strip() in UNSTARTED_OUTCOMES
              and isinstance(refs.get("dispatch"), dict)
              and not (refs.get("dispatch") or {}).get("admitted")):
            deferred.append({
                "id": work_id,
                "resume_generation": refs.get("resume_generation"),
                "resume_attempts": refs.get("resume_attempts"),
                "ever_admitted": bool(refs.get("ever_admitted")),
                "reason": reason,
            })
    return selected, deferred


def wrongly_marked(home: Path):
    """Markers THIS TOOL wrote that its current predicate would no longer write.

    The complement is the contract: if this tool would not mark a row today, its
    marker comes off today -- an earlier, weaker predicate is not allowed to keep
    a badge hidden.

    NO BUILD writes a marker from the runtime any more. One that is not this
    tool's therefore came from the mechanism deleted for being unsound, and it
    is revoked on sight: demanding evidence first would preserve exactly the
    conclusion that mechanism got wrong, because the effects it missed are the
    ones the ledger never recorded. A malformed marker -- hand-edited, imported,
    falsy -- suppresses the badge just as well, and goes the same way.

    A marker recording a human adjudication is kept. The evidence behind it was
    never in the ledger, so nothing here can re-derive it -- and the row stays
    frozen in ``needs_owner`` until an Owner decision, which removes the marker
    through the ordinary path anyway.
    """
    path = home / "autonomy" / "work.db"
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        rows = conn.execute(
            "SELECT id, completion_result, refs_json FROM work WHERE state='needs_owner'"
        ).fetchall()
    out = []
    for work_id, result, refs_json in rows:
        try:
            refs = json.loads(refs_json or "{}")
        except ValueError:
            continue
        if MARKER not in refs or refs[MARKER] is None:
            continue
        marker = refs[MARKER]
        # `work_state` suppresses on the key being present and non-null, so a
        # falsy value -- `false`, `0`, `""`, `[]` -- hides the badge exactly as
        # well as a real marker. Testing truthiness here left those hidden for
        # good.
        if not isinstance(marker, dict):
            # `work_state` suppresses on the key's presence, so a hand-edited or
            # imported `true` hides the badge exactly as well as a real marker --
            # and skipping it here left that obligation hidden with no way back.
            out.append((work_id, marker,
                        "a malformed marker no build writes; it suppresses the badge anyway"))
            continue
        if marker.get("repaired_by") != REPAIRED_BY:
            # No build writes a marker from the runtime any more, so one that is
            # not this tool's came from the mechanism that was deleted for being
            # unsound. Asking for recorded effects before revoking it preserves
            # exactly the conclusion that mechanism got wrong: the effects it
            # missed are the ones the ledger never recorded. So it goes, and the
            # row is visible again until a human says otherwise.
            out.append((work_id, marker,
                        "written by a mechanism that has been removed as unsound"))
            continue
        delivery = (refs.get("owner_delivery") or {}).get("message_id")
        if (isinstance(marker, dict) and marker.get("delivery") is not None
                and str(marker["delivery"]) != str(delivery)):
            out.append((work_id, marker,
                        "this row has delivered a different outcome since that decision"))
            continue
        if marker.get("adjudicated"):
            # A human named this row and said what the transcript showed. That
            # OUTRANKS every guard below, recorded effects and `ever_admitted`
            # included: a finished effect and an abandoned one are identical
            # here, and only the transcript separates them. Stated plainly
            # because it is an exception, not an invariant. The row stays frozen
            # in `needs_owner`; an Owner decision is the only way out, and it
            # clears the marker itself.
            continue
        if _records_an_effect(refs):
            out.append((work_id, marker, "the ledger records this task acting"))
            continue
        ok, reason = _selectable(result, refs)
        if not ok:
            out.append((work_id, marker, reason))
    return out


def needs_delivery_stamp(home: Path):
    """Markers this tool wrote before it recorded which delivery they were about."""
    path = home / "autonomy" / "work.db"
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        rows = conn.execute(
            "SELECT id, refs_json FROM work WHERE state='needs_owner'"
        ).fetchall()
    out = []
    for work_id, refs_json in rows:
        try:
            refs = json.loads(refs_json or "{}")
        except ValueError:
            continue
        marker = refs.get(MARKER)
        delivery = (refs.get("owner_delivery") or {}).get("message_id")
        if (isinstance(marker, dict) and marker.get("repaired_by") == REPAIRED_BY
                and marker.get("delivery") is None and delivery is not None):
            out.append((work_id, delivery))
    return out


def backup(home: Path):
    path = home / "autonomy" / "work.db"
    target = path.with_name(f"work.db.pre-unadmitted-repair-{time.strftime('%Y%m%d-%H%M%S')}")
    with closing(sqlite3.connect(str(path))) as conn, closing(sqlite3.connect(str(target))) as out:
        conn.backup(out)
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="Hermes data root")
    parser.add_argument("--apply", action="store_true", help="write; otherwise dry run")
    parser.add_argument(
        "--reason", default="", metavar="TEXT",
        help="what the transcript showed; required with --adjudicated and recorded "
             "in the marker",
    )
    parser.add_argument(
        "--adjudicated", default="", metavar="ID[,ID...]",
        help="work ids a human inspected in the canonical transcript and found to "
             "have left nothing for the Owner; accepted only for rows that are "
             "otherwise exactly this defect's shape",
    )
    args = parser.parse_args(argv)

    root = args.root.expanduser().resolve()
    adjudicated = {i.strip() for i in args.adjudicated.split(",") if i.strip()}
    if adjudicated and not args.reason.strip():
        print("--adjudicated requires --reason: say what the transcript showed",
              file=sys.stderr)
        return 2
    homes = profile_homes(root)
    if not homes:
        print(f"no autonomy ledger under {root}", file=sys.stderr)
        return 1

    total = repaired = held = unmarked = 0
    for name, home in homes:
        # A marker this tool wrote before it recorded deliveries suppresses
        # nothing now. The row has not delivered anything since -- its outcome is
        # still the escalation that decision was made about -- so the delivery it
        # was about is the one on the row, and stamping it preserves the decision
        # instead of quietly discarding it.
        upgraded = needs_delivery_stamp(home)
        for work_id, message_id in upgraded:
            print(f"{name}: STAMP {work_id} -- recording the delivery ({message_id})"
                  " this decision was made about")
        if upgraded and args.apply:
            from agent.autonomy import store as _store
            for work_id, message_id in upgraded:
                current = _store.get_work(work_id, home)
                marker = dict((current["refs"] or {}).get(MARKER) or {})
                marker["delivery"] = message_id
                _store.update_work(work_id, refs={MARKER: marker}, hermes_home=home,
                                   expected_state="needs_owner")

        stale = wrongly_marked(home)
        for work_id, _marker, why in stale:
            print(f"{name}: UNMARK {work_id} -- {why}; its badge must come back")
        if stale and args.apply:
            from agent.autonomy import store as _store
            for work_id, marker, _why in stale:
                # CAS on the marker that was READ: a concurrent run may have
                # replaced it with a valid human adjudication in between.
                if _store.update_work(work_id, refs={MARKER: None}, hermes_home=home,
                                      expected_state="needs_owner",
                                      expected_refs={MARKER: marker}) is not None:
                    unmarked += 1
                    print(f"  {work_id}: marker removed (badge restored)")
        selected, deferred = stranded(home, adjudicated)
        for row in deferred:
            held += 1
            print(
                f"{name}: HELD {row['id']} generation={row['resume_generation']} "
                f"attempts={row['resume_attempts']} ever_admitted={row['ever_admitted']}"
                f" -- {row['reason']}; inspect by hand"
            )
        if not selected:
            continue
        total += len(selected)
        print(f"{name}: {len(selected)} stranded escalation(s)")
        for row in selected:
            print(
                f"  {row['id']}  updated={row['updated_at']}  attempts={row['resume_attempts']}"
                f"  delivered_message={row['delivered_message_id']}"
            )
        if not args.apply:
            continue
        print(f"  backup -> {backup(home)}")
        from agent.autonomy import store

        for row in selected:
            try:
                updated = store.update_work(
                    row["id"],
                    refs={MARKER: {"attempts": row["resume_attempts"], "at": time.time(),
                                   "repaired_by": REPAIRED_BY,
                                   # The delivery this decision was made ABOUT.
                                   # `work_state` requires it to still be the
                                   # row's current one, so a later, different
                                   # question on the same task is never covered
                                   # by an older adjudication.
                                   "delivery": row["delivered_message_id"],
                                   "adjudicated": row["adjudicated"] or None,
                                   "reason": args.reason if row["adjudicated"] else None}},
                    hermes_home=home,
                    expected_state="needs_owner",
                    expected_refs=row["expected_refs"],
                )
            except ValueError as exc:
                # One unexpected row must not abandon the rest half-done.
                print(f"  {row['id']}: SKIPPED ({exc})")
                continue
            if updated is None:
                print(f"  {row['id']}: SKIPPED (changed concurrently)")
                continue
            repaired += 1
            print(f"  {row['id']}: marked continuation_unstarted "
                  f"({'human-adjudicated, ' if row['adjudicated'] else ''}"
                  "still needs_owner, still rebindable)")

    if unmarked:
        print(f"{unmarked} row(s) un-marked; their badge is visible again")
    if held:
        print(f"{held} row(s) held back for inspection; none of them were changed")
    if not total:
        print("nothing stranded; ledger already clean")
    elif args.apply:
        print(f"repaired {repaired}/{total}")
    else:
        print(f"{total} row(s) would be repaired; re-run with --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
