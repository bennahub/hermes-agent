"""Structured identity for agent-to-agent collaboration (BWM-797 Scope 5).

Hermes delivers an agent's message to another agent by *prepending a sentence*
to it — `"Message from 🤖 joud (@joud): "` — and letting the recipient's Bot Chat
persist it as an ordinary user message. That works for delivery and records
nothing: no thread, no sender id, no recipient list, no event id. The owner's
client could only recover the sender by parsing that sentence back out of the
prose, which is precisely what a structured view must not do.

This is the identity layer, added at the one point where the server already
knows both ends: the send site in `tools/bot_mode_dm`. Every send is recorded
with a durable thread id, an event id, the sender, the recipients, the time, and
any artifact references — the same artifact references a 1:1 message and a room
event carry, because it is the same registry.

**This is not a second messaging system.** Nothing is delivered from here and
nothing reads it to decide what an agent sees; delivery is untouched. It is the
ledger the *owner* inspects, and the reason it holds the delivered text is that
a collaboration view has to show what was actually said, in order, in both
directions — and the two halves of a conversation live in two different
profiles' databases. Joining those at read time by matching prose would
reintroduce exactly the fragility this exists to remove.

**Threads are pairwise *within one collaboration run*; fan-out is not a thread.**
A thread id is derived from the unordered pair of participants **and the run they
are collaborating in**, so both directions of one conversation share it and a
reply lands in the same thread as what it answers. A message sent to thirteen
agents is *one send event* with thirteen recipients, one run, and thirteen
pairwise threads — which is what keeps "Messaged 13 agents" a single
collaboration card while each exchange stays separately openable. An earlier
attempt keyed threads by counterpart and collapsed that fan-out; the distinction
is the whole point.

**The run is why a pair is not a thread.** Keying a thread on the pair alone
made every message two agents ever exchanged one endless conversation: opening
today's OAuth delegation between abu-saud and faisal showed their finance and
deployment traffic from months ago, because the only thing the id knew was that
the same two names were involved. The run is the missing half. It is *derived
from durable identifiers Hermes already owns* — the autonomy work item, the
owner request that woke the sender, the sender's own turn — and never from what
a message says. Topic membership that came out of prose would be a guess, and a
guess is what put the finance messages in the OAuth thread.

The run is the canonical collaboration identity for every surface:

    COLLABORATION RUN  (run_id)
      ├── abu-saud ↔ faisal   (thread_id = H(run_id, pair))
      ├── abu-saud ↔ badr     (thread_id = H(run_id, pair))
      └── abu-saud ↔ joud     (thread_id = H(run_id, pair))

`collaboration_run` is the **only** function in Hermes that mints a run id, and
`RUN_ANCHORS` is the whole list of things a run may be derived from. Two
spellings of "which run is this" is what once put a request and its own reply on
two different cards, so there is one spelling and every writer and reader
reaches it through this function — including the admission check for a run that
arrives from outside, which is that same function under the `inherited` anchor.

**Legacy rows are left exactly where they are.** Events written before the run
existed carry an empty run, and an empty run reproduces the historical
pair-only digest byte for byte — so their thread ids do not move, their history
stays inspectable, and nothing rewrites it.

An empty run marks the *conversation*, not the moment of writing. A reply to a
legacy request is written blank too, however long after the upgrade it lands,
because it inherits the run of what it answers and that run is blank — which is
what keeps a request and its own answer one card instead of two. So a blank
column reads as "belongs to a conversation that predates runs", and that reading
is exact: a legacy thread can never absorb a newly typed message and a newly
typed thread can never be contaminated by one, because the two eras spell a
thread id with different digests.

**Two surfaces read this ledger, and neither is a substitute for the other.**
`runs_for` answers "what collaborations did this agent take part in" — one entry
per run, which is one card in the owner's transcript. `threads_for` answers
"what conversations is this agent in" — one entry per thread. Run scoping made
that second list grow with *runs × pairs* rather than with the roster, so it is
paged: it returns a cursor and the caller asks for the next page.

Nothing may conclude that a conversation is empty because it did not appear on a
page. `resolve_thread` addresses one thread directly, by counterpart and run,
and a direct answer is the only authority on "there is nothing here". A listing
that had merely been cut short once told the owner that two agents had never
messaged each other while the exchange sat intact in this very table.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence


#: Bodies are bounded in the ledger. The delivered message is bounded already
#: (`MESSAGE_MAX_CHARS`); this is a second belt so a misbehaving caller cannot
#: grow the index without limit.
MAX_BODY_CHARS = 40_000

#: The most events one `runs_for` listing will **materialise**, newest first. A
#: run is a handful of sends and the answers to them, and the page is fifty
#: runs, so this is two orders of magnitude clear of any real ledger; it is here
#: so that a chain which grew without bound cannot make opening a chat unbounded
#: too.
#:
#: **It bounds materialisation, not arithmetic**, and the distinction is the
#: whole of it. What it cuts is the scan that reads bodies into `A2AEvent`s to
#: say what was *said*. Every number a card carries — how many messages, when
#: the run started, whether anyone answered — is computed by SQL aggregate over
#: the page's whole key set with no ceiling on it, because a dropped event is
#: not a message that did not happen. Counted from the survivors instead, a
#: legacy ledger of twelve thousand events across fourteen pairs reported ten
#: thousand and said the listing was complete: legacy keys are roster-bounded,
#: so every key still materialised, nothing fell out, and the recovery below
#: never fired.
#:
#: Flat rather than `limit`-proportional on purpose. A card's message count must
#: be the same number whoever asked for it — a caller requesting one run and a
#: caller requesting fifty must not be told two different things about the same
#: run — and a budget divided by the page size does exactly that. It is a
#: ceiling on work and nothing else: no grouping decision is taken here.
#:
#: What happens if a page's runs together exceed it: the oldest events are the
#: ones dropped, so the oldest runs on that page materialise empty and fall out
#: of it. They are not lost. The cursor is taken from the last card that *did*
#: materialise, so the next page resumes above them and picks them up — a page
#: comes up short rather than a run going missing. A run that materialised only
#: in part is still described in full: the threads under it come from the
#: aggregate, and the few whose every event was dropped have their last message
#: fetched by seek. Reaching this at all takes ten thousand events across one
#: page of runs; the live ledger holds a few hundred in total.
MAX_LISTING_EVENTS = 10_000

#: The exact shape of a canonical run id — the prefix and 24 hex characters of a
#: SHA-256 digest. Checked rather than assumed, because an inherited run arrives
#: inside an agent's message envelope and, once accepted, is written to a
#: database column, published on the wire, and used by the clients as a view
#: identity and an accessibility identifier. Anything not of this shape is
#: hashed into it rather than carried through.
_RUN_ID = re.compile(r"\Arun-[0-9a-f]{24}\Z")

#: Query-local marker for "this row predates runs, so its *thread* is its run".
#: `_run_of_key` turns it back into the canonical id before it reaches any
#: field a client reads. It exists because the canonical id is a hash, and a
#: reader that groups by the hash cannot then ask SQL which rows produced it;
#: carrying the thread forward keeps the follow-up query an index seek on
#: `a2a_events_thread` rather than a scan. A recorded run can never collide with
#: it: every one is `run-<hex>`, which `collaboration_run` is the only door to.
#:
#: The one place it does travel is inside a `runs_for` page cursor, which is
#: opaque and holds that listing's sort key — a thread id, which the same
#: response already carries in the open.
_LEGACY_KEY = "legacy:"

#: The table, then the columns added after it shipped, then the indexes — in
#: that order and never merged. `CREATE TABLE IF NOT EXISTS` is a no-op against
#: a database an older build already created, so an index over a column added
#: later must not be attempted until the `ALTER TABLE` has run; putting them in
#: one script made every existing deployment fail to open its own ledger.
_TABLE = """
CREATE TABLE IF NOT EXISTS a2a_events (
    event_id     TEXT PRIMARY KEY,
    thread_id    TEXT NOT NULL,
    sender       TEXT NOT NULL,
    recipient    TEXT NOT NULL,
    body         TEXT NOT NULL,
    attachments  TEXT NOT NULL DEFAULT '[]',
    sent_at      REAL NOT NULL,
    send_id      TEXT NOT NULL DEFAULT '',
    fanout       INTEGER NOT NULL DEFAULT 1,
    run_id       TEXT NOT NULL DEFAULT ''
);
"""

#: When an agent was deleted — the instant its name stopped being that agent's.
#:
#: A thread keys on the agent's NAME, and a name is a slug the Owner can mint
#: again, so a recreated `badr` addressed the deleted `badr`'s conversations and
#: `runs_for` listed them as the new agent's cards — after the delete
#: confirmation had told the Owner "their conversations go with them". That is
#: the same defect the browser identity had (a reused slug passed
#: `BrowserIdentity.allows()` and `attach_identity` mounted the previous agent's
#: cookies) and it gets the same answer: strike the new holder's claim, keep the
#: artefact enumerable and auditable.
#:
#: **Append-only, like the ledger it floors.** Nothing here rewrites, moves or
#: deletes an `a2a_events` row. The audit trail is the point of that table, and
#: the exchange really did happen — the counterpart's own history says so and
#: stays whole. What a retirement changes is only what the NAME may address
#: afterwards. Several rows per agent is normal: a slug can be created and
#: deleted repeatedly, and the floor is the newest of them.
_RETIREMENTS = """
CREATE TABLE IF NOT EXISTS a2a_retirements (
    agent       TEXT NOT NULL,
    retired_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS a2a_retirements_agent ON a2a_retirements(agent, retired_at);
"""

#: `ADD COLUMN` with a NOT NULL default backfills every existing row with the
#: empty run, which is exactly the intended reading of a row recorded before
#: provenance existed — and leaves its `thread_id` untouched, because the empty
#: run reproduces the original digest.
_MIGRATIONS = (("run_id", "ALTER TABLE a2a_events ADD COLUMN run_id TEXT NOT NULL DEFAULT ''"),)

_INDEXES = """
CREATE INDEX IF NOT EXISTS a2a_events_thread ON a2a_events(thread_id, sent_at);
CREATE INDEX IF NOT EXISTS a2a_events_sender ON a2a_events(sender, sent_at);
CREATE INDEX IF NOT EXISTS a2a_events_recipient ON a2a_events(recipient, sent_at);
CREATE INDEX IF NOT EXISTS a2a_events_send ON a2a_events(send_id);
CREATE INDEX IF NOT EXISTS a2a_events_run ON a2a_events(run_id, sent_at);
"""


def deployment_root(home: Path | str) -> Path:
    """The deployment root, given any profile home or the root itself.

    The same rule `bot_mode_dm._hermes_root` applies, stated here so the writer
    and the reader cannot disagree about where the ledger lives — a thread has
    two ends and exactly one index.
    """
    path = Path(home)
    return path.parent.parent if path.parent.name == "profiles" else path


def _db_path(home: Path | str) -> Path:
    # Deployment-level, not per-profile: a thread has two ends that live in two
    # different profiles, and the only place that can see both is here.
    return deployment_root(home) / "a2a_threads.db"


def _connect(home: Path | str) -> sqlite3.Connection:
    path = _db_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(_TABLE)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(a2a_events)")}
    for column, statement in _MIGRATIONS:
        if column in columns:
            continue
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            # Another process read `table_info` before our `ALTER` committed and
            # got there first, so this one raises `duplicate column name`. That
            # is a race, not a failure: fifteen profile processes and a gateway
            # restart open this ledger within the same second of a deploy, and
            # the loser used to propagate out of `_connect` — `record_send`
            # then answered "could not be recorded; nothing was sent" and
            # `threads_for` turned `a2a.threads` into error 5041, for exactly
            # the deploy that introduced the column.
            #
            # Re-read rather than blanket-suppress: if the column is there now,
            # whose `ALTER` created it does not matter. If it is not, the
            # failure was real and must still be raised.
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(a2a_events)")}
            if column not in columns:
                raise
    conn.executescript(_INDEXES)
    # After the events table and its migration, and in its own script: a fresh
    # `CREATE TABLE IF NOT EXISTS` is a no-op on every deployment that already
    # has it, and on the one deploy that does not, it races the same way the
    # events table does and is safe for the same reason — SQLite serialises the
    # write and the loser's `IF NOT EXISTS` then finds it there.
    conn.executescript(_RETIREMENTS)
    conn.commit()
    return conn


def _norm(name: Any) -> str:
    return str(name or "").strip().lower().lstrip("@")


def _retirement_floor(conn: sqlite3.Connection, agent: str) -> float:
    """The instant *agent*'s name last stopped being a live agent's, or 0.0.

    0.0 for the overwhelmingly common case — a name that has never been deleted
    — and every caller treats that as "no floor" and changes nothing at all.
    """
    if not agent:
        return 0.0
    row = conn.execute(
        "SELECT MAX(retired_at) AS floor FROM a2a_retirements WHERE agent = ?", (agent,)
    ).fetchone()
    try:
        floor = float(row["floor"]) if row is not None and row["floor"] is not None else 0.0
    except (TypeError, ValueError):
        return 0.0
    # A view predicate is built by formatting, because SQLite will not bind a parameter into a
    # `CREATE VIEW`. Finite is therefore not a nicety: `inf` and `nan` have no SQL spelling, and a
    # column any writer can reach must not be able to make the statement unparseable.
    return floor if math.isfinite(floor) else 0.0


def _connect_as(home: Path | str, agent: Any) -> sqlite3.Connection:
    """A connection on which `a2a_events` holds only what *agent* may address.

    Every agent-scoped read goes through here rather than through `_connect`, and
    this is the ONE place the retirement rule is applied. Writing it as a
    predicate instead would mean threading a floor through a dozen SQL fragments
    across `threads_for`, `runs_for` and `resolve_thread` — including the two
    that share one parameter list — and the rule would then hold only for as
    long as every one of them remembered it. A query cannot forget this.

    **A name that was never retired gets no view and no change whatsoever.** The
    floor is 0.0, this returns exactly what `_connect` returned before, and every
    statement below is byte-for-byte the one that ran yesterday. Whatever a
    retirement costs is paid only by a slug that has actually been deleted and
    minted again.

    The mechanism is SQLite's own name resolution: an unqualified table name
    resolves against `temp` before `main`, so a temp view named `a2a_events`
    shadows the table for every statement on this connection — inside CTEs,
    subqueries and window functions alike. Two consequences worth stating
    plainly. Qualifying a read as `main.a2a_events` would step around the floor,
    which is why nothing in this module qualifies it. And the view is read-only
    and dies with the connection, so no write path can reach it: `record_send`
    and `discard_send` use `_connect`, and the ledger stays append-only.
    """
    name = _norm(agent)
    conn = _connect(home)
    try:
        floor = _retirement_floor(conn, name)
        if floor > 0:
            conn.execute(
                "CREATE TEMP VIEW a2a_events AS "
                f"SELECT * FROM main.a2a_events WHERE sent_at >= {float(floor)!r}"
            )
    except BaseException:
        conn.close()
        raise
    return conn


def retire_agent(home: Path | str, agent: Any, *, at: Optional[float] = None) -> float:
    """Record that *agent*'s name is no longer the agent that made this history.

    Called from `delete_profile`, whose one job here is to make sure the next
    holder of the slug starts from nothing. Returns the retirement instant, or
    0.0 when there was nothing to retire.

    No ledger, no retirement: a deployment whose agents have never messaged each
    other has no history for a reused name to inherit, and this must not create
    an empty database to say so.
    """
    name = _norm(agent)
    if not name or not _db_path(home).exists():
        return 0.0
    retired_at = time.time() if at is None else float(at)
    conn = _connect(home)
    try:
        conn.execute("INSERT INTO a2a_retirements (agent, retired_at) VALUES (?, ?)",
                     (name, retired_at))
        conn.commit()
    finally:
        conn.close()
    return retired_at


#: The anchors a run may be derived from, strongest first. Each names durable
#: Hermes identifiers and nothing else; there is no text-derived anchor, and
#: adding one would reintroduce the guess this replaced.
#:
#: ``inherited`` is first and is the one anchor whose value comes from outside
#: this process — the run of the request being answered, carried in the message
#: envelope. It is admitted through the same function as every other anchor
#: precisely *because* it comes from outside: that is where the shape check
#: lives, and there is nowhere else to put a value into the column.
#: ``legacy`` is the read-time answer for a row recorded before the column
#: existed, and it anchors on that row's *thread* rather than on its send. A
#: legacy request and the reply written to it after the upgrade both store a
#: blank run — the reply inherits the request's, which is blank — so anchoring
#: on the send gave one exchange two ids and drew it as two cards, which is
#: precisely the defect runs exist to remove. Their thread they do share.
RUN_ANCHORS = ("inherited", "work", "owner", "turn", "send", "legacy")


def collaboration_run(kind: str, *parts: Any) -> str:
    """The canonical id of one collaboration run — the only function that mints one.

    ``kind`` names *why* these messages belong together — which of Hermes'
    existing causal identities is the root of this episode — and ``parts`` are
    that identity's own components. The pair is hashed, so the run is a fixed
    shape, is not mistakable for a routing handle, and cannot be reversed into
    the session or work id it was derived from by a client that should not
    have them.

    Nothing here reads a message. A run is a statement about *provenance*, and
    the only inputs are ids Hermes already assigned.

    The ``inherited`` anchor is the one door a run id can come *in* through: a
    value that already has the canonical shape is passed back unchanged, because
    re-hashing an inherited run would fork the very episode inheriting exists to
    keep whole, and anything else is hashed like any other anchor. That check is
    here rather than at the call sites because an inherited run reaches a
    database column, the wire, and a client's view identity — so whatever
    length and character set a sender chose for it must stop at one place, and
    this is the place.
    """
    if kind not in RUN_ANCHORS:
        raise ValueError(f"unknown collaboration run anchor: {kind!r}")
    values = [str(part or "").strip() for part in parts]
    if not all(values):
        return ""
    if kind == "inherited" and len(values) == 1 and _RUN_ID.match(values[0]):
        return values[0]
    payload = json.dumps([kind, *values], separators=(",", ":"))
    return f"run-{hashlib.sha256(payload.encode()).hexdigest()[:24]}"


def thread_id(a: Any, b: Any, run: Any = "") -> str:
    """The durable id for one conversation between two agents **in one run**.

    Derived from the **unordered** pair, so joud→badr and badr→joud are one
    thread and a reply lands with what it answers — and from the run, so the
    same two agents collaborating on a different task later are a different
    conversation rather than a continuation of an unrelated one. Hashed rather
    than concatenated so the id is a fixed shape regardless of how long the two
    names are, and so it cannot be mistaken for a routing handle.

    An empty run is the legacy reading and reproduces the original pair-only
    digest **byte for byte**, so rows written before runs existed keep the ids
    they were written with. That legacy payload is the one thing in here that
    may never change.

    The run-scoped payload is JSON rather than another NUL join, so no pair of
    arguments can spell the same bytes as another: NUL-joining ``run``, ``a``
    and ``b`` made ``thread_id("a\\x00b", "c")`` and ``thread_id("b", "c",
    run="a")`` one id. Agent names cannot contain NUL today, so that collision
    was unreachable — and it cost one line to make it unreachable by
    construction instead of by luck.
    """
    left, right = sorted([_norm(a), _norm(b)])
    scope = str(run or "").strip()
    payload = (f"{left}\x00{right}" if not scope
               else json.dumps(["run", scope, left, right], separators=(",", ":")))
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return f"a2a-{digest[:24]}"


def _legacy_run(thread: Any) -> str:
    """The run of a row that names none — its thread, canonically spelled.

    A row recorded before runs existed has no provenance to recover and Hermes
    will not invent any. What it does have is the conversation it is in, and for
    rows of that era the conversation is the only grouping there is: they all
    carry the pair-only digest, so one legacy thread is one pair's whole
    history, and naming that history is the most a reader can honestly say
    about it.

    Anchoring on the row's *send* instead — one legacy card per message — reads
    as the more conservative choice and is not, for two reasons. It reproduces
    the "1 message with 1 agent, four times over" rendering that this identity
    was built to end. And it breaks an exchange in half across the upgrade: a
    reply inherits its request's run, a legacy request's run is blank, so the
    reply stores blank too — same thread, different send, two cards for one
    conversation.

    This is a *read-time* reading of a blank column and never a write. The
    stored value stays blank, which is what keeps "blank means legacy"
    unambiguous and what keeps this migration free of any UPDATE.
    """
    return collaboration_run("legacy", thread)


def _run_of_key(key: str) -> str:
    """The canonical run for a grouping key from `_LEGACY_KEY`-tagged SQL."""
    return _legacy_run(key[len(_LEGACY_KEY):]) if key.startswith(_LEGACY_KEY) else key


@dataclass(frozen=True)
class A2AEvent:
    event_id: str
    thread_id: str
    sender: str
    recipient: str
    body: str
    sent_at: float
    send_id: str
    fanout: int
    attachments: list[dict[str, Any]] = field(default_factory=list)
    #: The collaboration run this exchange belongs to. Empty only on rows
    #: recorded before runs existed.
    run_id: str = ""

    @property
    def run(self) -> str:
        """The run this event belongs to, with the legacy row's answer stated.

        A row written before runs existed answers with its thread, spelled the
        single way `collaboration_run` spells anything. Reading it here rather
        than at each call site is what stops a request recorded before the
        upgrade and the reply written to it after from naming one exchange with
        two strings and opening two cards for it.

        **A thread has exactly one run, and this is why.** A typed thread's id
        is derived from its run, so every typed row in it shares that run. A
        legacy thread's rows are all blank and all answer with that same thread.
        Nothing can put a typed row and a blank row in one thread, because the
        two ids are different digests. So a thread id alone settles which run a
        conversation belongs to, and no reader has to narrow one by the other.
        """
        return self.run_id or _legacy_run(self.thread_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "thread_id": self.thread_id,
            # The canonical episode. Threads group under it; an aggregation
            # card is one run, not a time window. A legacy row answers with its
            # own send rather than with an empty string, so a client never has
            # to know which side of the migration a row came from.
            "run_id": self.run,
            "sender": self.sender,
            "recipient": self.recipient,
            "text": self.body,
            "sent_at": self.sent_at,
            "send_id": self.send_id,
            # How many agents that one send reached. The client renders
            # "Messaged 13 agents" from this without having to count rows.
            "fanout": self.fanout,
            "attachments": self.attachments,
        }


def send_event_id(sender, recipient, send_id):
    """New sends share this identity with their native transport sidecar."""
    return hashlib.sha256(json.dumps([_norm(sender), _norm(recipient), str(send_id)], separators=(",", ":")).encode()).hexdigest()


def reply_send_id(sender, recipient, request_send_id):
    """Stable identity for the reverse result of one admitted native send."""
    request_event = send_event_id(sender, recipient, request_send_id)
    return hashlib.sha256(
        json.dumps(["reply", request_event], separators=(",", ":")).encode()
    ).hexdigest()


def record_send(
    home: Path | str,
    *,
    sender: Any,
    recipients: Sequence[Any],
    body: str,
    attachments: Optional[Iterable[dict[str, Any]]] = None,
    sent_at: Optional[float] = None,
    send_id: Optional[str] = None,
    run_id: Optional[str] = None,
) -> list[A2AEvent]:
    """Record one send, which may reach several agents.

    One `send_id` across all of them — that is the fan-out — one `run_id`
    across all of them — that is the episode — and one pairwise `thread_id`
    each, because a conversation has two ends. Never raises into the caller;
    native dispatch decides whether an empty result is fail-open or a
    pre-admission refusal.

    A supplied send identity reuses its original events on replay. A different
    body, attachments or recipient set under that identity is refused. The run
    is **not** part of that check: the stored run is authoritative on replay,
    because moving an already-recorded exchange into a different episode would
    rewrite history that the owner may already have read.
    """
    sender_name = _norm(sender)
    targets = list(dict.fromkeys(t for t in (_norm(r) for r in recipients or []) if t and t != sender_name))
    if not sender_name or not targets:
        return []

    text = str(body or "")[:MAX_BODY_CHARS]
    refs = [dict(a) for a in (attachments or []) if isinstance(a, dict)]
    refs_json = json.dumps(refs, ensure_ascii=False)
    now = time.time() if sent_at is None else float(sent_at)
    batch = send_id or uuid.uuid4().hex
    fanout = len(targets)
    # Admitted, not trusted. A caller's run reaches here from an agent's message
    # envelope by way of the reply finalizer, and this is the only door into the
    # column: a canonical id passes through untouched, anything else is hashed
    # into one, and a blank stays blank so legacy rows keep the legacy reading.
    run = collaboration_run("inherited", run_id) if str(run_id or "").strip() else ""

    events = [
        A2AEvent(
            event_id=send_event_id(sender_name, target, batch),
            thread_id=thread_id(sender_name, target, run),
            sender=sender_name,
            recipient=target,
            body=text,
            sent_at=now,
            send_id=batch,
            fanout=fanout,
            attachments=refs,
            run_id=run,
        )
        for target in targets
    ]

    try:
        conn = _connect(home)
        try:
            with conn:
                # Serialize replay lookup and insert, including legacy random
                # event IDs, without rewriting history or migrating the schema.
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    """SELECT * FROM a2a_events WHERE sender=? AND send_id=?
                       ORDER BY sent_at, event_id""", (sender_name, batch),
                ).fetchall()
                if rows:
                    existing = {}
                    for row in rows:
                        event = _row(row)
                        if event.body != text or event.attachments != refs:
                            return []
                        existing.setdefault(event.recipient, event)
                    if set(existing) != set(targets):
                        return []
                    return [existing[target] for target in targets]
                conn.executemany(
                    """INSERT OR IGNORE INTO a2a_events(
                           event_id, thread_id, sender, recipient, body,
                           attachments, sent_at, send_id, fanout, run_id
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        (e.event_id, e.thread_id, e.sender, e.recipient, e.body,
                         refs_json, e.sent_at, e.send_id, e.fanout, e.run_id)
                        for e in events
                    ],
                )
        finally:
            conn.close()
    except Exception:  # pragma: no cover - defensive
        return []
    return events


