"""Deciding that the owner should be told, and telling them (BWM-797 Scope 6).

`push_registry` knows *which* devices exist, what may wake one, and what the
payload looks like. What it had no answer for was **when** — nothing observed
the system and decided. Registration worked and nothing ever followed from it,
which made the scope look finished from the client side and be inert from the
server side.

This is that missing half. It observes the gateway's own event bus, because that
is the one place every client-facing event already passes through: hooking it
means the notifier cannot form a second opinion about what happened, and cannot
miss an event kind by being wired into only some of the paths that produce it.

**Delivery is still gated on Apple.** Sending an APNs push needs a `.p8` signing
key from a Developer Program team, and this deployment's only team is a free
Personal one that cannot carry the `aps-environment` entitlement. So `deliver`
resolves a sender through `_load_sender()` and finds none, and says so — the
decision is real, recorded and inspectable, and the last hop is honestly
reported as blocked rather than faked. When a key exists, one module appears and
nothing here changes.

The bar stays deliberately high, and is `push_registry.is_notifiable`'s, not a
second one: a push lights up a phone on a bedside table.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from gateway import push_registry

logger = logging.getLogger(__name__)


#: Gateway events that can mean "a person is needed", mapped to the event kind
#: `push_registry.is_notifiable` judges. Everything else on the bus — deltas,
#: tool lifecycle, status updates, presence — is not listed, so the default is
#: silence rather than a filter someone must remember to extend.
_EVENT_KINDS: dict[str, str] = {
    "approval.request": "decision_required",
    "message.complete": "task_complete",
}

#: How long the same (agent, kind) may not repeat. A room that settles in
#: bursts, or a retried turn, must not become a row of identical alerts.
_COOLDOWN_SECONDS = 90.0

_recent: dict[tuple[str, str], float] = {}
_recent_lock = threading.Lock()


class Decision:
    """What the notifier concluded about one event, and why.

    Returned rather than logged-and-forgotten so the reason is testable and so
    `push.status` can show the owner exactly why their phone is quiet.
    """

    __slots__ = ("notify", "reason", "kind", "payload")

    def __init__(
        self,
        notify: bool,
        reason: str,
        *,
        kind: str = "",
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        self.notify = notify
        self.reason = reason
        self.kind = kind
        self.payload = payload

    def to_dict(self) -> dict[str, Any]:
        return {"notify": self.notify, "reason": self.reason, "kind": self.kind}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Decision notify={self.notify} reason={self.reason!r}>"


def _cooled_down(agent: str, kind: str, *, now: float) -> bool:
    key = (agent, kind)
    with _recent_lock:
        last = _recent.get(key, 0.0)
        if now - last < _COOLDOWN_SECONDS:
            return False
        _recent[key] = now
        # Bounded: this is a process-lifetime dict and a long-running gateway
        # would otherwise accumulate one entry per agent per kind forever.
        if len(_recent) > 512:
            cutoff = now - _COOLDOWN_SECONDS
            for stale in [k for k, t in _recent.items() if t < cutoff]:
                _recent.pop(stale, None)
        return True


def reset_for_testing() -> None:
    """Clear the cooldown. Tests only."""
    with _recent_lock:
        _recent.clear()


def consider(
    event: str,
    *,
    agent: str,
    payload: Optional[Mapping[str, Any]] = None,
    owner_is_watching: bool = False,
    now: Optional[float] = None,
) -> Decision:
    """Decide whether this event should wake the owner.

    Four gates, and every one of them can only make the answer quieter:

    * the event has to be one of the few that can mean a person is needed;
    * the owner must not already be looking — a phone that buzzes for a reply
      being read on screen is how people learn to turn notifications off;
    * a failed or interrupted turn is not a completed task;
    * `push_registry.is_notifiable` has the final say, so a push can never
      disagree with what the transcript and the unread badge decided.
    """
    now = time.time() if now is None else now
    data: Mapping[str, Any] = payload or {}

    kind = _EVENT_KINDS.get(event, "")
    if not kind:
        return Decision(False, "not a notifiable event")

    if owner_is_watching:
        return Decision(False, "the owner is already looking")

    if kind == "task_complete":
        status = str(data.get("status") or "")
        if status == "error":
            kind = "material_failure"
        elif status and status != "complete":
            return Decision(False, "the turn did not complete")
        elif not str(data.get("text") or "").strip():
            # A turn that said nothing is not something to be told about.
            return Decision(False, "the turn produced nothing to read")

    if not push_registry.is_notifiable(
        kind=kind,
        role=str(data.get("role") or "assistant"),
        content=data.get("text") or "",
        owner_visible=bool(data.get("owner_visible", True)),
        display_kind=(data.get("display_kind") or None),
        display_metadata=data.get("display_metadata"),
    ):
        return Decision(False, "the transcript does not show this to the owner")

    if not _cooled_down(agent, kind, now=now):
        return Decision(False, "an alert for this was just sent")

    return Decision(True, "the owner is needed", kind=kind)


def _load_sender() -> Optional[Callable[..., Any]]:
    """The APNs sender, if this deployment has one.

    Absent by design rather than by omission: it needs a `.p8` key from a paid
    Developer Program team. Resolved by import so the gate is a fact about the
    deployment, not a flag someone can flip to make tests pass.
    """
    try:  # pragma: no cover - exercised by its absence
        from gateway import apns_sender  # type: ignore

        return getattr(apns_sender, "send", None)
    except Exception:
        return None


def deliver(
    home: Path | str,
    decision: Decision,
    *,
    title: str,
    body: str,
    endpoint: str,
    agent: str,
    message_id: str = "",
    event_id: str = "",
    hide_preview: bool = False,
) -> dict[str, Any]:
    """Send the decision to every registered device, or say why it could not.

    Never raises into a caller: a notification that cannot be sent must not cost
    the turn that produced it.
    """
    if not decision.notify:
        return {"sent": 0, "blocked_reason": decision.reason}

    try:
        devices = push_registry.devices(home)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("push: device registry unavailable: %s", exc)
        return {"sent": 0, "blocked_reason": "registry_unavailable"}

    if not devices:
        return {"sent": 0, "blocked_reason": "no_registered_device"}

    body_payload = push_registry.payload(
        kind=decision.kind,
        title=title,
        body=body,
        endpoint=endpoint,
        agent=agent,
        message_id=message_id,
        event_id=event_id,
        hide_preview=hide_preview,
    )
    decision.payload = body_payload

    sender = _load_sender()
    if sender is None:
        # The honest end of the line. Everything up to Apple is done and
        # correct; the last hop needs a signing key this deployment does not
        # have, and saying so is more useful than a local notification
        # pretending to be a push.
        logger.info(
            "push: would notify %d device(s) for %s/%s — no APNs signing key",
            len(devices), agent, decision.kind,
        )
        return {
            "sent": 0,
            "would_send": len(devices),
            "blocked_reason": "apns_credentials_unavailable",
        }

    sent = 0
    for device in devices:
        try:
            sender(device.token, body_payload)
            sent += 1
        except Exception as exc:  # pragma: no cover - no sender on this box
            logger.warning("push: send failed for one device: %s", exc)
    return {"sent": sent}
