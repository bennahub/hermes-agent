"""Write-side safety guards for write_file / patch.

Every guard returns ``None`` when the write may proceed, else an error string
the tool returns verbatim.
Guards, in the order the tools apply them: ``_check_sensitive_path`` (hard
deny), ``_check_binary_document_write``, ``_check_protected_instruction_write``
(owner-turn/task authority or approval), ``_check_approval_required_write`` (normal gate),
``_check_cross_profile_path`` (sandbox-mirror lost-work), ``_is_internal_file_tool_content``.
"""
from tools.approval_prompt import prompt_dangerous_approval
from tools.approval_gateway_wait import _await_gateway_decision
from tools.approval_context import _is_unattended_platform_approval_context
from tools.approval_context import _is_single_query_approval_context
from tools.approval_context import _is_cron_approval_context
from tools.approval_context import get_current_session_key
from tools.approval_context import _approval_turn_id

import fnmatch
import logging
import threading
import os
from pathlib import Path

logger = logging.getLogger(__name__)

from tools.binary_extensions import has_opaque_document_extension, is_pdf_path
from tools.file_tools_paths import _expand_tilde, _resolve_path_for_task

# Prefixes matched after realpath. macOS: /private/var mirrors /var — block the
# sensitive subtrees only; a blanket "/private/var/" refuses every temp-file
# write because $TMPDIR, /tmp and /var/folders all realpath there.
_SENSITIVE_PATH_PREFIXES = (
    "/etc/", "/boot/", "/usr/lib/systemd/",
    "/private/etc/",
    "/private/var/db/", "/private/var/root/")
_SENSITIVE_EXACT_PATHS = {"/var/run/docker.sock", "/run/docker.sock"}









def _get_hermes_config_resolved() -> str | None:
    """Return the resolved `config.yaml` path for the home this request runs as."""
    try:
        from hermes_constants import hermes_home_key
        key = hermes_home_key()
    except Exception:
        key = ""

    with _hermes_config_lock:
        if key in _hermes_config_by_home:
            return _hermes_config_by_home[key]

    resolved: str | None
    try:
        from hermes_cli.config import get_config_path
        resolved = str(get_config_path().resolve())
    except Exception:
        try:
            resolved = str(Path(_expand_tilde("~/.hermes/config.yaml")).resolve())
        except Exception:
            resolved = None

    with _hermes_config_lock:
        if len(_hermes_config_by_home) > 256:
            _hermes_config_by_home.clear()
        _hermes_config_by_home[key] = resolved
    return resolved


def _get_real_hermes_home() -> str | None:
    """Return the realpath of the Hermes home **this request is running as**."""
    try:
        from hermes_constants import get_hermes_home, hermes_home_key
        home = get_hermes_home()
        key = hermes_home_key(home)
    except Exception:
        try:
            return os.path.realpath(_expand_tilde("~/.hermes"))
        except (OSError, ValueError, RuntimeError):
            return None

    with _real_hermes_home_lock:
        if key in _real_hermes_home_by_key:
            return _real_hermes_home_by_key[key]

    try:
        resolved: str | None = os.path.realpath(str(home))
    except (OSError, ValueError, RuntimeError):
        resolved = None

    with _real_hermes_home_lock:
        # A cap, not a policy: one entry per profile this process ever serves.
        if len(_real_hermes_home_by_key) > 256:
            _real_hermes_home_by_key.clear()
        _real_hermes_home_by_key[key] = resolved
    return resolved


def _resolved_or_raw(filepath: str, task_id: str) -> str:
    """Task-resolved path string, falling back to the raw input on resolution failure."""
    try:
        return str(_resolve_path_for_task(filepath, task_id))
    except (OSError, ValueError):
        return filepath


