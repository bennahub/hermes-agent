#!/usr/bin/env python3
"""Type the transport-failure notices the defect wrote into Owner transcripts.

``run_resume`` used to answer a continuation dispatch that never reached
``bind_turn`` with an assistant line in the canonical Bot Chat:

    The continuation was interrupted before a verified outcome. Existing effects
    need inspection before retry; no action was replayed.

Nothing had run, so there were no effects to inspect -- and the line is
indistinguishable, in the transcript and in every sidebar preview, from the
agent's own speech. It is the last thing an Owner sees on a chat whose real last
answer was "لا يوجد إجراء مطلوب."

This does NOT delete it. The row keeps its content, its timestamp and its place;
what changes is one thing, through the mechanism that already exists for it: the
native display projection. ``origin: runtime`` says Hermes' continuation
machinery wrote it, not the agent; ``purpose: progress`` says it reports on that
machinery rather than answering the Owner. ``agent.message_projection`` then
keeps it out of the Owner timeline and the preview exactly as it does for every
other runtime notice, and ``display_row`` types it ``hidden`` for clients.

The current build no longer writes this line at all: a dispatch that never
started re-arms, and if it exhausts its retries it says so about the ATTEMPT.

Proof, per row, is the ledger's own delivery receipt -- not the sentence:

  * a source-bound Owner work row carrying ``continuation_unstarted``, which is
    written only where the ledger (or a recorded human adjudication) established
    that no task turn ran
  * ``refs["owner_delivery"]`` naming this exact session and message id
  * ``sha256(content)`` equal to the ``content_sha256`` in that receipt

Idempotent: a row already carrying the projection is skipped. Dry run unless
``--apply``; every write is preceded by a backup of that profile's state.db.

    python3 tools/project_unstarted_continuation_notices.py --root ~/.hermes
    python3 tools/project_unstarted_continuation_notices.py --root ~/.hermes --apply
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

from agent.autonomy.owner_continuity import UNSTARTED_CONTINUATION as MARKER
from agent.message_projection import native_metadata, projection


def profile_homes(root: Path):
    homes = []
    if (root / "autonomy" / "work.db").is_file():
        homes.append(("default", root))
    profiles = root / "profiles"
    if profiles.is_dir():
        for child in sorted(profiles.iterdir()):
            if (child / "autonomy" / "work.db").is_file():
                homes.append((child.name, child))
    return homes


def delivered_notices(home: Path):
    """Every delivery receipt on a row proven to be a continuation that never ran."""
    path = home / "autonomy" / "work.db"
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        rows = conn.execute("SELECT id, refs_json FROM work").fetchall()
    out = []
    for work_id, refs_json in rows:
        try:
            refs = json.loads(refs_json or "{}")
        except ValueError:
            continue
        delivery = refs.get("owner_delivery")
        if not refs.get(MARKER) or not isinstance(delivery, dict):
            continue
        session_id = delivery.get("source_session_id", delivery.get("session_id"))
        if not session_id or delivery.get("message_id") is None:
            continue
        out.append({
            "work_id": work_id,
            "delivery_session_id": str(delivery.get("session_id") or ""),
            "session_id": str(session_id),
            "message_id": int(delivery["message_id"]),
            "content_sha256": delivery.get("content_sha256"),
        })
    return out


def _lineage(state_db: Path, notice):
    """Every session id the delivery could legitimately live in.

    The receipt names two: the conversation the task was bound to, and the
    session the delivery actually landed in when that conversation had been
    compressed. Compaction moves a message WITHIN one of those, never into
    somebody else's chat -- so this is the boundary the search must respect.
    An external review copied a work id and a matching body into an unrelated
    conversation and watched the previous version hide it.
    """
    ids = {notice["session_id"]}
    if notice.get("delivery_session_id"):
        ids.add(notice["delivery_session_id"])
    try:
        from hermes_state import SessionDB

        db = SessionDB(state_db, read_only=True)
        try:
            # Walk it to a fixed point. `get_compression_lineage` answers for the
            # session it is given, and a compression child of an explicit fork is
            # one hop further out than either of the receipt's own two ids -- the
            # carried notice lives there, visible, and a single hop missed it.
            for _ in range(8):
                grown = set(ids)
                for seed in ids:
                    for related in db.get_compression_lineage(seed) or []:
                        grown.add(related)
                    tip = db.get_compression_tip(seed)
                    if tip:
                        grown.add(tip)
                if grown == ids:
                    break
                ids = grown
        finally:
            db.close()
    except Exception:
        pass  # the receipt's own two ids remain the boundary
    return ids


def matching_rows(state_db: Path, notice):
    """Every row that receipt proves, wherever compaction has since moved it.

    The receipt's (session, id) address is not enough on its own: a delivery
    into a compressed session records the child while the receipt names the
    ancestor, and in-place compaction re-inserts the carried message under a NEW
    physical id. So two things are matched, and BOTH must hold: the row lives in
    this delivery's own conversation lineage, and its content hashes to the
    receipt's `content_sha256`. A row carrying the work id in its own projection
    envelope qualifies wherever it sits in that lineage; the receipt's exact
    address also qualifies, which is how a legacy notice with no envelope at all
    is still repaired.
    """
    lineage = _lineage(state_db, notice)
    out, skipped = [], []
    with closing(sqlite3.connect(state_db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        rows = conn.execute(
            "SELECT session_id, id, role, content, display_kind, display_metadata"
            " FROM messages WHERE role='assistant'"
        ).fetchall()
    for session_id, message_id, role, content, display_kind, metadata in rows:
        if session_id not in lineage:
            continue
        digest = hashlib.sha256((content or "").encode("utf-8")).hexdigest()
        if digest != notice["content_sha256"]:
            continue
        value = projection(metadata)
        # The receipt names two sessions: the source the task was bound to, and
        # the session the delivery actually landed in when that source had been
        # compressed. An envelope-free row can only be identified by address, so
        # BOTH have to count as its address or a legacy delivery into the child
        # is unreachable.
        addressed = (int(message_id) == int(notice["message_id"])
                     and session_id in {notice["session_id"],
                                        notice.get("delivery_session_id")})
        if value is None:
            # A legacy notice with no envelope has nothing to carry its identity,
            # so the receipt's own address is the only thing that names it. A
            # compaction-carried copy is therefore out of reach, and it stays out
            # of reach: reaching it by "the only row in this lineage with this
            # text" hid a second, unrelated message that merely repeated the
            # sentence, and stamped one task's work id onto another's notice.
            # Uniqueness among text matches is not delivery identity. A visible
            # duplicate is a smaller harm than hiding Owner-facing speech.
            if not addressed:
                continue
        elif value.get("work_id") != notice["work_id"]:
            continue
        elif value["origin"] == "runtime" and value["purpose"] == "progress":
            skipped.append((session_id, message_id, "already typed"))
            continue
        elif not (value["origin"] == "agent" and value["audience"] == "owner"
                  and value["purpose"] in {"decision", "result"}):
            # The only envelope this may replace is the delivery's own: an agent
            # answering the Owner. That is precisely the claim being corrected.
            skipped.append((session_id, message_id,
                            f"carries an unrelated {value['origin']}/{value['purpose']} envelope"))
            continue
        out.append({"session_id": session_id, "message_id": message_id,
                    "content": content, "metadata": metadata})
    return out, skipped


def orphaned_projections(home: Path, state_db: Path):
    """Notices still typed `runtime/progress` whose row is no longer marked.

    Revoking a false marker hands the badge back without the sentence that
    explains it, and the ordinary pass cannot reach it because that pass only
    looks at MARKED rows. This is the exact inverse -- and it is held to exactly
    the same proof, because a first version of it checked only the work id and
    turned an unrelated internal message in another conversation into
    Owner-facing agent speech. The receipt names the row: its address, its
    content hash, its lineage. Nothing else is touched.
    """
    path = home / "autonomy" / "work.db"
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        rows = conn.execute("SELECT id, refs_json FROM work").fetchall()
    out = []
    for work_id, refs_json in rows:
        try:
            refs = json.loads(refs_json or "{}")
        except ValueError:
            continue
        delivery = refs.get("owner_delivery")
        if MARKER in refs and refs.get(MARKER):
            continue  # still marked: the forward pass owns it
        if not isinstance(delivery, dict) or not delivery.get("content_sha256"):
            continue
        session_id = str(delivery.get("source_session_id", delivery.get("session_id")) or "")
        if not session_id or delivery.get("message_id") is None:
            continue
        notice = {"work_id": work_id, "session_id": session_id,
                  "delivery_session_id": str(delivery.get("session_id") or ""),
                  "message_id": int(delivery["message_id"]),
                  "content_sha256": delivery["content_sha256"]}
        lineage = _lineage(state_db, notice)
        # Addressed OR carrying its own envelope, exactly as the forward pass
        # accepts: compaction re-inserts a row under a new id, and requiring the
        # receipt's original id left the visible copy hidden with the badge back.
        with closing(sqlite3.connect(state_db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            found = conn.execute(
                "SELECT session_id, id, content, display_metadata FROM messages"
                " WHERE role='assistant' AND display_metadata IS NOT NULL"
                " AND session_id IN (%s)" % ",".join("?" * len(lineage)),
                tuple(sorted(lineage)),
            ).fetchall()
        for sid, mid, content, metadata in found:
            digest = hashlib.sha256((content or "").encode("utf-8")).hexdigest()
            if digest != notice["content_sha256"]:
                continue
            value = projection(metadata)
            if (value is None or value.get("origin") != "runtime"
                    or value.get("purpose") != "progress"
                    or value.get("work_id") != work_id):
                continue
            out.append({"session_id": sid, "message_id": mid, "content": content,
                        "metadata": metadata, "work_id": work_id})
    return out


def backup(state_db: Path):
    target = state_db.with_name(
        f"state.db.pre-notice-projection-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    with closing(sqlite3.connect(str(state_db))) as conn, closing(sqlite3.connect(str(target))) as out:
        conn.backup(out)
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--apply", action="store_true", help="write; otherwise dry run")
    args = parser.parse_args(argv)

    root = args.root.expanduser().resolve()
    homes = profile_homes(root)
    if not homes:
        print(f"no autonomy ledger under {root}", file=sys.stderr)
        return 1

    total = typed = skipped = 0
    for name, home in homes:
        state_db = home / "state.db"
        if not state_db.is_file():
            continue
        restore = orphaned_projections(home, state_db)
        for row in restore:
            print(f"{name}: RESTORE message {row['message_id']} -- work"
                  f" {row['work_id']} is no longer marked, so its notice must be"
                  " visible again")
        if restore and args.apply:
            written = 0
            print(f"  backup -> {backup(state_db)}")
            with closing(sqlite3.connect(str(state_db))) as conn:
                for row in restore:
                    previous = row["metadata"]
                    if isinstance(previous, (dict, list)):
                        previous = json.dumps(previous, ensure_ascii=False)
                    source = json.loads(previous or "{}")
                    cursor = conn.execute(
                        "UPDATE messages SET display_metadata=? WHERE session_id=?"
                        " AND id=? AND role='assistant' AND content=?"
                        " AND IFNULL(display_metadata,'') = IFNULL(?,'')",
                        (json.dumps(native_metadata("agent", "owner", "decision",
                                                    metadata=source,
                                                    work_id=row["work_id"]),
                                    ensure_ascii=False),
                         row["session_id"], row["message_id"], row["content"], previous),
                    )
                    if cursor.rowcount == 1:
                        written += 1
                    else:
                        print(f"  message {row['message_id']}: SKIPPED (changed concurrently)")
                conn.commit()
            print(f"  {written}/{len(restore)} notice(s) restored to the delivery's"
                  " own envelope")

        pending = []
        for notice in delivered_notices(home):
            if not notice["content_sha256"]:
                # The receipt IS the proof. Without its hash nothing here
                # distinguishes the notice from an Owner question.
                print(f"{name}: SKIP {notice['work_id']} -- the delivery receipt"
                      " carries no content hash to prove any row")
                skipped += 1
                continue
            found, ignored = matching_rows(state_db, notice)
            for _sid, _mid, why in ignored:
                if why != "already typed":
                    print(f"{name}: SKIP {notice['work_id']} message {_mid} -- {why}")
                skipped += 1
            for row in found:
                pending.append((notice, row))
        # Two receipts can name one physical row when both deliveries carried the
        # identical sentence and the row has no envelope to say whose it is.
        # Typing it would stamp one work id over another's, so it is held.
        claimed = {}
        for notice, row in pending:
            claimed.setdefault((row["session_id"], row["message_id"]), set()).add(
                notice["work_id"])
        contested = {k for k, v in claimed.items() if len(v) > 1}
        if contested:
            for session_id, message_id in sorted(contested):
                print(f"{name}: HELD message {message_id} -- claimed by"
                      f" {len(claimed[(session_id, message_id)])} deliveries; no envelope"
                      " says which, so nothing is stamped")
                skipped += 1
            pending = [(n, r) for n, r in pending
                       if (r["session_id"], r["message_id"]) not in contested]
        if not pending:
            continue
        total += len(pending)
        print(f"{name}: {len(pending)} continuation notice(s) to type")
        for notice, row in pending:
            print(f"  session {row['session_id']} message {row['message_id']}"
                  f"  (work {notice['work_id']})")
        if not args.apply:
            continue
        print(f"  backup -> {backup(state_db)}")
        with closing(sqlite3.connect(str(state_db))) as conn:
            for notice, row in pending:
                metadata = native_metadata(
                    "runtime", "owner", "progress",
                    metadata=row["metadata"], work_id=notice["work_id"],
                )
                # Fence the metadata that was READ as well as the content: a
                # reaction, or someone else's envelope correction, can land
                # between selection and here, and this must not erase it.
                previous = row["metadata"]
                if isinstance(previous, (dict, list)):
                    previous = json.dumps(previous, ensure_ascii=False)
                cursor = conn.execute(
                    "UPDATE messages SET display_metadata=? WHERE session_id=? AND id=?"
                    " AND content=? AND role='assistant'"
                    " AND IFNULL(display_metadata,'') = IFNULL(?,'')",
                    (json.dumps(metadata, ensure_ascii=False),
                     row["session_id"], row["message_id"], row["content"],
                     previous),
                )
                if cursor.rowcount == 1:
                    typed += 1
                    print(f"  message {row['message_id']}: typed runtime/progress"
                          " (content unchanged)")
                else:
                    print(f"  message {row['message_id']}: SKIPPED (changed concurrently)")
            conn.commit()

    if not total:
        print("nothing to type; transcripts already projected")
    elif args.apply:
        print(f"typed {typed}/{total}; {skipped} receipt(s) skipped")
    else:
        print(f"{total} notice(s) would be typed; re-run with --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