def discard_send(home: Path | str, *, sender: Any, send_id: Any) -> bool:
    """Remove one freshly staged request when every transport refused it."""
    sender_name, batch = _norm(sender), str(send_id or "")
    path = _db_path(home)
    if not sender_name or not batch or not path.exists():
        return False
    try:
        conn = _connect(home)
        try:
            with conn:
                cursor = conn.execute(
                    "DELETE FROM a2a_events WHERE sender=? AND send_id=?",
                    (sender_name, batch),
                )
                return bool(cursor.rowcount)
        finally:
            conn.close()
    except Exception:  # pragma: no cover - defensive
        return False


def _row(row: sqlite3.Row) -> A2AEvent:
    try:
        refs = json.loads(row["attachments"] or "[]")
    except Exception:
        refs = []
    keys = row.keys()
    return A2AEvent(
        event_id=row["event_id"], thread_id=row["thread_id"], sender=row["sender"],
        recipient=row["recipient"], body=row["body"], sent_at=float(row["sent_at"]),
        send_id=row["send_id"], fanout=int(row["fanout"]),
        attachments=refs if isinstance(refs, list) else [],
        # Tolerated rather than assumed: a reader opened against a database an
        # older build wrote must not raise, it must read the legacy run.
        run_id=str(row["run_id"] or "") if "run_id" in keys else "",
    )


