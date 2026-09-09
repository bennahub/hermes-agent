"""Bot Mode agent-to-agent DM tool — ``message_agent``.

Lets a Bot Mode agent message a teammate (a profile on this install, an agent on
a registered peer gateway, or one on another Desktop-connected machine): the
target is validated against the live roster, the attribution prefix is applied
server-side, and the reply arrives later via the background-process completion
notification (fire-and-forget). Containment: the schema is injected ONLY into a
bot's canonical "Bot Chat" session on a Bot-Mode-managed install (same gate as
``tools/bot_mode_probe.py``; never in the registry or any toolset), and dispatch
re-checks that gate so a forged call returns a structured error. Transports:
local → ``hermes -p <name> chat --in ~ -c "Bot Chat" --create-if-missing -Q
--query-file <tmp>``; peer → ``hermes peer dm <peer>[/<name>] < <tmp>``; both via
``terminal_tool(background=True, notify_on_complete=True)``.
"""

from __future__ import annotations

import contextlib
import json
import hashlib
import logging
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Optional

# Top-level imports stay stdlib-only: this module also runs directly as the background
# delivery runner (``python bot_mode_dm.py --run-delivery …``); Hermes helpers import lazily.

logger = logging.getLogger(__name__)

MESSAGE_AGENT_TOOL_NAME = "message_agent"

# Message body cap — generous for real work, small enough that a runaway paste can't
# turn one DM into a context bomb on the recipient.
MESSAGE_MAX_CHARS = 16000
# A runner owns and removes each DM file; this bounds residual plaintext lifetime if
# the machine dies between spawn ack and the runner's finally.
_DM_DIR_NAME = "hermes-dm"
_DM_STALE_SECONDS = 24 * 60 * 60

# '<peer>/<agent>' — peer names are lowercase (``hermes peer`` normalizes them).
_PEER_TARGET_RE = re.compile(r"^([a-z0-9][a-z0-9_-]{0,63})/([a-zA-Z0-9][a-zA-Z0-9_-]{0,63})$")
# Same shape as ``tools.bot_relay._HANDLE_RE`` (kept local: see import note above).
_LOCAL_TARGET_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


def _default_home() -> str:
    return os.getenv("HERMES_HOME") or os.path.expanduser("~/.hermes")


def message_agent_tool_schema() -> dict:
    """OpenAI-format schema for ``message_agent`` (injected, not registered)."""
    return {
        "type": "function",
        "function": {
            "name": MESSAGE_AGENT_TOOL_NAME,
            "description": (
                "Send a message to ANOTHER agent (teammate) on this install, or to an "
                "agent on a registered peer gateway. This is FIRE-AND-FORGET and "
                "asynchronous, like texting: it validates the target against the live "
                "roster, delivers your message into that agent's own Bot Chat with your "
                "attribution automatically prefixed, and returns immediately with a "
                "delivery acknowledgement. It does NOT return their reply and you must "
                "not wait or poll for one — send it, finish your turn, and the reply "
                "arrives later as a background-process completion notification that "
                "wakes you. COMPOSE the message yourself: write what YOU want to say to "
                "that agent (lead with the point; include the concrete ask or result). "
                "Never paste the user's words verbatim — paraphrase the actionable "
                "substance, and keep private 1:1 chat content private. Message one "
                "clearly relevant teammate when it genuinely helps the user's goal; "
                "don't fan out to several agents unless the user explicitly asked. "
                "Use the teammate roster in your system prompt (names + roles) to pick "
                "the right recipient; targets: a teammate name (e.g. 'researcher'), "
                "'<peer>/<agent>' for an agent on a registered peer gateway "
                "(e.g. 'spark/researcher', or just '<peer>' for the peer's main agent), "
                "or an agent on another connected machine from your roster (use "
                "'<handle>@<connection>' if the same handle exists on several)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": (
                            "Who to message: a teammate profile name from your roster "
                            "('researcher', 'hermes' for the default agent), or "
                            "'<peer>' / '<peer>/<agent>' for a registered peer gateway."
                        ),
                    },
                    "message": {
                        "type": "string",
                        "description": (
                            "The message YOU composed for that agent (max "
                            f"{MESSAGE_MAX_CHARS} chars). Do not include the "
                            "'Message from …' prefix — it is added automatically."
                        ),
                    },
                    "artifact_ids": {
                        "type": "array", "items": {"type": "string"}, "maxItems": 8,
                        "description": "Explicit published artifact IDs from this conversation to share with a local teammate. Never paths; omitted means no files are shared.",
                    },
                },
                "required": ["target", "message"],
            },
        },
    }