def _check_sensitive_path(filepath: str, task_id: str = "default") -> str | None:
    """Return an error message if the path targets a sensitive system location."""
    candidates = (_resolved_or_raw(filepath, task_id), os.path.normpath(_expand_tilde(filepath)))
    if any(c.startswith(_SENSITIVE_PATH_PREFIXES) or c in _SENSITIVE_EXACT_PATHS for c in candidates):
        return (
            f"Refusing to write to sensitive system path: {filepath}\n"
            "Use the terminal tool with sudo if you need to modify system files.")
    # approvals.mode and other security settings live in config.yaml; a
    # prompt-injected agent could silently disable exec approval by editing it.
    hermes_config = _get_hermes_config_resolved()
    if hermes_config and hermes_config in candidates:
        return (
            f"Refusing to write to Hermes config file: {filepath}\n"
            "Agent cannot modify security-sensitive configuration. "
            "Edit ~/.hermes/config.yaml directly or use 'hermes config' instead.")
    return None


# ── Protected agent-instruction files (always-ask approval gate) ─────────
# Files that steer FUTURE agent behavior are a prompt-injection persistence
# vector (AGENTS.md / CLAUDE.md / SOUL.md / .cursorrules / project .hermes tree).
# Writes ALWAYS require human approval — even under --yolo — and fail closed
# without a human channel. Basenames match in ANY directory, case-insensitively.
# Ported from: RooCodeInc/Roo-Code RooProtectedController (Apache-2.0). Companion: the terminal-tool vector
# is covered separately (#58631); this gate covers the write_file/patch vector. Symlink lesson from #41351:
# always realpath before matching. Scope decision (documented): basenames match in ANY directory, because
# project-context instruction files are loaded from cwd trees — an AGENTS.md anywhere the agent might later
# run from is a live target. Basenames match case-insensitively so case-variant spellings on
# case-insensitive filesystems (macOS/Windows) cannot slip past; on case-sensitive filesystems most loaders
# probe common case variants too, so the stricter behavior is kept uniform.
_PROTECTED_INSTRUCTION_BASENAMES = frozenset({
    "agents.md", "claude.md", "soul.md", ".cursorrules"})


def _protected_instruction_config() -> tuple[bool, list[str]]:
    """Return ``(enabled, extra_patterns)`` from ``security.protected_instruction_files`` /
    ``security.protected_instruction_extra_patterns`` (fnmatch on basename). Config read
    failures keep the gate ON — fail-safe for a security boundary."""
    try:
        from hermes_cli.config import load_config, cfg_get
        cfg = load_config()
        enabled = cfg_get(cfg, "security", "protected_instruction_files", default=True)
        extra = cfg_get(cfg, "security", "protected_instruction_extra_patterns", default=[])
    except Exception:
        return True, []
    if not isinstance(enabled, bool):
        enabled = True
    if not isinstance(extra, list):
        extra = []
    return enabled, [str(p) for p in extra if p]


