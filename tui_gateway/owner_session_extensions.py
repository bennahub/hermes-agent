"""Owner session callbacks and durable context helpers."""
from __future__ import annotations
from .method_ctx import bind_module

_PUSH_OBSERVED_EVENTS = frozenset({"approval.request", "message.complete"})
_REPLY_QUOTE_CHARS = 280

def _emit_activity(sid: str, event: str, payload: dict | None = None):
    """Structured ephemeral state, independent of optional tool-progress chrome."""
    try:
        from tui_gateway.activity import record, snapshot
        session = _sessions.get(sid)
        if session is None or session.get("_finalized"):
            return
        if record(session, event, payload):
            status = _session_live_status(sid, session)
            if event in {"message.complete", "error"}:
                status = "idle"
            elif status != "waiting":
                status = "working"  # the callback itself proves active execution
            activity = snapshot(session, status)
            # Not a transcript message or push-notification event. Do not recurse
            # through _emit; one state transition yields one small scoped frame.
            write_json(_event_frame("activity.update", sid, activity))
    except Exception:
        # Optional header state must never cost a tool call or its original event.
        logger.debug("activity_projection_unavailable")


def _consider_push(event: str, sid: str, payload: dict | None) -> None:
    """Let the push observer see every client-facing event.

    Hooked here rather than at the handful of places that produce notifiable
    events, for the same reason `finalize_turn` owns the artifact stamp: this is
    the one path they all already take, so the observer cannot hold a second
    opinion about what happened and cannot be missed by a path someone forgets
    to wire.

    Whether the owner is *watching* is read from the live-transport registry —
    a connected client means the phone is in their hand and a push would buzz
    for something already on screen.

    Never raises: a notification decision must not be able to cost an event its
    delivery to the client.
    """
    if event not in _PUSH_OBSERVED_EVENTS:
        return
    try:
        from gateway import push_notifier
        from hermes_constants import get_hermes_home

        with _live_transports_lock:
            owner_is_watching = bool(_live_transports)
        session = _sessions.get(sid) or {}
        agent = str(session.get("profile") or session.get("agent_name") or "")
        decision = push_notifier.consider(
            event,
            agent=agent,
            payload=payload,
            owner_is_watching=owner_is_watching,
        )
        if not decision.notify:
            return
        text = str((payload or {}).get("text") or "")
        push_notifier.deliver(
            get_hermes_home(),
            decision,
            title=agent or "Hermes",
            body=text,
            endpoint=str(session.get("endpoint") or ""),
            agent=agent,
            message_id=str((payload or {}).get("message_id") or ""),
            event_id=f"{sid}:{event}",
        )
    except Exception:
        logger.debug("push observation failed", exc_info=True)


def _load_inline_diffs() -> bool:
    """Whether tool results may carry a rendered edit diff to the client.

    ``display.inline_diffs`` already gates the classic CLI's inline edit
    diffs; the desktop/TUI backend ignored it and always shipped the rendered
    panel, so an owner who turned it off still saw every code diff in chat.
    Default True — absent key keeps the historical behaviour.
    """
    return bool((_load_cfg().get("display") or {}).get("inline_diffs", True))


def _session_inline_diffs(sid: str) -> bool:
    """Per-session inline-diff visibility, falling back to config.yaml.

    Stored on the session at create/resume like ``show_reasoning`` so a live
    toggle applies without a restart.
    """
    session = _sessions.get(sid)
    if session is not None and "inline_diffs" in session:
        return bool(session.get("inline_diffs"))
    return _load_inline_diffs()


def _on_reasoning_delta(sid: str, text: str) -> None:
    """Emit a reasoning delta unless this session hides reasoning.

    ``display.show_reasoning`` was stored per session but never consulted on
    this path, so Desktop received every "Thought" block regardless of the
    setting. Reasoning is display-only; suppressing it never changes what the
    model receives.
    """
    session = _sessions.get(sid)
    if session is not None and not session.get("show_reasoning", True):
        return
    if session is None and not _load_show_reasoning():
        return
    _emit(
        "reasoning.delta",
        sid,
        {"text": text, **({"verbose": True} if _session_verbose(sid) else {})},
    )