def ensure_message_agent_tool(agent: Any) -> bool:
    """Inject the ``message_agent`` schema into a Bot Chat agent's tool list (once per turn).
    Idempotent and deterministic for the session's life (the gate is stable from the
    first turn), so the tool list is byte-identical across turns — prompt-cache safe. Never raises."""
    try:
        if not getattr(agent, "_bot_mode_protocol", True):
            return False
        tools = getattr(agent, "tools", None)
        if tools and any(
            isinstance(t, dict) and t.get("function", {}).get("name") == MESSAGE_AGENT_TOOL_NAME
            for t in tools
        ):
            return True
        from tools.bot_mode_probe import BOT_CHAT_TITLE, is_bot_mode_managed

        # Managed-install check, NOT section non-emptiness: a SOUL.md carrying the
        # legacy protocol text gets an empty section but must still get the tool.
        if ((not getattr(agent, "_bot_mode_all_sessions", False) and _session_title(agent) != BOT_CHAT_TITLE)
                or not is_bot_mode_managed(_agent_home(agent))):
            return False
        if agent.tools is None:
            agent.tools = []
        agent.tools.append(message_agent_tool_schema())
        valid = getattr(agent, "valid_tool_names", None)
        if isinstance(valid, set):
            valid.add(MESSAGE_AGENT_TOOL_NAME)
        return True
    except Exception:  # pragma: no cover — must never break a turn
        logger.debug("ensure_message_agent_tool failed", exc_info=True)
        return False


def _resolve_local_name(target: str, roster: list[str]) -> Optional[str]:
    """Map a target handle to a profile name ('hermes' → 'default')."""
    want = target.strip().lower()
    if want == "hermes":
        return "default" if "default" in roster else None
    return next((name for name in roster if name.lower() == want), None) if want else None


def _err(message: str, *, roster: list[str] | None = None, peers: list[str] | None = None) -> str:
    from tools.bot_failure_reasons import classify_agent_error

    payload: dict[str, Any] = {"error": message, "reason": classify_agent_error(message)}
    if roster is not None:
        payload["teammates"] = roster
    if peers is not None:
        payload["peers"] = peers
    return json.dumps(payload)