def _protected_instruction_reason(filepath: str, task_id: str = "default",
                                  *, enabled: bool | None = None,
                                  extra_patterns: list[str] | None = None) -> str | None:
    """Return a short label when ``filepath`` targets a protected
    agent-instruction file, else ``None``.

    Matching runs on BOTH the normalized input path and its realpath so
    neither a symlink pointing AT a protected file (#41351) nor a protected
    name that is itself a symlink escapes the gate. ``..`` traversal is
    neutralized by normpath/realpath before the basename compare.
    """
    if enabled is None or extra_patterns is None:
        enabled, extra_patterns = _protected_instruction_config()
    if not enabled:
        return None

    normalized = os.path.normpath(_expand_tilde(filepath))
    try:
        resolved = os.path.realpath(str(_resolve_path_for_task(filepath, task_id)))
    except (OSError, ValueError, RuntimeError):
        resolved = os.path.realpath(normalized)

    # The authoritative ~/.hermes home is governed by its own guards
    # (config.yaml hard-block, cross-profile guard, write_approval), so
    # ORDINARY files under it are not this gate's business — gating the whole
    # home would make an agent's own working directory unusable, and the
    # ``.hermes`` component rule below would otherwise match the home itself.
    #
    # An agent's own charter is not an ordinary file. It defines what that
    # agent is and what it may do, and "it lives under my own home" is not a
    # reason to let an agent rewrite it unwatched — that is precisely the file
    # an agent would rewrite to widen its own boundaries. So the protected
    # basenames stay protected inside the home too; what happens next is the
    # caller's decision, and an owner who explicitly asked for the edit
    # authorises it without a prompt (see `_owner_task_covers`).
    real_home = _get_real_hermes_home()
    if real_home and (resolved == real_home
                      or resolved.startswith(real_home + os.sep)):
        import fnmatch as _fnmatch
        for candidate in (normalized, resolved):
            base = os.path.basename(candidate)
            if base.lower() in _PROTECTED_INSTRUCTION_BASENAMES:
                return _protected_instruction_label(candidate, base)
            for pattern in extra_patterns:
                if _fnmatch.fnmatch(base.lower(), pattern.lower()):
                    return _protected_instruction_label(candidate, base)
        return None

    import fnmatch
    for candidate in (normalized, resolved):
        base = os.path.basename(candidate)
        base_lower = base.lower()
        if base_lower in _PROTECTED_INSTRUCTION_BASENAMES:
            return _protected_instruction_label(candidate, base)
        for pattern in extra_patterns:
            if fnmatch.fnmatch(base_lower, pattern.lower()):
                return _protected_instruction_label(candidate, base)
        # Project-local .hermes config dirs (e.g. <repo>/.hermes/config.yaml)
        # are loaded as project context and steer behavior the same way.
        # Scope: the file's IMMEDIATE parent must be ``.hermes`` — matching
        # any ancestor named .hermes would gate every write inside a
        # checkout that happens to live under ~/.hermes (e.g. the
        # hermes-agent repo itself at ~/.hermes/hermes-agent).
        parts = candidate.replace("\\", "/").rstrip("/").split("/")
        if len(parts) >= 2 and parts[-2] == ".hermes":
            return candidate
    return None


_APPROVAL_UNAVAILABLE = "requires approval but the approval subsystem is unavailable."
_NO_HUMAN = "requires approval but no interactive user or gateway is present to approve it."