def read_thread(home: Path | str, *, thread: str, limit: int = 500,
                profile: Any = None) -> list[A2AEvent]:
    """One conversation, **both directions**, in the order it happened.

    Ordered by `sent_at` and then by `event_id`, so two messages recorded in the
    same instant still have one agreed order rather than an arbitrary one.

    There is deliberately **no run filter here.** A thread already names exactly
    one run — see `A2AEvent.run` — so narrowing a thread by a run can only ever
    return the thread or nothing, and a reader that has the thread id has
    already asked the whole question. Filtering it again in Python over rows the
    cap had truncated is what once made a pair past five hundred events answer
    every later run with nothing at all: the owner tapped a card reading "8
    messages with 4 agents" and was told the two agents had never messaged each
    other. There is no such filter to get wrong now.

    *profile*, when given, is the agent this thread is being read FOR, and a
    thread it may address is still only its own from its retirement onward. A
    legacy thread keys on the pair alone, so a deleted `badr` and a recreated one
    share one thread id with `joud`: the new agent resolves it the moment `joud`
    writes into it, and unfloored, the whole of the old agent's side would open
    behind that first new message. Omitted, this reads the ledger whole — that is
    the Owner addressing a thread id directly in their own audit trail, where the
    answer to "what does this thread hold" is all of it.
    """
    if not _db_path(home).exists() or not str(thread or "").strip():
        return []
    conn = _connect_as(home, profile)
    try:
        rows = conn.execute(
            """SELECT * FROM a2a_events WHERE thread_id = ?
               ORDER BY sent_at ASC, event_id ASC LIMIT ?""",
            (str(thread).strip(), max(1, min(int(limit), 2000))),
        ).fetchall()
    finally:
        conn.close()
    return [_row(r) for r in rows]