def _submit_internal_prompt(
    sid: str,
    text: str,
    *,
    terminal_callback: Callable[[dict[str, Any]], None] | None = None,
    display_metadata: dict | None = None,
) -> bool:
    """Submit a service-originated turn through the live session owner.

    Unlike a second CLI process, this path shares the owner's in-memory
    transcript. If the session is busy, the prompt and its completion callback
    are queued together and drained after the current turn settles.
    """
    session = _sessions.get(str(sid or ""))
    if not session or session.get("_closing"):
        return False
    transport = session.get("transport")
    from agent.message_projection import native_metadata, projection
    if display_metadata is None:
        display_metadata = native_metadata("runtime", "internal", "control")
    value = projection(display_metadata)
    display_kind = "a2a_message" if value and value["audience"] == "peer" else "hidden"
    with session["history_lock"]:
        if session.get("running"):
            _enqueue_prompt(
                session,
                text,
                transport,
                terminal_callback=terminal_callback,
                display_metadata=display_metadata,
            )
            session["last_active"] = time.time()
            return True
        session["running"] = True
        session["last_active"] = time.time()
    try:
        return bool(
            _run_prompt_submit(
                f"internal-{uuid.uuid4().hex}",
                sid,
                session,
                text,
                terminal_callback=terminal_callback,
                display_metadata=display_metadata,
                display_kind=display_kind,
            )
        )
    except Exception:
        with session["history_lock"]:
            session["running"] = False
        logger.exception("Internal prompt dispatch failed for live session %s", sid)
        return False


def _session_profile_name(session: dict) -> str:
    home = _session_home(session).resolve()
    if home.parent.name == "profiles":
        return home.name
    return "default"


def _find_live_bot_chat(profile: str) -> tuple[str, dict] | None:
    from tools.bot_mode_dm import _session_title
    from tools.bot_mode_probe import BOT_CHAT_TITLE

    wanted = "default" if str(profile).lower() == "hermes" else str(profile)
    for live_sid, session in list(_sessions.items()):
        if session.get("_closing") or _session_profile_name(session) != wanted:
            continue
        agent = session.get("agent")
        if agent is not None and _session_title(agent) == BOT_CHAT_TITLE:
            return live_sid, session
    return None


def _bind_live_bot_dm_dispatcher(agent: Any, source_sid: str) -> None:
    """Give ``message_agent`` an in-process path owned by this live session."""

    def _dispatch(profile: str, message: str, *, display_metadata=None) -> bool:
        return _dispatch_live_bot_dm(source_sid, profile, message, display_metadata=display_metadata)

    setattr(agent, "_message_agent_dispatcher", _dispatch)


def _dispatch_live_bot_dm(source_sid: str, target_profile: str, message: str, *, display_metadata=None) -> bool:
    """Route a local teammate DM through this process's live Bot Chat owner."""
    target = _find_live_bot_chat(target_profile)
    if target is None:
        return False
    target_sid, _target_session = target
    target_handle = "hermes" if target_profile == "default" else target_profile

    def _reply(receipt: dict[str, Any]) -> None:
        status = str(receipt.get("status") or "")
        text = str(receipt.get("text") or receipt.get("error") or "").strip()
        if status not in {"settled", "complete"}:
            text = text or f"Delivery to @{target_handle} failed ({status or 'unknown'})."
        if not text:
            return
        from agent.message_projection import native_metadata, projection
        identity = projection(display_metadata) or {}
        _submit_internal_prompt(
            source_sid,
            f"Message from 🤖 {target_handle} (@{target_handle}): {text}",
            display_metadata=native_metadata("runtime", "internal", "control",
                send_id=identity.get("send_id"), event_id=identity.get("event_id"),
                source_session_id=identity.get("source_session_id")),
        )

    return _submit_internal_prompt(
        target_sid,
        message,
        terminal_callback=_reply,
        display_metadata=display_metadata,
    )