def _request_protected_instruction_approval(
        reasons: list[str], task_id: str = "default", *,
        targets: list[str] | None = None,
        session_key: str | None = None,
        turn_key: str | None = None) -> str | None:
    """Ask the human to approve a write to protected instruction file(s).

    Returns ``None`` when approved, or a BLOCKED error string. This gate
    intentionally does NOT route through ``_run_approval_gate``: that gate
    honors --yolo and session/permanent allowlists, and neither may reach
    these files. Fail-closed when no human channel exists.

    The owner may answer with the wider, task-scoped grant ("for this task"),
    which is recorded against this turn and these exact resolved targets — see
    the note above ``_task_instruction_authority``. ``always`` is still never
    offered: nothing here is ever persisted beyond the running task.
    """
    targets_label = ", ".join(dict.fromkeys(reasons))
    description = (
        f"Write to protected agent-instruction file(s): {targets_label}. "
        "These files steer future agent behavior; approval is always "
        "required (not bypassed by auto-approve)."
    )
    display = f"<write to {targets_label}>"
    blocked = (
        f"BLOCKED: write to protected agent-instruction file(s) ({targets_label}) "
        "{why} The user has NOT consented to this write. Do NOT retry it or "
        "attempt the same edit via another path (terminal, execute_code, "
        "etc.)."
    )

    try:
        import tools.approval as _approval
    except Exception:
        return blocked.format(why="requires approval but the approval "
                                  "subsystem is unavailable.")

    # Gateway surface: block on the button round-trip when a notify callback
    # is registered for this session (Telegram/Discord/Slack). One-operation
    # only — no session/permanent buttons are offered.
    if session_key is None:
        session_key = get_current_session_key()
    if turn_key is None:
        turn_key = _current_task_authority_turn()
    if targets is None:
        targets = []
    # The wider scope is offered only where it could be honoured at all.
    allow_task_scope = bool(turn_key) and bool(targets) and _task_authority_scope_allowed()
    notify_cb = None
    try:
        with _approval._lock:
            notify_cb = _approval._gateway_notify_cbs.get(session_key)
    except Exception:
        notify_cb = None

    if notify_cb is not None:
        approval_data = {
            "command": display,
            "pattern_key": "protected_instruction_file",
            "pattern_keys": ["protected_instruction_file"],
            "description": description,
            "allow_permanent": False,
            "allow_session": allow_task_scope,
        }
        decision = _await_gateway_decision(
            session_key, notify_cb, approval_data, surface="gateway",
        )
        if decision.get("notify_failed"):
            return blocked.format(
                why="requires approval but the approval request could not "
                    "be delivered.")
        choice = decision.get("choice")
        if decision.get("resolved") and choice in {"once", "session", "always"}:
            # "session" is the owner choosing to settle the whole task; it is
            # honoured only where the scope was actually offered, so a client
            # that sends it unprompted still gets one operation and nothing
            # more. "always" is never offered and is never persisted.
            if choice == "session" and allow_task_scope:
                _grant_task_authority(session_key, turn_key, targets)
                logger.warning(
                    "task-scoped authority granted by the user for protected "
                    "agent-instruction file(s): targets=%s session=%s turn=%s",
                    targets_label, session_key, turn_key,
                )
            return None
        if not decision.get("resolved"):
            return blocked.format(
                why="approval prompt timed out without a user response. "
                    "Silence is not consent.")
        return blocked.format(why="was denied by the user.")

    # CLI surface: per-thread approval callback (prompt_toolkit panel).
    callback = None
    try:
        from tools.terminal_tool import _get_approval_callback
        callback = _get_approval_callback()
    except Exception:
        callback = None

    if callback is not None:
        choice = prompt_dangerous_approval(
            display, description,
            allow_permanent=False,
            allow_session=allow_task_scope,
            approval_callback=callback,
        )
        if choice in {"once", "session", "always"}:
            if choice == "session" and allow_task_scope:
                _grant_task_authority(session_key, turn_key, targets)
                logger.warning(
                    "task-scoped authority granted by the user for protected "
                    "agent-instruction file(s): targets=%s session=%s turn=%s",
                    targets_label, session_key, turn_key,
                )
            return None
        if choice == "timeout":
            return blocked.format(
                why="approval prompt timed out without a user response. "
                    "Silence is not consent.")
        return blocked.format(why="was denied by the user.")

    # No human channel at all (script, cron, background thread): fail
    # closed. Auto-approving here would recreate the persistence vector.
    return blocked.format(
        why="requires approval but no interactive user or gateway is "
            "present to approve it.")