def resolve_thread(home: Path | str, *, profile: Any, counterpart: Any, run: Any = "") -> str:
    """The id of one conversation, addressed by who and which run — or "".

    This is the route into a thread that does **not** go through a listing, and
    adding it is what makes the listing safe to page. Run scoping turned
    `threads_for` from a bounded list — one row per pair, so at most the roster
    — into one that grows with runs × pairs, and a client that could only reach
    a thread by finding it in that list would, past the page, render "no
    messages between abu-saud and faisal" over an exchange this table holds in
    full. A page boundary is not evidence of absence. This answers from the
    ledger, so its empty string is.

    Confirmed against the ledger rather than derived and hoped for. The typed
    era and the legacy era spell a thread differently — `H(run, pair)` against
    the pair-only digest — and a client must not be asked to know which era a
    run came from, so both are tried here and only a thread that actually holds
    rows is returned.
    """
    me, other = _norm(profile), _norm(counterpart)
    wanted = str(run or "").strip()
    if not me or not other or not _db_path(home).exists():
        return ""
    # The pair's legacy thread, which is what an unscoped ask means and what a
    # legacy run resolves to. `_legacy_run` is the same reading `A2AEvent.run`
    # gives those rows, so a card's run id and this lookup cannot disagree.
    legacy = thread_id(me, other)
    candidates = []
    if not wanted or _legacy_run(legacy) == wanted:
        candidates.append((legacy, ""))
    if wanted:
        candidates.append((thread_id(me, other, wanted), wanted))
    conn = _connect_as(home, me)
    try:
        for candidate, expected in candidates:
            row = conn.execute(
                """SELECT 1 FROM a2a_events
                    WHERE thread_id = ? AND run_id = ? LIMIT 1""",
                (candidate, expected),
            ).fetchone()
            if row is not None:
                return candidate
    finally:
        conn.close()
    return ""