def message_agent_tool(target: str = "", message: str = "", task_id: Optional[str] = None, agent: Any = None,
                       artifact_ids=None) -> str:
    """Deliver ``message`` to ``target``'s Bot Chat. Returns a JSON ack/error.
    ``agent`` is the calling AIAgent — used for the Bot Chat gate and sender identity."""
    home = _agent_home(agent)
    try:
        from tools.bot_mode_probe import (
            BOT_CHAT_TITLE, _handle, _hermes_root, _peers, _profile_name as _self_profile_name, _roster,
            is_bot_mode_managed,
        )
        from tools.bot_relay import BOT_CHAT_TURN_ARGS

        if not getattr(agent, "_bot_mode_all_sessions", False) and _session_title(agent) != BOT_CHAT_TITLE:
            return _err("message_agent is only available in a Bot Mode 'Bot Chat' session. "
                        "This session is not one; do not retry.")
        if not is_bot_mode_managed(home):
            return _err("This install is not Bot-Mode-managed (no bot roster); "
                        "message_agent is unavailable. Do not retry.")
    except Exception as exc:  # pragma: no cover — defensive
        return _err(f"Bot Mode gate check failed: {exc}")

    root, me = _hermes_root(Path(home)), _self_profile_name(Path(home))
    roster = [name for name, _dir in _roster(root)]
    peers = _peers(root)
    teammates = [_handle(n) for n in roster if n != me]

    def _roster_err(msg: str) -> str:
        return _err(msg, roster=teammates, peers=peers)

    body = str(message or "").strip()
    if not body:
        return _err("message is required — compose what you want to say to that agent.")
    if len(body) > MESSAGE_MAX_CHARS:
        return _err(f"message too long ({len(body)} chars > {MESSAGE_MAX_CHARS}). "
                    "Send the essentials; share large content as a file path instead.")

    raw_target = str(target or "").strip().lstrip("@")
    if not raw_target:
        return _roster_err("target is required.")
    content = f"Message from 🤖 {_handle(me)} (@{_handle(me)}): " + body
    delivery = dict(task_id=task_id, agent=agent)
    try:
        from gateway.a2a_artifacts import authorize
        attachments = authorize(agent, artifact_ids)
    except (ValueError, OSError) as exc:
        return _err(f"Artifact sharing refused: {exc}")

    # Peer target: '<peer>/<agent>' or a bare registered peer name.
    peer_match = _PEER_TARGET_RE.match(raw_target)
    if peer_match or raw_target.lower() in peers:
        if attachments:
            return _err("Artifact sharing across installations is not supported by this transport.")
        peer_name, peer_profile = peer_match.groups() if peer_match else (raw_target.lower(), None)
        if peer_name not in peers:
            return _roster_err(f"No registered peer named '{peer_name}'.")
        dm_target = f"{peer_name}/{peer_profile}" if peer_profile else peer_name
        # Pin the registry-owning profile: `hermes peer` resolves bot_peers via the profile-scoped
        # load_config(), while the roster above reads the machine-root config — the CLI must run
        # in that same profile or a secondary-profile bot sees an empty registry.
        return _start_delivery(["hermes", "-p", _self_profile_name(root), "peer", "dm", dm_target], content,
                               f"@{peer_profile or peer_name} on peer '{peer_name}'", stdin_file=True, **delivery)

    # Local teammate.
    is_local_shape = bool(_LOCAL_TARGET_RE.match(raw_target))
    if not is_local_shape and "@" not in raw_target:
        return _roster_err(f"Invalid target: {raw_target!r}.")
    resolved = _resolve_local_name(raw_target, roster) if is_local_shape else None
    if resolved is None or resolved == me:
        if attachments:
            return _err("Artifacts can only be shared with another local teammate.")
        # Unknown locally, or same-name target on ANOTHER connection (this gateway's 'default'
        # messaging the cloud 'default'): every Desktop-connected gateway is reachable via the
        # relay roster, so try that before reporting a resolution failure / self-message.
        relayed = _try_relay_delivery(root, raw_target, content, me, **delivery)
        if relayed is not None:
            return relayed
        if resolved == me:
            return _err("You can't message yourself. Pick a teammate from the roster.")
        return _roster_err(f"No teammate named '{raw_target}' on this install, on a connected "
                           "machine, or on a registered peer. Pick a name from the roster "
                           "(roles are listed in your system prompt).")
    # A Bot Chat already open in this same Desktop process has a live ownership
    # lease. Drive that exact in-memory session instead of spawning a second
    # ``hermes -p`` writer, which the session fence must (and does) reject.
    # The dispatcher returns False when the target is not live here, preserving
    # the proven CLI transport for closed/off-process Bot Chats.
    from agent.message_projection import native_metadata
    from gateway.a2a_threads import send_event_id
    send_id = uuid.uuid4().hex
    # Which collaboration this send belongs to, decided here because this is
    # the one place that can see the sending turn's own provenance. It travels
    # in the projection below, so the recipient answers into the same episode
    # instead of into "everything these two have ever said".
    run_id = resolve_collaboration_run(agent, root=root, sender=me, send_id=send_id)
    # Capture the logical send time before either transport can execute the
    # recipient. The request is staged in the durable ledger immediately before
    # dispatch, so even a synchronous child callback can open the complete
    # request/reply exchange. If every transport refuses admission, the exact
    # fresh send identity is removed below.
    sent_at = time.time()
    metadata = native_metadata("peer", "peer", "collaboration", sender=me, recipient=resolved,
        send_id=send_id, event_id=send_event_id(me, resolved, send_id), run_id=run_id,
        source_session_id=getattr(agent, "session_id", None))
    from agent.execution_scope import capture_binding, derive_child

    parent_scope = capture_binding()
    if parent_scope is not None:
        metadata["execution_scope"] = derive_child(
            parent_scope, "a2a:" + send_event_id(me, resolved, send_id), content
        )
    if attachments:
        metadata["attachments"] = attachments
    live_dispatch = getattr(agent, "_message_agent_dispatcher", None)
    request_recorded = False
    if callable(live_dispatch):
        try:
            import inspect
            supports_metadata = "display_metadata" in inspect.signature(live_dispatch).parameters
            if supports_metadata:
                request_recorded = bool(_record_a2a(
                    root, sender=me, recipients=[resolved], body=body, send_id=send_id,
                    attachments=attachments, sent_at=sent_at, run_id=run_id,
                ))
                if not request_recorded:
                    return _err("The collaboration request could not be recorded; nothing was sent.")
                accepted = live_dispatch(resolved, content, display_metadata=metadata)
            else:
                accepted = False
            if accepted:
                label = f"@{_handle(resolved)}"
                return json.dumps(
                    {
                        "status": "sent",
                        "to": label,
                        "transport": "live_session",
                        "detail": (
                            f"Message dispatched to {label}. This is asynchronous — do NOT "
                            "wait or poll. Finish your turn now; the reply will arrive as a "
                            "new agent message."
                        ),
                        "sent_at": int(time.time()),
                        "display_metadata": metadata,
                    }
                )
        except Exception:
            logger.debug("live Bot Chat delivery unavailable; falling back", exc_info=True)

    if not request_recorded:
        request_recorded = bool(_record_a2a(
            root, sender=me, recipients=[resolved], body=body, send_id=send_id,
            attachments=attachments, sent_at=sent_at, run_id=run_id,
        ))
        if not request_recorded:
            return _err("The collaboration request could not be recorded; nothing was sent.")
    result = _start_delivery(["hermes", "-p", resolved, *BOT_CHAT_TURN_ARGS], content, f"@{_handle(resolved)}",
                           stdin_file=False, display_metadata=metadata, projection_root=str(root.resolve()), **delivery)
    if json.loads(result).get("status") == "sent":
        result = json.dumps({**json.loads(result), "display_metadata": metadata})
    else:
        _discard_a2a(root, sender=me, send_id=send_id)
    return result