def _check_protected_instruction_write(paths: list[str],
                                       task_id: str = "default") -> str | None:
    """Gate a write/patch touching protected instruction files.

    Returns ``None`` when no target is protected or the human approved;
    otherwise a BLOCKED error string. For multi-file V4A patches, ONE
    protected file gates the ENTIRE patch: a single prompt lists every
    protected target, and a deny applies nothing (including innocent
    files) — partial application of an approved-in-part patch would be
    more surprising than an atomic all-or-nothing outcome.
    """
    enabled, extra = _protected_instruction_config()
    if not enabled:
        return None
    reasons: list[str] = []
    targets: list[str] = []
    for p in paths:
        reason = _protected_instruction_reason(
            p, task_id, enabled=enabled, extra_patterns=extra)
        if reason:
            reasons.append(reason)
            targets.append(_protected_target_key(p, task_id))
    if not reasons:
        return None

    session_key = _current_approval_session_key()
    turn_key = _current_task_authority_turn()

    # The owner's own task, arriving over their authenticated connection, is
    # itself the authority for the ordinary protected writes it implies. This
    # is what removes the first prompt: an owner who said "edit Mishari's
    # charter" is not asked whether they meant it.
    granted = _owner_task_authority(turn_key)
    if granted is None:
        logger.info(
            "protected agent-instruction write with no owner task authority: "
            "targets=%s turn=%s session=%s",
            ", ".join(dict.fromkeys(reasons)), turn_key, session_key)
    if granted is not None:
        verdicts = [_owner_task_covers(target, task_id) for target in targets]
        if all(covered for covered, _ in verdicts):
            logger.warning(
                "protected agent-instruction write proceeding under owner task "
                "authority: targets=%s reasons=%s session=%s turn=%s nonce=%s",
                ", ".join(dict.fromkeys(reasons)),
                "; ".join(dict.fromkeys(why for _, why in verdicts)),
                session_key, turn_key, granted.get("nonce"),
            )
            return None
        logger.warning(
            "owner task authority does not extend to this write; asking: "
            "targets=%s reasons=%s session=%s turn=%s",
            ", ".join(dict.fromkeys(reasons)),
            "; ".join(why for covered, why in verdicts if not covered),
            session_key, turn_key,
        )

    # Already authorised by the owner, for this task, for these exact files.
    if _task_authority_covers(session_key, turn_key, targets):
        logger.warning(
            "protected agent-instruction write proceeding under task-scoped "
            "authority granted by the user: targets=%s session=%s turn=%s",
            ", ".join(dict.fromkeys(reasons)), session_key, turn_key,
        )
        return None

    return _request_protected_instruction_approval(
        reasons, task_id, targets=targets,
        session_key=session_key, turn_key=turn_key,
    )


def _check_approval_required_write(paths: list[str], task_id: str = "default") -> str | None:
    """Gate a write/patch touching an approval-required path (``~/.ssh/config`` can steer
    execution via ``ProxyCommand``). Routine gate: once/session/always, honors --yolo,
    fail-closed without an interactive/gateway channel."""
    try:
        from agent.file_safety import is_write_approval_required
    except Exception:
        return None

    targets = [p for p in paths if is_write_approval_required(p)]
    if not targets:
        return None

    display_targets = ", ".join(dict.fromkeys(targets))
    description = (
        f"Write to SSH client config file(s): {display_targets}. "
        "The SSH config can carry ProxyCommand / Match exec directives that "
        "run commands, so writes require your approval.")
    blocked = (
        f"BLOCKED: write to SSH config file(s) ({display_targets}) "
        "{why} Do NOT retry it via another path (terminal, execute_code) "
        "without the user's explicit consent.")

    try:
        import tools.approval as _approval
    except Exception:
        return blocked.format(why=_APPROVAL_UNAVAILABLE)

    result = _approval._run_approval_gate(
        pattern_key="ssh_config_write",
        description=description,
        display_target=f"<write to {display_targets}>",
        cron_deny_message=blocked.format(why="requires approval but this cron session denies it."),
        single_query_deny_message=blocked.format(
            why="requires approval but single-query (-q) sessions run "
                "without a user present to approve it. To allow flagged "
                "actions in single-query mode, set approvals.single_query_mode: "
                "approve in config.yaml."),
        autoapprove_log_prefix="ssh_config_write",
        fail_closed_when_no_human=True,
        no_human_block_message=blocked.format(why=_NO_HUMAN))
    if result.get("approved"):
        return None
    return result.get("message") or blocked.format(why="was denied.")


def _get_container_mirror_prefix_for_task(task_id: str = "default") -> str | None:
    """Return the container-side Hermes mirror prefix for persistent Docker file tools."""
    try:
        from tools.terminal_tool import (
            _active_environments, _env_lock, _get_env_config, _resolve_container_task_id)
        container_key = _resolve_container_task_id(task_id)
        with _env_lock:
            env = _active_environments.get(container_key) or _active_environments.get(task_id)
        if env is not None:
            persistent_docker = (env.__class__.__name__ == "DockerEnvironment"
                                 and bool(getattr(env, "_persistent", False)))
            return "/root/.hermes" if persistent_docker else None
        config = _get_env_config()
    except Exception:
        return None
    if config.get("env_type") == "docker" and config.get("container_persistent", True):
        return "/root/.hermes"
    return None


