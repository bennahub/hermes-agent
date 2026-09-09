"""Display provenance, independent of provider roles and Owner authority.

Only native ingress/lifecycle writers construct this envelope. Consumers read
the durable sidecar; they must never infer permissions from it.
"""
from __future__ import annotations

import json
import re
from contextvars import ContextVar

_query_projection = ContextVar("native_query_projection", default=None)


def stage_query_projection(content, metadata):
    """Private native CLI ingress only; not populated from RPC request metadata."""
    from hermes_constants import get_hermes_home
    from pathlib import Path
    _query_projection.set((content, metadata, str(Path(get_hermes_home()).resolve())) if metadata else None)


def consume_query_projection(content, agent=None):
    pending = _query_projection.get()
    _query_projection.set(None)
    if not pending or pending[0] != content:
        return None
    from hermes_constants import get_hermes_home
    from pathlib import Path
    if str(Path(get_hermes_home()).resolve()) != pending[2]:
        return None
    db_path = getattr(getattr(agent, "_session_db", None), "db_path", None)
    if isinstance(db_path, (str, Path)) and str(Path(db_path).resolve().parent) != pending[2]:
        return None
    return pending[1]

_ORIGINS = frozenset({"owner", "agent", "peer", "runtime"})
_AUDIENCES = frozenset({"owner", "peer", "internal"})
_PURPOSES = frozenset({"message", "result", "decision", "collaboration", "progress", "control"})
_INTERNAL_KINDS = frozenset({"hidden", "control", "internal_notification", "model_switch",
                             "personality_switch", "auto_continue", "async_delegation_complete"})
#: ``run_id`` is the canonical collaboration episode (see
#: ``gateway.a2a_threads``). Listing it here is what makes it *travel*: a
#: request carries it to the recipient in the delivered projection, and
#: ``stamp_final`` copies every identity it finds back onto the reply — so an
#: exchange stays in one episode without either end being told twice.
_IDENTITIES = frozenset({"source_session_id", "source_message_id", "work_id", "send_id",
                         "event_id", "process_id", "delegation_id", "sender", "recipient",
                         "run_id"})