def _resolve_reply_reference(session, raw):
    """Resolve a client's reply reference against the session it belongs to.

    Returns ``(reference, quote)``: the reference to store on the owner's
    message, and the preamble the model is shown so it knows which message is
    being answered. Both are ``None`` when there is nothing to reply to.

    The row id is **verified against this session** rather than trusted. A
    client that names a row from another conversation — or a row that no longer
    exists after a compaction rewrote the session's ids — gets an ordinary
    message rather than a dangling reference or a quote from somewhere else.
    That is also why the excerpt is resolved here and not accepted from the
    client: a caller must not be able to put words in another message's mouth.
    """
    if not isinstance(raw, dict):
        return None, None
    try:
        row_id = int(raw.get("message_id"))
    except (TypeError, ValueError):
        return None, None

    row = None
    try:
        with _session_db(session) as db:
            if db is not None:
                row = db.get_message_for_reply(session["session_key"], row_id)
    except Exception:
        row = None
    if not row:
        # The row is gone — a compaction rewrites every id in a session — or it
        # never belonged here. Send the message as an ordinary one rather than
        # attaching a reference that resolves to nothing.
        return None, None

    content = row.get("content")
    if not isinstance(content, str):
        content = ""
    excerpt = " ".join(content.split())[:_REPLY_QUOTE_CHARS]
    role = str(row.get("role") or "")
    reference = {
        "message_id": row_id,
        "role": role,
        "excerpt": excerpt,
    }
    speaker = "you" if role == "user" else "your"
    quote = (
        f"[Replying to {speaker} earlier message #{row_id}: \"{excerpt}\"]\n\n"
        if excerpt
        else f"[Replying to message #{row_id}]\n\n"
    )
    return reference, quote


def _session_message_rows(session):
    try:
        with _session_db(session) as db:
            if db is None:
                return []
            return list(db.get_messages(session["session_key"]) or [])
    except Exception:
        return []


def _compose_owner_turn_text(session, text, reply_reference):
    """Bind reply/quote/deictic follow-ups before mint and before the model runs."""
    reply_to, reply_quote = _resolve_reply_reference(session, reply_reference)
    if reply_quote:
        return (reply_quote + (text or "")), reply_to, reply_quote
    try:
        from agent.conversation_referents import (
            AmbiguousReferent, followup_quote, resolve_conversation_referent,
        )
        rows = _session_message_rows(session)
        referent = resolve_conversation_referent(rows, text)
        quote = followup_quote(referent)
    except AmbiguousReferent:
        quote = None
    except Exception:
        quote = None
    if quote:
        return quote + (text or ""), reply_to, quote
    return text, reply_to, None


def _session_attachment_context_paths(
    session: dict, prompt: str, cwd: str,
) -> tuple[Path, ...]:
    """Exact file refs inside this profile's persistent native upload namespace.

    Uploads live outside a project's cwd so container mounts can expose them.
    Derive this per turn rather than keeping transient grants that disappear
    on reconnect/retry. Ordinary workspace and folder-reference rules stay as-is.
    """
    from agent.context_references import parse_context_references

    home = _session_home(session).resolve()
    root = home / "attachments"
    try:
        # A redirected staging directory must not grant another profile/root.
        if root.resolve() != root:
            return ()
    except (OSError, RuntimeError):
        return ()
    paths = set()
    for ref in parse_context_references(prompt):
        if ref.kind != "file":
            continue
        try:
            target = Path(ref.target).expanduser()
            if not target.is_absolute():
                target = Path(cwd) / target
            target = target.resolve(strict=True)
            target.relative_to(root)
            if target.is_file():
                paths.add(target)
        except (OSError, RuntimeError, ValueError):
            continue
    return tuple(paths)


def register(server):
    bind_module(globals(),server,skip=("_",))