def _check_cross_profile_path(filepath: str, task_id: str = "default") -> str | None:
    """Soft-guard: warn when ``filepath`` lands on a host-side or Docker sandbox MIRROR of
    Hermes state (a write the host never reads). Not profile isolation — that guard was
    removed; ``cross_profile=True`` keeps bypassing this one for replay compat. Fails open."""
    try:
        from agent.file_safety import get_container_mirror_warning, get_sandbox_mirror_warning
    except Exception:
        return None
    resolved = _resolved_or_raw(filepath, task_id)
    warning = get_sandbox_mirror_warning(resolved)
    if warning is not None:
        return warning
    return get_container_mirror_warning(resolved, mirror_prefix=_get_container_mirror_prefix_for_task(task_id))


def _check_binary_document_write(filepath: str, task_id: str = "default") -> str | None:
    """Reject text-tool writes that would corrupt a binary document (read_file showed
    EXTRACTED text, so the model may write it back). Opaque formats are always rejected;
    .pdf only when OVERWRITING an existing file (raw PDF syntax is text-authorable).

    ``read_file`` auto-extracts .docx/.xlsx/.pptx (and PDF, via anydoc) to readable text, so the model
    plausibly believes it holds the file's contents and tries to write the edited text back with
    write_file/patch. A plain-text write can never produce a valid OOXML/OLE/ODF container, so that write
    silently destroys the document (port of nearai/ironclaw#7109).
    """
    if has_opaque_document_extension(filepath):
        ext = filepath[filepath.rfind("."):].lower()
        return (
            f"Refusing to write plain text to binary document '{filepath}' ({ext}). "
            "A text write cannot produce a valid document container and would "
            "corrupt the file (read_file showed you EXTRACTED text, not the real "
            "bytes). Use the docx/xlsx/powerpoint skills or a library like "
            "python-docx/openpyxl/python-pptx via the terminal to create or edit "
            "this document.")
    if is_pdf_path(filepath):
        try:
            resolved = Path(_resolve_path_for_task(filepath, task_id))
        except Exception:
            resolved = Path(_expand_tilde(filepath))
        try:
            if resolved.is_file():
                return (
                    f"Refusing to overwrite existing PDF '{filepath}' with plain text. "
                    "read_file showed you EXTRACTED text, not the real bytes — writing "
                    "text back would destroy the document. Use the pdf skill or a PDF "
                    "library via the terminal to modify it. (Creating a NEW .pdf file "
                    "is allowed.)")
        except OSError:
            pass
    return None


# ── Internal display text must never be persisted as file content ────────
_READ_DEDUP_STATUS_MESSAGE = (
    "File unchanged since last read. The content from "
    "the earlier read_file result in this conversation is "
    "still current — refer to that instead of re-reading.")


def _is_internal_file_status_text(content: str) -> bool:
    """True when content is the read_file dedup status message, verbatim or lightly framed
    (contains the full message and is <=2x its length — a real file quoting it would be longer)."""
    if not isinstance(content, str):
        return False
    stripped = content.strip()
    return bool(stripped) and _READ_DEDUP_STATUS_MESSAGE in stripped and (
        len(stripped) <= 2 * len(_READ_DEDUP_STATUS_MESSAGE))