def _try_relay_delivery(root: Path, raw_target: str, content: str, me: str, *,
                        task_id: Optional[str], agent: Any) -> Optional[str]:
    """Cross-connection delivery via the Desktop relay; None when the target doesn't
    resolve against the relay roster. The envelope is queued on disk for the Desktop
    to drain; a background waiter is spawned immediately so the relayed reply wakes
    the sender through the standard completion-notification path."""
    try:
        from tools.bot_mode_probe import _handle
        from tools.bot_relay import (
            EnvelopeRefusedError, enqueue_envelope, read_remote_roster, resolve_remote_target, waiter_command,
        )

        roster = read_remote_roster(root)
        match = resolve_remote_target(raw_target, roster) if roster else None
        if match is None:
            return None
        if match == "ambiguous":
            want = raw_target.strip().lstrip("@").lower()
            forms = ", ".join(f"{r['handle']}@{r['connection_id']}" for r in roster if r["handle"].lower() == want)
            return _err(f"'{raw_target}' exists on several connected machines — disambiguate with one of: {forms}.")
        try:
            envelope = enqueue_envelope(root, target=match, message=content, sender_profile=me, sender_handle=_handle(me))
        except EnvelopeRefusedError as exc:
            # Fail fast: target definitively offline — nothing was queued.
            # Structured refusal so the agent can distinguish it from a resolution error ('runtime_offline'
            # per the #93091 reason enum).
            return json.dumps({"error": str(exc), "reason": exc.reason})
        label = f"@{match['handle']} on {match['connection_label'] or match['connection_id']}"
        return _spawn_delivery(waiter_command(root, envelope), label, task_id=task_id, agent=agent)
    except Exception:
        logger.debug("relay delivery attempt failed", exc_info=True)
        return None


def _dm_dir() -> Path:
    uid_getter = getattr(os, "getuid", None)
    uid = uid_getter() if callable(uid_getter) else None
    path = Path(tempfile.gettempdir()) / (f"{_DM_DIR_NAME}-{uid}" if uid is not None else _DM_DIR_NAME)
    path.mkdir(mode=0o700, exist_ok=True)
    # Shared POSIX temp roots need a per-user directory. Fail closed if an
    # attacker pre-created the expected path or replaced it with a symlink.
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise PermissionError(f"DM temp path is not a directory: {path}")
    if uid is not None and info.st_uid != uid:
        raise PermissionError(f"DM temp directory is owned by another user: {path}")
    if stat.S_IMODE(info.st_mode) != 0o700:
        path.chmod(0o700)
    return path


def cleanup_bot_dm_cache(max_age_hours: float = _DM_STALE_SECONDS / 3600, *, now: float | None = None) -> int:
    """Delete orphaned DM payload files older than *max_age_hours*; returns count.
    Same contract as the other ``cleanup_*_cache`` helpers (hourly gateway housekeeping);
    legacy temp-root locations from versions predating the dedicated directory are swept too."""
    cutoff = (time.time() if now is None else now) - max_age_hours * 3600
    temp_root = Path(tempfile.gettempdir())
    locations = [(temp_root, "hermes-dm-*.txt"), (temp_root, "hermes-relay-dm-*.txt")]
    with contextlib.suppress(OSError):
        locations.append((_dm_dir(), "*.txt"))
        locations.append((_dm_dir(), "*.projection.json"))
    from tools.bot_relay import unlink_files_older_than

    return sum(unlink_files_older_than(d, pattern, cutoff) for d, pattern in locations)


