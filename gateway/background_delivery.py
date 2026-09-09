"""Owner-facing delivery policy for non-user-initiated (background) turns.

A background event — webhook, cron/routine, agent-to-agent relay — is executed
by running a real chat turn inside the profile's canonical Bot Chat session
(``hermes -p <profile> chat -c "Bot Chat" …``).  That is deliberate: the agent
needs Bot Chat context to judge the event.  The side effect is that BOTH the
transport envelope (persisted as a ``user`` message) and the agent's reply
(persisted as ``assistant``) land in the owner's chat — including replies that
are nothing but a ``[SILENT]`` sentinel.

The owner's policy is: a background event may contribute **only** the agent's
useful final summary.  Transport scaffolding and no-op outcomes must not exist
as chat messages at all — not as a row, not as a preview, not as an unread, not
as a notification.

This module is the single decision point for that.  Transport filtering is
inert unless :func:`background_delivery_active` is true, which requires the
``HERMES_BACKGROUND_DELIVERY`` environment variable to be set by the background
path itself.  One invariant is global: an exact assistant silence control token
is never persisted or displayed as prose on any surface.

This is NOT Quiet Bot Mode.  Quiet Bot Mode hides execution *detail* on
messages that legitimately exist.  This policy stops transport noise and no-op
outcomes from becoming messages in the first place.
"""

from __future__ import annotations

import os
import re
from typing import Any

# Set by background delivery paths (webhook bot-chat delivery, cron/routine
# delivery, agent-to-agent background handling) around the chat subprocess.
BACKGROUND_DELIVERY_ENV = "HERMES_BACKGROUND_DELIVERY"


# Transport envelopes injected as the "user" turn of a background event.
# These are execution inputs, not something the owner said.
_ENVELOPE_PATTERNS = (
    # [Webhook "route-name" output — inbound event, not the user. ...]
    re.compile(r'^\s*\[\s*webhook\b[^\]]*\boutput\b[^\]]*\]', re.I),
    # [Cronjob "name" output — scheduled job, not the user. ...]
    # [Routine "name" output — ...] / [Scheduled task output — ...]
    re.compile(r'^\s*\[\s*(?:routine|cronjob|cron|scheduled(?:\s+task)?)\b[^\]]*\boutput\b[^\]]*\]', re.I),
    # Generic "... , not the user ..." transport wrapper regardless of label —
    # covers both "inbound event, not the user" (webhook) and "scheduled job,
    # not the user" (cron / agent-to-agent relay, which share this lane).
    re.compile(r'^\s*\[[^\]]*\bnot the user\b[^\]]*\]', re.I),
)

# Agent-to-agent communication is conversation, not transport noise. The owner
# expects it to remain visible in the recipient profile's canonical Bot Chat.
_AGENT_RELAY_PATTERNS = (
    re.compile(r'^\s*Message from\s+🤖\s+[^:]+\s+\(@[^)]+\):', re.I),
    re.compile(r'^\s*\[\s*Message from\s+@[^\]]*\bagent relay\b[^\]]*\]', re.I),
)

# Placeholder / test scaffolding some sources emit as their whole payload.
_PLACEHOLDER_PATTERNS = (
    re.compile(r'^\s*placeholder\b.*\bignore\b\s*$', re.I),
    re.compile(r'^\s*\[?\s*placeholder\s*(?:—|-|:)?\s*ignore\s*\]?\s*$', re.I),
)


def background_delivery_active() -> bool:
    """True when this process is executing a background-originated turn."""
    value = os.environ.get(BACKGROUND_DELIVERY_ENV, "")
    return value.strip().lower() not in ("", "0", "false", "no")


def is_transport_envelope(content: Any) -> bool:
    """True when ``content`` is a background transport wrapper, not a person.

    Matches only the *opening* wrapper line.  A real summary that happens to
    quote an envelope later in its body is not a wrapper.
    """
    if not isinstance(content, str):
        return False
    stripped = content.strip()
    if not stripped:
        return False
    if any(p.search(stripped) for p in _AGENT_RELAY_PATTERNS):
        return False
    return any(p.search(stripped) for p in _ENVELOPE_PATTERNS)


def is_placeholder_payload(content: Any) -> bool:
    """True for bare placeholder/test scaffolding payloads."""
    if not isinstance(content, str):
        return False
    stripped = content.strip()
    if not stripped or len(stripped) > 200:
        return False
    return any(p.match(stripped) for p in _PLACEHOLDER_PATTERNS)


def is_no_material_result(content: Any) -> bool:
    """True when an agent reply carries no material outcome for the owner.

    Reuses the gateway's autonomous silence matcher so the marker set can
    never drift from the rest of the codebase.
    """
    if not isinstance(content, str):
        return False
    if not content.strip():
        return True
    try:
        from gateway.response_filters import is_autonomous_silence_response
    except Exception:  # pragma: no cover — never break persistence
        return False
    return bool(is_autonomous_silence_response(content))


def should_suppress_owner_facing_message(
    role: Any,
    content: Any,
    tool_calls: Any = None,
) -> bool:
    """The policy. True => do not persist this as an owner-facing message.

    Exact assistant silence tokens are global; transport filtering applies only
    while :func:`background_delivery_active`.  Fails open on anything
    unexpected, because losing a real event is far worse than showing one noisy
    row.
    """
    try:
        if not background_delivery_active():
            return False
        if role == "user":
            return is_transport_envelope(content) or is_placeholder_payload(content)
        if role == "assistant":
            # Suppress ONLY a no-material-result reply. A real summary — a
            # decision, blocker, failure, alert, approval request, completed
            # work — is always delivered.
            return is_no_material_result(content)
        return False
    except Exception:  # pragma: no cover — never break persistence
        return False