def _looks_like_read_file_line_numbered_content(content: str) -> bool:
    """True for content dominated by read_file's ``LINE_NUM|CONTENT`` display (>=60% of
    non-empty lines are consecutive numbered lines; a lone ``1|value`` is allowed)."""
    if not isinstance(content, str):
        return False
    lines = [line for line in content.splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    numbered: list[int] = []
    for line in lines:
        prefix, sep, _rest = line.lstrip().partition("|")
        if sep and prefix.isdigit():
            numbered.append(int(prefix))
    if len(numbered) < 2 or len(numbered) / len(lines) < 0.6:
        return False
    consecutive_pairs = sum(1 for prev, current in zip(numbered, numbered[1:]) if current == prev + 1)
    return consecutive_pairs >= len(numbered) - 1


def _is_internal_file_tool_content(content: str) -> bool:
    """Return True when content is file-tool display text, not intended file bytes."""
    return _is_internal_file_status_text(content) or _looks_like_read_file_line_numbered_content(content)


def reset_hermes_config_cache() -> None:
    """Drops the memoised config paths. For tests, and for a profile that moves."""
    with _hermes_config_lock:
        _hermes_config_by_home.clear()

def reset_real_hermes_home_cache() -> None:
    """Drops the memoised homes. For tests, and for a profile that moves."""
    with _real_hermes_home_lock:
        _real_hermes_home_by_key.clear()

def _current_task_authority_turn() -> str | None:
    """The current turn's id, or ``None`` when there is no turn identity.

    ``None`` means no grant can be minted or honoured: a grant with no turn to
    belong to would silently become session-wide, which is the persistence
    vector this gate exists to avoid.
    """
    try:
        import tools.approval as _approval
        turn_id = _approval_turn_id.get()
    except Exception:
        return None
    turn_id = (turn_id or "").strip()
    return turn_id or None

def _task_authority_scope_allowed() -> bool:
    """Whether this context may be offered the wider, task-scoped grant.

    A necessary condition, never a sufficient one — the owner's answer is what
    actually grants. Fail-closed on any error: an upstream refactor that
    removes one of these predicates must degrade to "ask every time", not to
    "grant silently".
    """
    try:
        import tools.approval as _approval
        if _is_cron_approval_context():
            return False
        if _is_single_query_approval_context():
            return False
        if _is_unattended_platform_approval_context():
            return False
        if _approval.is_approval_bypass_active():
            return False
        from agent.delegation_context import is_delegated_child_context
        if is_delegated_child_context():
            return False
    except Exception:
        return False
    return True

def _protected_target_key(filepath: str, task_id: str = "default") -> str:
    """The identity a grant is recorded against: the fully resolved target.

    Resolved the same way the gate resolves it, so a grant for
    ``profiles/mishari/SOUL.md`` cannot be spent on a symlink, a ``..`` path or
    a relative spelling of a different file.
    """
    try:
        return os.path.realpath(str(_resolve_path_for_task(filepath, task_id)))
    except (OSError, ValueError, RuntimeError):
        try:
            return os.path.realpath(os.path.normpath(_expand_tilde(filepath)))
        except (OSError, ValueError, RuntimeError):
            return os.path.normpath(_expand_tilde(filepath))

def _task_authority_covers(session_key: str, turn_key: str | None,
                           targets: list[str]) -> bool:
    """True only when every target is already covered for this exact task."""
    if not turn_key or not targets:
        return False
    with _task_instruction_authority_lock:
        granted = _task_instruction_authority.get((session_key, turn_key))
        if not granted:
            return False
        return all(target in granted for target in targets)

def _grant_task_authority(session_key: str, turn_key: str | None,
                          targets: list[str]) -> None:
    if not turn_key or not targets:
        return
    with _task_instruction_authority_lock:
        if len(_task_instruction_authority) >= _TASK_AUTHORITY_MAX_TASKS:
            _task_instruction_authority.clear()
        _task_instruction_authority.setdefault(
            (session_key, turn_key), set()).update(targets)

def clear_task_instruction_authority(session_key: str | None = None,
                                     turn_key: str | None = None) -> int:
    """Drops recorded authority. Called at task end, and available to tests.

    Not required for correctness — a turn id is unique, so a finished task's
    entry can never match again — but a task that ends should not leave a
    grant lying in memory.
    """
    with _task_instruction_authority_lock:
        if session_key is None and turn_key is None:
            dropped = len(_task_instruction_authority)
            _task_instruction_authority.clear()
            return dropped
        doomed = [
            key for key in _task_instruction_authority
            if (session_key is None or key[0] == session_key)
            and (turn_key is None or key[1] == turn_key)
        ]
        for key in doomed:
            _task_instruction_authority.pop(key, None)
        return len(doomed)

def _owner_task_authority(turn_key: str | None) -> dict | None:
    """The owner-origin grant for this turn, if the server minted one.

    Minted at the authenticated RPC ingress, never by anything the model can
    say — see ``tools/owner_task_authority``.
    """
    if not turn_key:
        return None
    try:
        from tools import owner_task_authority
        return owner_task_authority.turn_authority(turn_key)
    except Exception:
        return None

def _acting_profile_name() -> str | None:
    try:
        from agent.file_safety import _resolve_active_profile_name
        return _resolve_active_profile_name()
    except Exception:
        return None

def _profile_owning_path(target: str) -> str | None:
    """The profile a path belongs to, or ``None`` if it belongs to none.

    ``<root>/profiles/<name>/...`` → ``name``; anything directly under the
    root → ``default``.
    """
    try:
        from agent.file_safety import _hermes_root_path
        root = os.path.realpath(str(_hermes_root_path()))
    except Exception:
        return None
    target = os.path.realpath(target)
    if not (target == root or target.startswith(root + os.sep)):
        return None
    rel = os.path.relpath(target, root).replace("\\", "/").split("/")
    if rel and rel[0] == "profiles" and len(rel) >= 2:
        return rel[1]
    return "default"

def _owner_task_covers(target: str, task_id: str) -> tuple[bool, str]:
    """Whether an owner-sent task ordinarily implies writing this file.

    What stays outside is anything the task is not working on. A protected file
    inside this Hermes installation is ordinary task material for a task the
    owner sent; one anywhere else is not, and is still asked about.

    "The task's working tree" was in this rule and has been removed. The base
    directory it resolved to is the live terminal working directory, which the
    model changes with `cd` — so an agent could widen its own authority to any
    checkout on the machine by stepping into it first. Authorisation may not be
    derived from something the thing being authorised controls.

    The agent's own charter is inside — but only here, under an owner task.
    On its own initiative an agent has no authority over it at all: without an
    owner-minted grant this function is never consulted and the gate asks.
    """
    owning = _profile_owning_path(target)
    if owning is not None:
        acting = _acting_profile_name()
        if acting is not None and owning == acting:
            # The agent's own charter, which it may not touch on its own
            # initiative — without an owner task there is no authority here at
            # all and the gate asks. With one, the owner asked for this exact
            # thing ("edit your charter"), and asking again is the redundancy
            # they told us to remove.
            return True, "the agent's own instruction file, under an owner-sent task"
        return True, "another profile in this installation"

    return False, "outside this installation"

def _current_approval_session_key() -> str:
    try:
        import tools.approval as _approval
        return get_current_session_key()
    except Exception:
        return "default"

def _protected_instruction_label(candidate: str, base: str) -> str:
    """Whose protected file this is, not merely which name it has.

    The bare basename made every agent's charter read "SOUL.md", so an owner
    editing thirteen of them was handed thirteen approval prompts with
    identical text and no way to tell which was which — on the one gate whose
    entire purpose is that the owner decides knowingly. The profile is the fact
    that distinguishes them, and it is the fact the decision turns on.

    Deliberately no regex and no new import: this runs on the hot path of every
    file write by every agent, and the segment after ``profiles`` is all that
    is wanted.

    Display only. Authority matching uses the resolved target key from
    ``_protected_target_key``, never this label.
    """
    parts = str(candidate).replace("\\", "/").split("/")
    try:
        index = len(parts) - 1 - parts[::-1].index("profiles")
    except ValueError:
        return base
    if index + 1 < len(parts) and parts[index + 1]:
        return f"{parts[index + 1]}/{base}"
    return base

_hermes_config_by_home: dict[str, str | None] = {}

_hermes_config_lock = threading.Lock()

_real_hermes_home_by_key: dict[str, str | None] = {}

_real_hermes_home_lock = threading.Lock()

_task_instruction_authority: dict[tuple[str, str], set[str]] = {}

_task_instruction_authority_lock = threading.Lock()

_TASK_AUTHORITY_MAX_TASKS = 64