def metadata_dict(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def projection(metadata):
    value = metadata_dict(metadata).get("projection")
    if (not isinstance(value, dict) or type(value.get("version")) is not int
            or value["version"] != 1 or value.get("origin") not in _ORIGINS
            or value.get("audience") not in _AUDIENCES or value.get("purpose") not in _PURPOSES):
        return None
    return value


def native_metadata(origin, audience, purpose, *, metadata=None, **identities):
    """Merge a native envelope without dropping attachments or client UUIDs."""
    value = {"version": 1, "origin": origin, "audience": audience, "purpose": purpose}
    value.update({k: str(v) for k, v in identities.items()
                  if k in _IDENTITIES and isinstance(v, (str, int)) and not isinstance(v, bool) and str(v)})
    if projection({"projection": value}) is None:
        raise ValueError("invalid native display provenance")
    return {**metadata_dict(metadata), "projection": value}


def owner_visible(role, content="", display_kind=None, display_metadata=None):
    """Timeline/preview predicate. Unknown legacy messages remain readable."""
    value = projection(display_metadata)
    if value is not None:
        return value["audience"] == "owner" and value["purpose"] not in {"control", "progress"}
    if display_kind in _INTERNAL_KINDS:
        return False
    if display_kind == "autonomy_owner_notice":
        return role == "assistant"
    # Existing legacy convention, overridden only by native structural typing.
    return not (role == "user" and str(content or "").lstrip().startswith("[System:"))


def owner_attention(role, content="", display_kind=None, display_metadata=None):
    """Shared incoming-reply predicate for canonical unread and push."""
    if role != "assistant" or not owner_visible(role, content, display_kind, display_metadata):
        return False
    if projection(display_metadata) is None and display_kind not in (None, "", "autonomy_owner_notice"):
        return False  # preserve legacy typed-event attention behavior
    if str(content or "").strip() == "[SILENT]":
        return False
    attachments = metadata_dict(display_metadata).get("attachments") or []
    if not isinstance(attachments, list):
        attachments = []
    return bool(str(content or "").strip() or any(
        isinstance(a, dict) and isinstance(a.get("artifact_id"), str) and a["artifact_id"]
        for a in attachments))


def owner_preview(role, content="", display_kind=None, display_metadata=None):
    if projection(display_metadata) is None and display_kind not in (None, "", "autonomy_owner_notice"):
        return False
    return str(content or "").strip() != "[SILENT]" and owner_visible(role, content, display_kind, display_metadata)


_ATTACHMENT_FIELDS = (
    "artifact_id", "item_id", "filename", "mime_type", "size_bytes", "sha256",
    "profile", "session_id", "created_at", "kind",
)


def canonical_attachments(display_metadata=None):
    """Return owner-safe attachment records in their canonical order.

    Filesystem locations are deliberately outside this projection.  A client can
    open an artifact by ``artifact_id``; it must never need a backend path.
    """
    attachments = metadata_dict(display_metadata).get("attachments")
    if not isinstance(attachments, list):
        return []
    result = []
    for item in attachments:
        if (not isinstance(item, dict) or not isinstance(item.get("artifact_id"), str)
                or not item["artifact_id"]):
            continue
        result.append({key: item[key] for key in _ATTACHMENT_FIELDS if item.get(key) is not None})
    return result


def canonical_transcript(value, attachments=None):
    """Validate transcript metadata bound to one canonical audio artifact."""
    value = metadata_dict(value)
    if type(value.get("version")) is not int or value.get("version") != 1:
        return None
    text = value.get("text")
    artifact_id = value.get("audio_artifact_id")
    item_id = value.get("audio_item_id")
    if not isinstance(text, str) or not text.strip():
        return None
    records = canonical_attachments({"attachments": attachments}) if attachments is not None else []
    if attachments is not None:
        artifact_given = isinstance(artifact_id, str) and bool(artifact_id)
        item_given = isinstance(item_id, str) and bool(item_id)
        if not artifact_given and not item_given:
            return None
        record = next((item for item in records if
            str(item.get("mime_type") or "").lower().startswith("audio/")
            and (not artifact_given or item.get("artifact_id") == artifact_id)
            and (not item_given or item.get("item_id") == item_id)), None)
        if record is None:
            return None
        artifact_id = record["artifact_id"]
        item_id = record.get("item_id")
    elif not isinstance(artifact_id, str) or not artifact_id:
        return None
    out = {"version": 1, "text": text.strip(), "audio_artifact_id": artifact_id}
    if isinstance(item_id, str) and item_id:
        out["audio_item_id"] = item_id
    for key in ("source", "provider", "model", "language"):
        if isinstance(value.get(key), str) and value[key].strip():
            out[key] = value[key].strip()
    for key in ("duration_seconds", "created_at"):
        number = value.get(key)
        if isinstance(number, (int, float)) and not isinstance(number, bool) and number >= 0:
            out[key] = number
    return out


_PRIVATE_DISPLAY_VALUE = re.compile(r"^(?:[/~]|[A-Za-z]:[\\/]|file://|@(file|image):)")


def _safe_display_value(value):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if not isinstance(key, str) or "path" in key.lower() or "backend" in key.lower():
                continue
            safe = _safe_display_value(item)
            if safe is not None:
                out[key] = safe
        return out
    if isinstance(value, list):
        return [safe for item in value if (safe := _safe_display_value(item)) is not None]
    if isinstance(value, str) and _PRIVATE_DISPLAY_VALUE.match(value.strip()):
        return None
    return value


def owner_safe_metadata(display_metadata=None):
    """Recursively remove backend/file locations from owner-facing metadata."""
    metadata = metadata_dict(display_metadata)
    out = _safe_display_value(metadata)
    if not isinstance(out, dict):
        return {}
    if "attachments" in metadata:
        out["attachments"] = canonical_attachments(metadata)
    if "transcript" in metadata:
        transcript = canonical_transcript(metadata.get("transcript"), canonical_attachments(metadata))
        if transcript is None:
            out.pop("transcript", None)
        else:
            out["transcript"] = transcript
    reply = metadata.get("reply_to")
    if isinstance(reply, dict):
        message_id = reply.get("message_id")
        out["reply_to"] = ({"message_id": message_id}
                           if isinstance(message_id, int) and not isinstance(message_id, bool) and message_id > 0
                           else {})
    return out


def collaboration_summary(display_metadata=None):
    metadata = metadata_dict(display_metadata)
    native = projection(metadata)
    if not native or native["audience"] != "peer" or native["purpose"] != "collaboration":
        return None
    declared = metadata.get("collaboration")
    declared = declared if isinstance(declared, dict) else {}
    key = native.get("process_id") or native.get("event_id") or native.get("send_id")
    if not key:
        return None
    def count(name):
        value = declared.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 1
    from gateway.a2a_threads import _legacy_run, collaboration_run, thread_id
    # Admitted rather than read. A projection's ``run_id`` is written by
    # `stamp_final`, which admits it, so every value reaching here is already
    # canonical — but this card's ``run_id`` and ``thread_id`` are what the
    # client opens a conversation *by*, and a projection is a stored blob that
    # an older build or a hand-edited row could have put anything into. Passing
    # it through the one door costs nothing when it is canonical (it is returned
    # unchanged) and makes the guarantee structural instead of a property of who
    # happened to write the row.
    run = collaboration_run("inherited", native.get("run_id"))
    sender, recipient = native.get("sender"), native.get("recipient")
    thread = ""
    if isinstance(sender, str) and sender and isinstance(recipient, str) and recipient:
        thread = thread_id(sender, recipient, run)
        # A message delivered before runs existed answers with its thread's own
        # `legacy` run, which is the same value `a2a.runs` and `a2a.threads`
        # give those rows. One identity for one thing, so a client is never
        # handed two spellings and asked to reconcile them.
        run = run or _legacy_run(thread)
    return {
        "key": key,
        # The canonical episode and the canonical conversation inside it, so a
        # card that aggregates several exchanges and the thread a client opens
        # from it are named by the *same* identity rather than two derivations
        # that can disagree.
        "run_id": run,
        "thread_id": thread,
        "message_count": count("message_count"),
        "agent_count": count("agent_count"),
    }


def compact_owner_collection(rows):
    """Collapse each attributed collaboration event/run into one body-free card."""
    output, positions, durable = [], {}, set()
    for row in rows:
        summary = collaboration_summary(row.get("display_metadata")) if isinstance(row, dict) else None
        if summary is None:
            if isinstance(row, dict):
                row_id = row.get("_row_id", row.get("row_id", row.get("id")))
                session_id = row.get("session_id")
                if isinstance(row_id, int) and not isinstance(row_id, bool) and row_id > 0:
                    identity = (session_id if isinstance(session_id, str) else "", row_id)
                    if identity in durable:
                        continue
                    durable.add(identity)
            output.append(row)
            continue
        key = summary["key"]
        if key in positions:
            existing = output[positions[key]]["collaboration_summary"]
            existing["message_count"] = max(existing["message_count"], summary["message_count"])
            existing["agent_count"] = max(existing["agent_count"], summary["agent_count"])
            continue
        projected = dict(row)
        if "content" in projected:
            projected["content"] = ""
        if "text" in projected:
            projected["text"] = ""
        projected.pop("display_content", None)
        safe_metadata = owner_safe_metadata(projected.get("display_metadata"))
        projected["display_metadata"] = {
            key: safe_metadata[key] for key in ("projection", "collaboration")
            if isinstance(safe_metadata.get(key), dict)
        }
        projected["collaboration_summary"] = summary
        positions[key] = len(output)
        output.append(projected)
    return output


def owner_semantics(row):
    """Canonical Apple-client semantics for one stored Hermes message.

    This is additive projection data.  Durable role/content remain untouched and
    older clients may ignore it, while every native client can share the same
    visibility, unread, notification, attachment and correlation truth.
    """
    role = row.get("role")
    content = row.get("content", row.get("text", ""))
    kind = row.get("display_kind")
    metadata = metadata_dict(row.get("display_metadata"))
    native = projection(metadata)
    visible = timeline_visible(role, content, kind, metadata)
    collaboration = bool(native and native["audience"] == "peer" and native["purpose"] == "collaboration")
    # Legacy rows have no trustworthy authorship provenance.  Preserve that as
    # unknown instead of turning a role or a text shape into attribution.
    authored = (
        native["origin"] == "owner" and native["audience"] == "owner"
        if native is not None else None
    )
    attention = owner_attention(role, content, kind, metadata)
    if not visible:
        semantic_kind = "hidden"
    elif collaboration:
        semantic_kind = "collaboration"
    elif authored is True:
        semantic_kind = "owner_message"
    elif native is None:
        semantic_kind = "legacy_message"
    else:
        semantic_kind = "agent_message"

    identity = {}
    row_id = row.get("_row_id", row.get("row_id", row.get("id")))
    if isinstance(row_id, int) and not isinstance(row_id, bool) and row_id > 0:
        identity["durable_row_id"] = row_id
    for source, target in (("session_id", "session_id"), ("root_session_id", "root_session_id")):
        if isinstance(row.get(source), str) and row[source]:
            identity[target] = row[source]
    client_ids = metadata.get("client_message_ids")
    if isinstance(client_ids, list):
        client_ids = list(dict.fromkeys(value for value in client_ids if isinstance(value, str) and value))
        if client_ids:
            identity["client_message_ids"] = client_ids
    if native is not None:
        for key in _IDENTITIES:
            if isinstance(native.get(key), str) and native[key]:
                identity[key] = native[key]
    reply = metadata.get("reply_to")
    if isinstance(reply, dict):
        reply_id = reply.get("message_id")
        if isinstance(reply_id, int) and not isinstance(reply_id, bool) and reply_id > 0:
            identity["reply_to"] = {"message_id": reply_id}

    attachments = canonical_attachments(metadata)
    transcript = canonical_transcript(metadata.get("transcript"), attachments)
    result = {
        "semantic_kind": semantic_kind,
        "owner_authored": authored,
        "owner_notification_eligible": attention,
        "owner_unread_eligible": attention,
        "message_identity": identity,
        "attachments": attachments,
    }
    if transcript is not None:
        result["transcript"] = transcript
    return result


# Exactly the fields of ``agent.connection_health.ConnectionBlock`` — a strict allow-list, so a row
# whose sidecar grew an unexpected key cannot smuggle it onto the wire under this name. A test pins
# this tuple against the dataclass so the two cannot drift.
_CONNECTION_FAULT_FIELDS = (
    "provider", "provider_label", "connection_id", "scope", "reason_code",
    "owner_action", "retryable", "provider_status", "message",
)


def connection_fault(display_metadata=None):
    """The structural Connection descriptor a failed turn's assistant row carries, or None.

    The failed turn's descriptor is durable in ``display_metadata['connection']``
    (``agent.conversation_loop._append_connection_failure_row``); both history projections
    re-publish it from here as a top-level ``connection`` so a client reads ONE key name whether the
    turn arrives on a live ``message.complete`` frame, in ``session.resume``'s inflight snapshot, or
    from cold history. ``reason_code`` is required: it is what a client localizes from, so a sidecar
    without one is not a renderable fault.
    """
    value = metadata_dict(display_metadata).get("connection")
    if not isinstance(value, dict) or not value.get("reason_code"):
        return None
    return {key: value[key] for key in _CONNECTION_FAULT_FIELDS if value.get(key) is not None}


def display_row(row):
    """Additive REST projection; physical content stays intact for export."""
    out = dict(row)
    if row.get("display_metadata"):
        out["display_metadata"] = owner_safe_metadata(row.get("display_metadata"))
    value = projection(row.get("display_metadata"))
    if not timeline_visible(row.get("role"), row.get("content"), row.get("display_kind"), row.get("display_metadata")):
        out["display_kind"] = "hidden"
    elif value is not None and value["origin"] == "owner":
        # Consumers with envelope support bypass legacy text sniffing.
        out["display_metadata"] = owner_safe_metadata(row.get("display_metadata"))
    # A failed-Connection turn renders its reconnect card from REST history too — same key as the
    # live frame, so the client normalizer needs one branch, not one per transport.
    if (fault := connection_fault(row.get("display_metadata"))) is not None:
        out["connection"] = fault
    return out


def timeline_visible(role, content="", display_kind=None, display_metadata=None):
    value = projection(display_metadata)
    if value is not None and value["purpose"] == "collaboration" and value["audience"] == "peer":
        return True  # attributed collaboration, never a direct Owner reply
    return owner_visible(role, content, display_kind, display_metadata)


def process_collaboration_identity(db, session_id, process_id):
    """Join only a native message_agent ACK in this exact source lineage.

    An ACK supplies attribution, never successful completion or work proof.
    Legacy/ambiguous ACKs stay unjoined rather than being guessed from prose.
    """
    if not db or not session_id or not process_id:
        return {}
    lineage = db.get_compression_chain(session_id)
    if not lineage:
        return {}
    with db._lock:
        rows = db._conn.execute(
            "SELECT content FROM messages WHERE role='tool' AND tool_name='message_agent'"
            " AND session_id IN (" + ','.join('?' for _ in lineage) + ")"
            " AND json_valid(content) AND json_extract(content, '$.process_id')=?",
            (*lineage, process_id)).fetchall()
    identities = []
    for row in rows:
        ack = metadata_dict(row[0])
        value = projection(ack.get('display_metadata'))
        if (ack.get('status') != 'sent' or not value or value.get('source_session_id') not in lineage
                or not value.get('send_id') or not value.get('event_id')):
            continue
        # ``run_id`` rides along so the woken parent knows which collaboration
        # it is being woken *for*, without re-deriving it from anything.
        item = {k: value[k] for k in ('send_id', 'event_id', 'sender', 'recipient', 'run_id')
                if k in value}
        if item not in identities:
            identities.append(item)
    return identities[0] if len(identities) == 1 else {}


def stamp_final(agent, messages):
    """Type only this native turn's final; never rewrite past context or content."""
    if not messages or messages[-1].get("role") != "assistant" or messages[-1].get("tool_calls"):
        return
    current = getattr(agent, "_persist_user_message_idx", None)
    if isinstance(current, int) and len(messages) - 1 <= current:
        return
    row = messages[-1]
    if projection(row.get("display_metadata")) is not None:
        return  # final already typed at the native pre-flush boundary
    incoming = getattr(agent, "_turn_display_projection", None)
    work_id = getattr(agent, "_owner_continuity_work_id", None)
    if not isinstance(incoming, dict) and not (isinstance(work_id, str) and work_id):
        return
    audience, purpose, ids = "owner", "message", {}
    if isinstance(incoming, dict) and incoming.get("origin") == "peer":
        audience, purpose = "peer", "collaboration"
        ids = {k: incoming[k] for k in _IDENTITIES if k in incoming}
        sender, recipient, request_send = (
            incoming.get("recipient"), incoming.get("sender"), incoming.get("send_id")
        )
        if sender and recipient and request_send:
            from gateway.a2a_threads import collaboration_run, reply_send_id, send_event_id
            reply_send = reply_send_id(recipient, sender, request_send)
            ids.update(
                sender=sender,
                recipient=recipient,
                send_id=reply_send,
                event_id=send_event_id(sender, recipient, reply_send),
            )
            # A reply belongs to the run that asked for it, however long it took
            # to arrive — inherited, never re-derived from this turn, because
            # re-deriving would put a late reply on a card of its own. Admitted
            # through the one function that mints runs, so an envelope value is
            # shape-checked before it can reach the column.
            ids.pop("run_id", None)
            inherited = str(incoming.get("run_id") or "").strip()
            if not inherited:
                # The request carried no run: it was staged by a build that
                # predates them. Its episode is still recorded, addressed by
                # the same identity this reply is derived from — read it back
                # rather than opening a fresh thread holding half an exchange.
                inherited = _request_run(agent, requester=recipient, target=sender,
                                         send_id=request_send)
            admitted = collaboration_run("inherited", inherited) if inherited else ""
            if admitted:
                ids["run_id"] = admitted
        else:
            # The audience remains peer-scoped, but incomplete legacy native
            # provenance must not be relabelled as a valid durable event — and
            # a run that cannot be tied to a request is not provenance either.
            for key in ("sender", "recipient", "send_id", "event_id", "run_id"):
                ids.pop(key, None)
    if isinstance(work_id, str) and work_id:
        from agent.autonomy import store
        work = store.get_work(work_id)
        if work:
            source = work["refs"].get("owner_request") or {}
            ids.update(work_id=work_id, source_session_id=source.get("session_id"), source_message_id=source.get("message_id"))
            pending = work["refs"].get("pending_owner_result")
            if pending:
                row["display_metadata"] = {**(row.get("display_metadata") or {}),
                    "owner_work_id": work_id, "owner_result_id": pending.get("id")}
                audience, purpose = "owner", "decision" if pending["state"] == "needs_owner" else "result"
            elif work["state"] == "waiting":
                audience, purpose = "internal", "progress"
    row["display_metadata"] = native_metadata("agent", audience, purpose, metadata=row.get("display_metadata"), **ids)
    if audience == "internal":
        row["display_kind"] = "hidden"
    elif audience == "peer":
        row["display_kind"] = "a2a_message"


def record_peer_final(agent, messages):
    """Index one successful native peer result after artifact finalization.

    Both live and off-process ``message_agent`` deliveries run through this
    finalizer before their receipt can wake the parent.  Recording here keeps
    the pairwise thread complete without teaching either transport a second
    settlement contract.  Replay is idempotent through ``record_send``'s
    deterministic reply identity and mismatch refusal.
    """
    if not isinstance(messages, list):
        return []
    row = next((item for item in reversed(messages)
                if isinstance(item, dict) and item.get("role") == "assistant"), None)
    if row is None:
        return []
    value = projection(row.get("display_metadata"))
    if not value or value.get("origin") != "agent" or value.get("audience") != "peer":
        return []
    sender, recipient, send_id, event_id = (
        value.get("sender"), value.get("recipient"), value.get("send_id"), value.get("event_id")
    )
    if not all(isinstance(v, str) and v for v in (sender, recipient, send_id, event_id)):
        return []
    from gateway.a2a_threads import record_send, send_event_id
    if event_id != send_event_id(sender, recipient, send_id):
        return []
    from agent.message_content import flatten_message_text
    body = flatten_message_text(row.get("content")).strip()
    if not body or body == "[SILENT]":
        return []
    metadata = metadata_dict(row.get("display_metadata"))
    attachments = metadata.get("attachments")
    if not isinstance(attachments, list):
        attachments = []
    db_path = getattr(getattr(agent, "_session_db", None), "db_path", None)
    if not db_path:
        return []
    from pathlib import Path
    return record_send(
        Path(db_path).resolve().parent,
        sender=sender,
        recipients=[recipient],
        body=body,
        attachments=attachments,
        send_id=send_id,
        run_id=value.get("run_id"),
    )


def _request_run(agent, *, requester, target, send_id):
    """The run of the request this turn is answering, read from the ledger.

    ``requester`` sent, ``target`` received — the original direction, which is
    the reverse of the reply being stamped. Never raises: a run that cannot be
    read leaves the reply legacy-scoped with its request, which is where it
    belongs, rather than failing the turn.
    """
    try:
        from pathlib import Path
        from gateway.a2a_threads import run_for_request
        db_path = getattr(getattr(agent, "_session_db", None), "db_path", None)
        if not db_path:
            return ""
        return run_for_request(Path(db_path).resolve().parent,
                               sender=requester, recipient=target, send_id=send_id)
    except Exception:  # pragma: no cover - defensive
        return ""