def _unlink_dm_file(path: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(path)
    with contextlib.suppress(OSError):
        os.unlink(path + ".projection.json")


def _write_dm_file(content: str, *, display_metadata=None, projection_root=None) -> str:
    """The message rides a temp file — never inline shell text."""
    cleanup_bot_dm_cache()
    fd, path = tempfile.mkstemp(prefix="dm-", suffix=".txt", dir=_dm_dir(), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        if display_metadata is not None:
            sidecar = {"root": projection_root, "sha256": hashlib.sha256(content.encode()).hexdigest(), "metadata": display_metadata}
            side_fd = os.open(path + ".projection.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(side_fd, "w", encoding="utf-8") as stream:
                json.dump(sidecar, stream)
    except BaseException:
        # If fdopen itself failed the raw descriptor is still ours; closing twice is harmless.
        with contextlib.suppress(OSError):
            os.close(fd)
        _unlink_dm_file(path)
        raise
    return path


def read_dm_projection(path, content):
    """Read only a native private DM sidecar bound to exact body/root/recipient.

    No token, env override, historical prose matching, or Owner authority is
    accepted here. A missing/invalid sidecar preserves legacy delivery.
    """
    try:
        candidate = Path(path)
        directory = _dm_dir().resolve()
        if candidate.parent != directory or candidate.resolve() != candidate or not candidate.name.startswith("dm-"):
            return None
        sidecar = Path(str(candidate) + ".projection.json")
        fd = os.open(sidecar, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            uid = getattr(os, "getuid", lambda: info.st_uid)()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 32768:
                return None
            data = json.load(stream)
        from hermes_constants import get_hermes_home
        from tools.bot_mode_probe import _hermes_root, _profile_name
        from agent.message_projection import projection, native_metadata
        home = Path(get_hermes_home()).resolve()
        value = projection(data.get("metadata"))
        if (not value or value["origin"] != "peer" or value["audience"] != "peer" or value["purpose"] != "collaboration"
                or value.get("recipient") != _profile_name(home)
                or data.get("root") != str(_hermes_root(home).resolve())
                or data.get("sha256") != hashlib.sha256(content.encode()).hexdigest()):
            return None
        from gateway.a2a_threads import send_event_id
        if not value.get("send_id") or value.get("event_id") != send_event_id(value.get("sender"), value["recipient"], value["send_id"]):
            return None
        metadata = native_metadata("peer", "peer", "collaboration", **{k: v for k, v in value.items() if k not in {"version", "origin", "audience", "purpose"}})
        if data["metadata"].get("attachments"):
            metadata["attachments"] = data["metadata"]["attachments"]
        scope = data["metadata"].get("execution_scope")
        if isinstance(scope, dict) and all(isinstance(scope.get(key), str) for key in ("db_path", "scope_id", "assignment_id")):
            # Transport only. The turn binder must validate the durable record
            # against this native event identity and exact assigned message.
            metadata["execution_scope"] = {key: scope[key] for key in ("db_path", "scope_id", "assignment_id")}
        return metadata
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _delivery_lock(argv: list[str], *, stdin_file: bool):
    """Per-profile turn lock for a LOCAL teammate delivery: local and relay deliveries
    into one profile both run a Bot Chat turn here, so the turn window is serialized on
    ``tools.bot_relay``'s cross-process lock. Peer transports (stdin mode) are locked
    on the remote gateway by its own deliver path.

    See #93091.
    """
    # Match the CLI element by basename: argv[0] may be an absolute venv path
    # (service contexts lack PATH) and carries .exe on Windows; split on both separators.
    # Split on both separators so the shape matches regardless of which platform built the argv. See #93590.
    cli = (argv[0] if argv else "").rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    if stdin_file or len(argv) < 3 or cli not in ("hermes", "hermes.exe") or argv[1] != "-p":
        return contextlib.nullcontext()
    from tools.bot_mode_probe import _hermes_root
    from tools.bot_relay import acquire_turn_lock

    return acquire_turn_lock(_hermes_root(Path(_default_home())), argv[2])


def _run_local_turn(argv: list[str], dm_file: str) -> int:
    """One Bot Chat turn via ``--query-file`` (plus one policy-gated retry); re-emits
    the transport's streams and returns its exit code. Transient failures re-run the
    same session; a context_overflow re-run lets the retried turn's pre-API compaction
    compact the transcript first (no fresh session is ever minted). Auth/quota/config never retry."""

    def _turn():
        return subprocess.run([*argv, "--query-file", dm_file], check=False, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, env=_background_delivery_env())

    proc = _turn()
    if proc.returncode != 0:
        from tools.bot_failure_reasons import RETRY_NONE, classify_agent_error, retry_action

        if retry_action(classify_agent_error((proc.stderr or proc.stdout or "").strip()[-500:])) != RETRY_NONE:
            proc = _turn()
    if proc.returncode != 0 and "already has a live owner" in (proc.stderr or ""):
        # The target's Bot Chat is held live by another surface (Desktop); the turn
        # never ran — tell the sender plainly instead of leaking a raw lease error.
        # See #100523.
        who = argv[argv.index("-p") + 1] if "-p" in argv[:-1] else "the teammate"
        print(json.dumps({
            "error": f"Delivery failed: @{who}'s Bot Chat is open on another "
                     "surface right now, so your message was NOT delivered. Try again later.",
            "reason": "target_busy",
        }))
        return 1
    # Re-emit the transport's streams: stdout is the reply text the
    # completion notification carries back to the sending agent.
    for stream, text in ((sys.stdout, proc.stdout), (sys.stderr, proc.stderr)):
        if text:
            stream.write(text)
            stream.flush()
    return proc.returncode


def _run_delivery(argv: list[str], dm_file: str, *, stdin_file: bool) -> int:
    """Run one DM transport and remove its plaintext file after consumption. The turn
    window (not the enqueue) holds the target profile's cross-process lock, so two
    deliveries into one profile queue; a bounded wait ends in a 'target_busy' refusal.

    Local (query-file) turns get one policy-gated retry (#93091 item 5): transient failures re-run the same
    session; a context_overflow re-run lets the retried turn's pre-API compaction pass compact the Bot Chat
    transcript first (agent/conversation_loop.py) — the sanctioned compression lever; no fresh session is
    ever minted. Auth/quota/config failures never retry. Peer transports (stdin mode) retry on their own
    gateway's deliver path, not here.
    """
    try:
        with _delivery_lock(argv, stdin_file=stdin_file):
            if not stdin_file:
                return _run_local_turn(argv, dm_file)
            # Keep the file open until the transport exits; cleanup occurs
            # after subprocess.run returns, not merely after stdin reaches EOF.
            with open(dm_file, "r", encoding="utf-8") as stream:
                return subprocess.run(argv, stdin=stream, check=False, env=_background_delivery_env()).returncode
    finally:
        _unlink_dm_file(dm_file)


def _delivery_command(argv: list[str], dm_file: str, *, stdin_file: bool) -> str:
    """Build an argv-safe command for the cleanup-owning background runner."""
    runner_argv = [sys.executable, str(Path(__file__).resolve()), "--run-delivery",
                   "stdin" if stdin_file else "query-file", dm_file, *argv]
    if sys.platform == "win32":
        # The tracked local backend uses Git Bash on native Windows: forward slashes keep drive
        # paths executable there; backslash paths are parsed as command names (exit 127).
        runner_argv = [part.replace("\\", "/") for part in runner_argv]
    return shlex.join(runner_argv)


def _start_delivery(argv: list[str], content: str, label: str, *, stdin_file: bool,
                    task_id: Optional[str], agent: Any, display_metadata=None, projection_root=None) -> str:
    """Create a DM file and transfer its cleanup ownership to the runner."""
    dm_file = (_write_dm_file(content, display_metadata=display_metadata, projection_root=projection_root)
               if display_metadata is not None else _write_dm_file(content))
    try:
        command = _delivery_command(argv, dm_file, stdin_file=stdin_file)
    except BaseException:
        _unlink_dm_file(dm_file)
        raise
    return _spawn_delivery(command, label, dm_file=dm_file, task_id=task_id, agent=agent)


def _spawn_delivery(command: str, label: str, *, dm_file: Optional[str] = None,
                    task_id: Optional[str], agent: Any) -> str:
    """Launch the cleanup-owning runner and transfer file ownership on ack. ``dm_file``
    is None for relay deliveries (the waiter watches a reply file; envelope artifacts
    are owned/swept by ``tools/bot_relay.py``)."""
    transferred = False
    try:
        from tools.terminal_tool import terminal_tool

        raw = terminal_tool(command, background=True, notify_on_complete=True, task_id=task_id,
                            workdir=str(Path(__file__).resolve().parent.parent), _host_local=True)
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = {}
        proc_id = parsed.get("session_id") or ""
        if parsed.get("error"):
            return _err(f"Delivery to {label} failed to start: {parsed['error']}")
        if not proc_id:
            return _err(f"Delivery to {label} failed to start: no process id returned")
        # From here the background runner owns the file (removed after the consumer finishes).
        transferred = True
        return json.dumps({
            "status": "sent",
            "to": label,
            "detail": (f"Message dispatched to {label}. This is asynchronous — do NOT wait "
                       "or poll. Finish your turn now; when the delivery completes, its "
                       "notification carries the reply — relay it then, attributed to that agent."),
            "process_id": proc_id,
            "sent_at": int(time.time()),
        })
    except Exception as exc:
        logger.error("message_agent delivery spawn failed: %s", exc, exc_info=True)
        return _err(f"Delivery to {label} could not be started: {exc}")
    finally:
        if dm_file and not transferred:
            _unlink_dm_file(dm_file)


def _delivery_main(args: list[str]) -> int:
    if len(args) < 3 or args[0] != "--run-delivery" or args[1] not in ("stdin", "query-file"):
        return 2
    try:
        return _run_delivery(args[3:], args[2], stdin_file=args[1] == "stdin")
    except Exception as exc:
        # 'target_busy': the queued delivery gave up after its bounded wait — surface the
        # structured payload on stdout so the completion notification carries it back.
        if getattr(exc, "reason", "") == "target_busy":
            # See #93091.
            print(json.dumps({"error": str(exc), "reason": "target_busy"}))
        else:
            print(f"message_agent delivery failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


# agent-context helpers (mirror system_prompt.py's resolution)


def _agent_home(agent: Any) -> str:
    """The calling agent's OWN home (session-db derived), not ambient env."""
    with contextlib.suppress(Exception):
        db_path = getattr(getattr(agent, "_session_db", None), "db_path", None)
        if db_path:
            return str(Path(db_path).parent)
    return _default_home()


def _session_title(agent: Any) -> str:
    title = str(getattr(agent, "_session_title_hint", "") or "").strip()
    if title:
        return title
    with contextlib.suppress(Exception):
        sdb, sid = getattr(agent, "_session_db", None), getattr(agent, "session_id", None)
        if sdb and sid:
            return str(sdb.get_session_title(sid) or "").strip()
    return ""




def _turn_projection(agent: Any) -> dict:
    """The native provenance of the message that started this turn, if any."""
    value = getattr(agent, "_turn_display_projection", None)
    return value if isinstance(value, dict) else {}


def resolve_collaboration_run(agent: Any, *, root: Any, sender: Any, send_id: str) -> str:
    """Which collaboration run this outbound message belongs to.

    A thread used to be "these two agents", so opening the collaboration
    between two teammates showed every message they had ever exchanged — an
    OAuth delegation sent today arrived on top of a finance discussion from
    months earlier, because the pair was the only thing the id knew. The run is
    the missing half, and this is where it is decided: the send site is the one
    place that can see *why* this message is being sent.

    The anchors, strongest first. Each is a durable Hermes identifier; none is
    derived from what any message says, because topic membership guessed from
    prose is exactly what put those finance messages in the OAuth thread.

    1. **Continuation.** This turn was woken by a collaboration — a peer's
       message arriving, or the completion notification for a message this
       agent sent — so what it sends now is part of that same episode. Both
       wakes carry the run in their projection; when an older build staged the
       request without one, the ledger still knows it, addressed by that
       request's own send identity. Inheriting is what makes A→B→C one run, and
       what makes a follow-up land where the exchange it continues is, across a
       turn boundary that no clock could have joined.
    2. **The owner task.** The autonomy work item the sender is executing
       (``agent._owner_continuity_work_id``) — Hermes' existing durable
       identity for one piece of owner work, restart-safe and resumed under the
       same id, which is what proves a resumed task is the same task. Scoped by
       profile, because work ids are assigned in a profile-local store.
    3. **The owner request.** The owner message that woke this turn, when it
       carried session and message provenance but no work item.
    4. **The sending turn.** ``agent._current_turn_id``, bound once per turn:
       every ``message_agent`` call in one turn shares it, so a fan-out to
       thirteen agents is one run with thirteen threads. A later, unrelated
       instruction is a different turn and therefore a different run — which is
       precisely the separation the owner asked for. ``_current_task_id`` stands
       in when the turn has no id, so one fan-out cannot become four cards.
    5. **This send.** Last resort, so a run is *always* resolved and an empty
       run stays an unambiguous marker of "recorded before runs existed".

    **Known limitation, accepted deliberately: inheriting outranks the work
    item.** An agent woken by joud's finance run that then messages faisal about
    an OAuth work item it is separately executing puts that OAuth message on the
    finance card. The fix would be to prefer anchor 2 whenever the waking
    envelope names a different work item — and it is not worth it, because the
    two cases are indistinguishable without reading the message. An agent woken
    by a request *and* holding its own work item is usually messaging onward
    **for the request**, which is anchor 1's whole purpose and what makes
    A→B→C one episode; preferring the work item would fracture every such chain
    to spare this one message. It is bounded: one message, and only for the turn
    the wake started — the next turn is not woken by joud and re-anchors on the
    work item. Pinned by ``test_inherited_run_outranks_a_concurrent_work_item``
    so that changing it has to be a decision rather than an accident.
    """
    from gateway.a2a_threads import collaboration_run, run_for_request

    me = str(sender or "").strip()
    incoming = _turn_projection(agent)

    inherited = str(incoming.get("run_id") or "").strip()
    if not inherited and incoming.get("send_id"):
        # No run to carry: the request was staged by a build that predates
        # them. Its episode is still recorded under the very identity this
        # wake names, so read it back rather than opening a second one.
        with contextlib.suppress(Exception):
            inherited = run_for_request(
                root, sender=incoming.get("sender"), recipient=incoming.get("recipient"),
                send_id=incoming.get("send_id"),
            )
    if inherited:
        # Admitted through the one function that mints runs, so a value that
        # arrived in an envelope is bounded in shape before it reaches a column.
        with contextlib.suppress(Exception):
            admitted = collaboration_run("inherited", inherited)
            if admitted:
                return admitted

    work = str(getattr(agent, "_owner_continuity_work_id", "") or "").strip()
    work = work or str(incoming.get("work_id") or "").strip()
    for anchor in (
        ("work", me, work),
        ("owner", me, incoming.get("source_session_id"), incoming.get("source_message_id")),
        ("turn", me, getattr(agent, "_current_turn_id", "")),
        # The task, when the turn has no id of its own. `_bind_turn_identity`
        # sets both together, so this is only reached on a degenerate agent —
        # but reaching past it to the send would fracture the very thing the run
        # exists to hold together. `message_agent` takes one target, so four
        # teammates are four calls with four send ids, and anchoring each on its
        # own send makes one fan-out four cards. One coarse anchor is better
        # than four wrong ones.
        ("turn", me, getattr(agent, "_current_task_id", "")),
        ("send", send_id),
    ):
        with contextlib.suppress(Exception):
            run = collaboration_run(*anchor)
            if run:
                return run
    return ""


def _record_a2a(root: Any, *, sender: Any, recipients: list, body: str, send_id=None,
                attachments=None, sent_at=None, run_id=None) -> list:
    """Record who said what to whom, at the one point that knows all three.

    Hermes delivers an agent's message by prepending a sentence to it and
    letting the recipient's Bot Chat persist it as an ordinary user message.
    That records nothing structural — no thread, no sender id, no event id — so
    the owner's client could only recover the sender by parsing that sentence
    back out of the prose. This is the identity layer that removes the need to.

    Native structured delivery stages this event before the recipient can run.
    The caller treats an empty result as a pre-admission refusal, preserving a
    complete thread rather than delivering an exchange it cannot represent.
    """
    try:
        from gateway import a2a_threads

        return a2a_threads.record_send(
            root, sender=sender, recipients=recipients, body=body, send_id=send_id,
            attachments=attachments, sent_at=sent_at, run_id=run_id,
        )
    except Exception:  # pragma: no cover - defensive
        logger.debug("a2a thread indexing failed", exc_info=True)
        return []


def _discard_a2a(root: Any, *, sender: Any, send_id: Any) -> None:
    try:
        from gateway.a2a_threads import discard_send
        discard_send(root, sender=sender, send_id=send_id)
    except Exception:  # pragma: no cover - defensive
        logger.debug("a2a staged request cleanup failed", exc_info=True)



def _background_delivery_env() -> dict:
    env = os.environ.copy()
    env["HERMES_BACKGROUND_DELIVERY"] = "1"
    return env


if __name__ == "__main__":  # pragma: no cover - exercised as a background process
    raise SystemExit(_delivery_main(sys.argv[1:]))