def _encode_cursor(listing: str, last_at: float, key: str) -> str:
    """An opaque page mark. Keyset, not an offset, so a page cannot skip a row.

    It holds the sort key of the last row handed out and the listing it came
    from. Both halves of the sort key are already on the wire in the row that
    produced them, so nothing new is disclosed, and a stale mark simply resumes
    from a moment that has since acquired newer neighbours rather than
    misreporting a count.

    The listing is named because the two listings sort on different second
    halves — a thread id in one, a run's grouping key in the other. A mark from
    one silently accepted by the other would page from a comparison between two
    unrelated strings, which is a wrong page rather than an obvious error.
    """
    raw = json.dumps([listing, float(last_at), str(key)], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(listing: str, cursor: Any) -> Optional[tuple[float, str]]:
    text = str(cursor or "").strip()
    if not text:
        return None
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        kind, last_at, key = json.loads(raw)
        if kind != listing:
            return None
        return float(last_at), str(key)
    except Exception:
        # An unreadable cursor starts at the beginning. Refusing the call would
        # turn a client's stale page mark into an error dialog over a listing
        # that is perfectly readable from the top.
        return None


#: One end of "every row this agent is in". Two of these unioned replace
#: `sender = ? OR recipient = ?`. A recent SQLite will answer that form with a
#: two-index union of its own accord — but only when its planner elects to, and
#: only while nothing else in the query gives it a reason not to. Stating the
#: two scans is not a hint the planner may decline: it is the query, and it
#: reads `a2a_events_sender` and `a2a_events_recipient` on every version.
#:
#: `record_send` drops a recipient equal to the sender, so no row carries both
#: ends and UNION ALL is the same set without the sort a UNION would pay for.
_MINE_LEG = "SELECT thread_id, sent_at FROM a2a_events WHERE {end} = ?"


def threads_for(
    home: Path | str, *, profile: Any, limit: int = 100, cursor: Any = None
) -> dict[str, Any]:
    """One page of the conversations this agent is part of, newest first.

    The counterpart is whichever end is not this agent, which is the same rule
    the client's collaboration summary uses — stated once, here, rather than
    derived twice. `has_reply` is stated too: a thread is pairwise, so it has at
    most two senders, and `MIN(sender) <> MAX(sender)` is exactly "both ends
    spoke" — an answer no message count can give, and the last thing the client
    was still inferring for itself.

    **Paged, because run scoping made this list unbounded.** Keying a thread on
    the pair alone capped this at the roster: one row per counterpart, and a
    hundred rows was always the whole truth. Keying it on the run means one row
    per run *per* pair, which grows with how much work the team does — 120
    episodes across 8 peers is 960 rows, and behind a 500-row cap with no way to
    ask for the rest, 13 of those 120 runs were reachable. The rest were not
    merely hidden: the client looked for a thread it had a card for, did not
    find it in the page, and rendered "no messages between abu-saud and faisal"
    over an intact exchange. That is a worse failure than the one run scoping
    was fixing, because it asserts history does not exist.

    So: `next_cursor` when more remain, `None` when the caller has seen
    everything. The mark is keyset — the `(last_at, thread_id)` of the last row
    handed out — so a thread that gains a message mid-page cannot slide a row
    onto a page the caller already read, the way an OFFSET would.

    Returns a dict rather than a list. `next_cursor` has to travel with the
    rows: a caller that receives only rows has no way to tell a short page from
    a last page, and guessing that is the whole defect above.

    Two queries, for the same reason `runs_for` uses two: name the page first,
    then count only what is on it. Counting inside the ordering query meant
    every page carried the whole ledger's `body` column through a sort, which at
    50,000 threads is 510-1100 ms per page — and the owner waits that out each
    time a chat opens.
    """
    name = _norm(profile)
    page = max(1, min(int(limit), 500))
    empty: dict[str, Any] = {"threads": [], "next_cursor": None}
    if not _db_path(home).exists() or not name:
        return empty
    mark = _decode_cursor("threads", cursor)
    conn = _connect_as(home, name)
    try:
        # Which threads are on this page. Grouped over every row this agent is
        # in, because a thread's position is its newest event and there is no
        # honest shortcut to that: cutting the scan at the cursor's timestamp
        # hides the newer events of threads already handed out, which then come
        # back on the next page under an older `last_at` — a duplicate on one
        # page and, once the cut window holds nothing but threads already seen,
        # an early stop that loses every thread below it. That is the defect
        # this whole function exists to fix, so the scan stays whole.
        #
        # It reads two columns rather than the row. `body` is the large one and
        # nothing here needs it; leaving it behind is most of the cost.
        having, having_params = "", []
        if mark is not None:
            # Strictly after the last row handed out, in the listing's own sort
            # order. The tie-break on `thread_id` is what makes this exact when
            # several threads share a timestamp.
            having = "HAVING (last_at < ? OR (last_at = ? AND thread_id < ?))"
            having_params = [mark[0], mark[0], mark[1]]
        # One thread more than the page, to learn whether another page exists
        # without a second COUNT over the whole ledger.
        chosen = [r["thread_id"] for r in conn.execute(
            f"""SELECT thread_id, MAX(sent_at) AS last_at FROM (
                    {_MINE_LEG.format(end='sender')}
                    UNION ALL
                    {_MINE_LEG.format(end='recipient')}
                ) GROUP BY thread_id {having}
                  ORDER BY last_at DESC, thread_id DESC LIMIT ?""",
            (name, name, *having_params, page + 1),
        ).fetchall()]
        if not chosen:
            return empty
        # Now the counts and the last message, over the page's threads only —
        # `a2a_events_thread` answers both by seek. Doing this in the ordering
        # query above instead meant dragging every `body` in the agent's ledger
        # through a sort to render a hundred rows. At most 501 bound ids, clear
        # of SQLite's historical 999-variable ceiling.
        #
        # `page` is not re-filtered by sender or recipient: a thread id is
        # derived from the pair, so every row carrying it is between those two
        # agents, and one of them is this profile. `message_count` is therefore
        # the thread's whole total rather than a slice of it.
        marks = ",".join("?" * len(chosen))
        rows = conn.execute(
            f"""WITH page AS (SELECT * FROM a2a_events WHERE thread_id IN ({marks})),
                     agg AS (
                         SELECT thread_id,
                                MAX(sent_at)               AS last_at,
                                COUNT(*)                   AS message_count,
                                MIN(sender) <> MAX(sender) AS has_reply
                           FROM page GROUP BY thread_id
                     ),
                     last AS (
                         SELECT thread_id, body, sender, recipient, run_id,
                                ROW_NUMBER() OVER (
                                    PARTITION BY thread_id
                                    ORDER BY sent_at DESC, event_id DESC
                                ) AS rn
                           FROM page
                     )
                SELECT agg.thread_id, agg.last_at, agg.message_count, agg.has_reply,
                       last.body AS last_text, last.sender AS last_sender,
                       last.recipient AS last_recipient, last.run_id AS last_run
                  FROM agg JOIN last ON last.thread_id = agg.thread_id AND last.rn = 1
                 ORDER BY agg.last_at DESC, agg.thread_id DESC""",
            chosen,
        ).fetchall()
    finally:
        conn.close()

    more = len(rows) > page
    out = []
    for row in rows[:page]:
        counterpart = row["last_recipient"] if row["last_sender"] == name else row["last_sender"]
        out.append({
            "thread_id": row["thread_id"],
            # The episode this conversation belongs to — the same identity
            # `a2a.runs` cards carry and `a2a.thread` resolves against, so the
            # three surfaces name one thing one way. A legacy thread answers
            # with its own `legacy` run rather than with an empty string: a
            # client should never have to know which side of the migration a
            # row came from in order to open it.
            "run_id": str(row["last_run"] or "") or _legacy_run(row["thread_id"]),
            "counterpart": counterpart,
            "message_count": int(row["message_count"]),
            "last_at": float(row["last_at"]),
            "last_text": row["last_text"],
            "last_sender": row["last_sender"],
            "has_reply": bool(row["has_reply"]),
        })
    return {
        "threads": out,
        "next_cursor": (_encode_cursor("threads", out[-1]["last_at"], out[-1]["thread_id"])
                        if more and out else None),
    }


def runs_for(
    home: Path | str, *, profile: Any, limit: int = 50, cursor: Any = None
) -> dict[str, Any]:
    """One page of the collaboration **runs** this agent took part in, newest first.

    One entry here is one card in the owner's transcript. The grouping key is
    the event's own run and nothing else: no clock window, no same-sender rule,
    no text comparison. That is what makes a fan-out to four agents one card
    with four participants, a reply that arrives an hour late land back on the
    card that asked for it, and a second instruction raised a second later open
    a second card.

    Everything is scoped to `profile` — its own counterparts, its own message
    counts — because this answers "what did *this* agent do with the team", and
    the owner reads it inside that agent's chat. The pairwise threads inside a
    run are listed so the card can offer a way into each exchange.

    `has_reply` says whether anything came back, per thread and for the run, and
    `reply_count` says from how many. The client used to infer it as "more
    messages than threads", which is not the same question: two `message_agent`
    calls to one teammate and one to another, with nobody answering, is three
    messages across two threads, and the owner was told an exchange had happened
    when none had. The direction of every event is here; the client should not
    be re-deriving semantics from counts.

    **Paged like `threads_for`, and for the same reason.** A run list grows with
    how much work the team does, so a fixed cap with no cursor would put older
    collaborations out of reach exactly as it did for threads. The cursor is the
    `(last_at, run_id)` of the last card handed out. Opening a card never
    depends on it either way — the transcript projection carries a run's own id
    and `resolve_thread` opens it directly — but a listing that silently ends is
    the shape of the defect this change exists to remove, and there is no reason
    to leave one of the two listings in it.

    **Every number here is the ledger's, not the listing's.** `MAX_LISTING_EVENTS`
    bounds how much is read into memory to say what was *said*; it may not become
    the answer to how much there is. `message_count`, `started_at` and
    `has_reply` are aggregated in SQL over the page's whole key set, so a card
    reports the same totals whether it sat under the ceiling or over it — and
    `next_cursor: null` next to them means the listing really is complete.

    The threads inside a card carry the same eight fields as a `threads_for`
    row, `run_id` included. A thread belongs to exactly one run, so listing it
    twice with two shapes only asked a client to remember which listing it was
    reading.
    """
    name = _norm(profile)
    count = max(1, min(int(limit), 500))
    empty: dict[str, Any] = {"runs": [], "next_cursor": None}
    if not _db_path(home).exists() or not name:
        return empty
    mark = _decode_cursor("runs", cursor)
    conn = _connect_as(home, name)
    try:
        # The newest `count` runs first, then what happened in *those* runs.
        # Name the page before reading it, rather than cutting one scan at N
        # events: a cut scan slices a long run in half and reports a count that
        # is short — a card that says six messages when there were nine is worse
        # than one card fewer.
        #
        # A pre-run row's run is blank, and grouping *by the column* put every
        # one of them in a single entry — which then named the entire legacy
        # ledger to the second query below, tens of thousands of rows built into
        # as many dicts to return fifty cards, on every chat open, for as long as
        # history is blank. Right after the deploy that is all of it. The key
        # splits them by their thread, the way `A2AEvent.run` reads them, and
        # carries that thread forward so the second query can seek on
        # `a2a_events_thread` instead of reading the agent's whole ledger.
        key = (f"CASE WHEN run_id <> '' THEN run_id "
               f"ELSE '{_LEGACY_KEY}' || thread_id END")
        having, having_params = "", []
        if mark is not None:
            having = "HAVING (last_at < ? OR (last_at = ? AND key < ?))"
            having_params = [mark[0], mark[0], mark[1]]
        # One key more than the page, to learn whether another page exists. The
        # tie-break on `key` is what makes the cursor exact when several runs
        # end at the same instant — a fan-out recorded in one batch does.
        chosen = conn.execute(
            f"""SELECT key, MAX(sent_at) AS last_at FROM (
                    SELECT {key} AS key, sent_at FROM a2a_events WHERE sender = ?
                    UNION ALL
                    SELECT {key} AS key, sent_at FROM a2a_events WHERE recipient = ?
                ) GROUP BY key {having}
                  ORDER BY last_at DESC, key DESC LIMIT ?""",
            (name, name, *having_params, count + 1),
        ).fetchall()
        more = len(chosen) > count
        keys = [r["key"] for r in chosen[:count]]
        if not keys:
            return empty
        recorded = [k for k in keys if not k.startswith(_LEGACY_KEY)]
        legacy = [k[len(_LEGACY_KEY):] for k in keys if k.startswith(_LEGACY_KEY)]
        legs: list[str] = []
        tally: list[str] = []
        params: list[Any] = []
        if recorded:
            legs.append(f"""SELECT * FROM a2a_events
                             WHERE run_id IN ({",".join("?" * len(recorded))})
                               AND (sender = ? OR recipient = ?)""")
            tally.append(f"""SELECT run_id AS key, thread_id, sender, recipient, sent_at
                               FROM a2a_events
                              WHERE run_id IN ({",".join("?" * len(recorded))})
                                AND (sender = ? OR recipient = ?)""")
            params += [*recorded, name, name]
        if legacy:
            # The same expression the key was built from, so a row matches its
            # own key and no other. `a2a_events_thread` serves this leg, and
            # naming the thread once keeps both statements inside one bound
            # variable per key: `recorded` and `legacy` are disjoint halves of
            # `keys`, so the two legs together can never ask for more than
            # `limit` + 4. Each statement binds one more of its own — the
            # ceiling below, this agent's name above — so the worst case is
            # 505 either way, at `limit=500` split across both kinds of key.
            # SQLite's historical 999-variable ceiling is clear of both.
            legs.append(f"""SELECT * FROM a2a_events
                             WHERE run_id = ''
                               AND thread_id IN ({",".join("?" * len(legacy))})
                               AND (sender = ? OR recipient = ?)""")
            tally.append(f"""SELECT '{_LEGACY_KEY}' || thread_id AS key, thread_id,
                                    sender, recipient, sent_at
                               FROM a2a_events
                              WHERE run_id = ''
                                AND thread_id IN ({",".join("?" * len(legacy))})
                                AND (sender = ? OR recipient = ?)""")
            params += [*legacy, name, name]
        # Newest first under the ceiling, then back into the order things
        # happened — the aggregation below reads participants in first-contact
        # order, which is the order of the fan-out.
        rows = conn.execute(
            "SELECT * FROM (" + " UNION ALL ".join(legs)
            + " ORDER BY sent_at DESC, event_id DESC LIMIT ?)"
            + " ORDER BY sent_at ASC, event_id ASC",
            (*params, MAX_LISTING_EVENTS),
        ).fetchall()
        # **The ceiling bounds materialisation, not arithmetic.** The scan above
        # keeps a card's *cost* bounded by dropping the oldest events, which is
        # right — but every number on the card was then counted from what
        # survived it, and a dropped event is not a message that did not happen.
        # On a legacy ledger that is silent: legacy keys are roster-bounded, so
        # all of them materialise, none fall out, and the "page comes up short
        # and the cursor picks it up" recovery never fires. Twelve thousand
        # events across fourteen pairs reported ten thousand and said the
        # listing was complete.
        #
        # So the counts come from SQL instead, over the same rows the scan was
        # cut out of. This aggregates; it does not materialise: one output row
        # per (key, thread) — the page's own threads, which the response already
        # carries in the open — and no `body` column in it at all. `COUNT(*)` is
        # therefore the thread's whole total whatever the ceiling did, and
        # `MIN(sender) <> MAX(sender)` is "both ends spoke" over the whole
        # thread rather than over the tail of it.
        totals = conn.execute(
            """SELECT key, thread_id, COUNT(*) AS message_count,
                      MIN(sent_at) AS first_at, MAX(sent_at) AS last_at,
                      MIN(sender) <> MAX(sender) AS has_reply,
                      MIN(CASE WHEN sender = ? THEN recipient ELSE sender END) AS counterpart
                 FROM (""" + " UNION ALL ".join(tally) + """)
                GROUP BY key, thread_id""",
            (name, *params),
        ).fetchall()
        # A thread of a card that *is* on this page, whose every event the
        # ceiling dropped. It is described from `totals` like any other, and
        # needs only the text of its last message — one row, by seek, for the
        # few threads in that state. A key that materialised *nothing* is not
        # enriched: it is not on this page at all, `next_cursor` hands it to the
        # next one, and drawing it here would undo that.
        drawn = {(str(r["run_id"] or "") or _LEGACY_KEY + r["thread_id"]) for r in rows}
        seen = {r["thread_id"] for r in rows}
        missing = [t["thread_id"] for t in totals
                   if t["key"] in drawn and t["thread_id"] not in seen]
        tails: dict[str, sqlite3.Row] = {}
        # Chunked, because the page's thread count is bounded by the roster and
        # not by 999 — 500 runs of fourteen pairs is seven thousand ids.
        for start in range(0, len(missing), 400):
            batch = missing[start:start + 400]
            tails.update({r["thread_id"]: r for r in conn.execute(
                f"""SELECT thread_id, body, sender FROM (
                        SELECT thread_id, body, sender,
                               ROW_NUMBER() OVER (
                                   PARTITION BY thread_id
                                   ORDER BY sent_at DESC, event_id DESC
                               ) AS rn
                          FROM a2a_events WHERE thread_id IN ({",".join("?" * len(batch))})
                    ) WHERE rn = 1""", batch).fetchall()})
    finally:
        conn.close()

    # The keys are what the first query chose; anything the second query reached
    # past them is not this page's, and does not get to displace a card.
    wanted = {_run_of_key(k) for k in keys}

    # What was *said*, and in what order. This pass establishes which cards this
    # page holds, the text of each conversation's last message, and the order
    # the fan-out reached the team in. It counts nothing: the ceiling is allowed
    # to cut it short, so it is not a witness to how much there is.
    runs: dict[str, dict[str, Any]] = {}
    for raw in rows:
        event = _row(raw)
        if event.run not in wanted:
            continue
        counterpart = event.recipient if event.sender == name else event.sender
        run = runs.setdefault(event.run, {
            "run_id": event.run,
            "started_at": event.sent_at,
            "last_at": event.sent_at,
            "message_count": 0,
            "participants": [],
            "_threads": {},
        })
        if counterpart not in run["participants"]:
            # First-contact order, which is the order the fan-out happened in
            # — stable across reads and independent of when anyone replied.
            run["participants"].append(counterpart)
        thread = run["_threads"].setdefault(event.thread_id, {
            "thread_id": event.thread_id,
            "counterpart": counterpart,
            "message_count": 0,
            "last_at": event.sent_at,
            "last_text": event.body,
            "last_sender": event.sender,
            "has_reply": False,
        })
        if event.sent_at >= thread["last_at"]:
            thread.update(last_at=event.sent_at, last_text=event.body, last_sender=event.sender)

    # How much there is, which only the aggregate knows. A card's `message_count`
    # is the thread's whole `COUNT(*)`, its `started_at` the ledger's own
    # earliest, and `has_reply` "both ends spoke" over the whole thread — none of
    # them a tally of what the ceiling happened to leave behind. A thread the
    # ceiling dropped entirely is added back here rather than left off a card
    # that is already being drawn; in first-contact order, like every other.
    started: dict[str, float] = {}
    for fact in sorted(totals, key=lambda f: (f["first_at"], f["thread_id"])):
        run = runs.get(_run_of_key(fact["key"]))
        if run is None:
            continue  # its key drew nothing; the cursor hands it to the next page
        started[run["run_id"]] = min(started.get(run["run_id"], fact["first_at"]),
                                     fact["first_at"])
        thread = run["_threads"].get(fact["thread_id"])
        if thread is None:
            tail = tails.get(fact["thread_id"])
            thread = run["_threads"][fact["thread_id"]] = {
                "thread_id": fact["thread_id"],
                "counterpart": fact["counterpart"],
                "last_text": tail["body"] if tail is not None else "",
                "last_sender": tail["sender"] if tail is not None else "",
            }
            if fact["counterpart"] not in run["participants"]:
                run["participants"].append(fact["counterpart"])
        thread.update(
            message_count=int(fact["message_count"]),
            last_at=float(fact["last_at"]),
            # A thread is pairwise, so both names appearing as a sender *is* the
            # answer to "did anyone reply", and it is an answer no count can give.
            has_reply=bool(fact["has_reply"]),
        )
    for run in runs.values():
        threads = run["_threads"].values()
        run["message_count"] = sum(t["message_count"] for t in threads)
        run["started_at"] = started.get(run["run_id"], run["started_at"])
        run["last_at"] = max(t["last_at"] for t in threads)

    # Back into the order the first query chose, which is the order the cursor
    # advances in — the same `(last_at, key)` sort, so a page ends exactly where
    # the next one resumes.
    of_run = {_run_of_key(k): k for k in keys}
    out = []
    for run in sorted(runs.values(),
                      key=lambda r: (r["last_at"], of_run.get(r["run_id"], "")),
                      reverse=True)[:count]:
        threads = list(run.pop("_threads").values())
        for thread in threads:
            # One shape for a thread wherever it is listed. `a2a.threads` rows
            # carry their run; these carried seven of those eight fields and
            # left the client to remember that the enclosing card supplied the
            # eighth. A thread belongs to exactly one run — see `A2AEvent.run` —
            # so the value is the card's own and there is nothing to reconcile.
            thread["run_id"] = run["run_id"]
        # Same order as `participants`, so the card's faces and the list the
        # card opens name the agents in one order rather than two.
        order = {who: i for i, who in enumerate(run["participants"])}
        threads.sort(key=lambda t: order.get(t["counterpart"], len(order)))
        answered = [t for t in threads if t["has_reply"]]
        out.append({
            **run,
            "agent_count": len(run["participants"]),
            # Whether anything came back at all, and from how many of the agents
            # this run reached — so a card can say "Messaged" or say how much was
            # said without counting rows to guess which.
            "has_reply": bool(answered),
            "reply_count": len(answered),
            "threads": threads,
        })
    # More to come if the first query saw another key — or if `MAX_LISTING_EVENTS`
    # dropped the oldest events and took whole cards off this page with them.
    # Without that second clause a page shortened by the ceiling ended the
    # listing, and the runs it dropped were never handed out at all: the ceiling
    # would have become a quiet cap on history, which is the defect this
    # function was rewritten to remove.
    return {
        "runs": out,
        "next_cursor": (_encode_cursor("runs", out[-1]["last_at"], of_run[out[-1]["run_id"]])
                        if out and (more or len(out) < len(keys)) else None),
    }


def run_for_request(home: Path | str, *, sender: Any, recipient: Any, send_id: Any) -> str:
    """The run of one already-recorded request, by its exact send identity.

    A reply belongs to the run of what it answers. Normally the request's run
    travels with it in the delivered projection and the reply simply carries it
    back — but a request staged by an older build, or one whose projection was
    truncated, has no run to carry. The answer is still knowable without
    guessing: the request is a row in this ledger, addressed by the same
    ``(sender, recipient, send_id)`` triple the reply's identity is derived
    from, and its run is recorded. Reading it back is proof, not inference.

    Returns the empty string when the request is not found or predates runs —
    which places the reply in the same legacy thread as its request rather than
    opening a typed thread containing half an exchange.
    """
    source, target, batch = _norm(sender), _norm(recipient), str(send_id or "").strip()
    if not source or not target or not batch or not _db_path(home).exists():
        return ""
    try:
        conn = _connect(home)
        try:
            row = conn.execute(
                "SELECT run_id FROM a2a_events WHERE event_id = ?",
                (send_event_id(source, target, batch),),
            ).fetchone()
        finally:
            conn.close()
    except Exception:  # pragma: no cover - defensive
        return ""
    return str(row["run_id"] or "") if row is not None else ""
