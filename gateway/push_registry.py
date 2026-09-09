"""Devices that should be told when something needs the owner.

BWM-797 Scope 6. Hermes had no push capability at all — no device-token store,
no notification policy, no sender. Asera's client half has existed since the
previous wave and calls `push.register` on every connect, which today answers
"method not found"; this is the other half.

What is here is everything that does not need Apple: the registry, the
eligibility rule, and the payload. What is **not** here is the APNs sender,
because it needs a `.p8` signing key from an Apple Developer Program team, and
this deployment's only team is a free Personal one that cannot carry the
`aps-environment` entitlement. That is recorded as an external gate rather than
faked: a local notification is not a push, and pretending otherwise would make
the gap invisible exactly where it matters.

**Eligibility is the part worth getting right.** A push is the most intrusive
thing this system can do — it lights up a phone on a bedside table — so the
rule is deliberately narrower than "something happened":

* a decision or a block, because the owner is the only one who can clear it;
* an agent finishing work the owner asked for;
* a material failure they would want to know about.

And explicitly not: tool lifecycle, terminal output, internal agent-to-agent
traffic, runtime retries, working telemetry, or anything the transcript already
hides. That is the same owner-visible rule Scope 4 established and Scope 7's
unread contract follows — one definition of "the owner should see this", not a
third opinion.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional


#: APNs device tokens are 64 hex characters today. Validated so a malformed one
#: is refused at registration rather than discovered when a send fails.
_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{64}$")

#: How long a registration survives without being seen again. Apple invalidates
#: tokens on reinstall and the feedback path only tells a sender after the fact,
#: so a registration nothing has refreshed in a month is assumed stale.
_STALE_AFTER_SECONDS = 30 * 24 * 60 * 60


class PushRegistrationRefused(Exception):
    """A device was not registered, with a reason worth acting on."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class RegisteredDevice:
    token: str
    platform: str
    #: Stable per install, so a reinstall replaces rather than accumulates.
    install_id: str
    bundle_id: str
    registered_at: float
    last_seen_at: float

    def to_dict(self) -> dict[str, Any]:
        """What a caller is told. The token is **not** included.

        A registration response has no need for it, and echoing a credential
        back is how it ends up in a log.
        """
        return {
            "install_id": self.install_id,
            "platform": self.platform,
            "bundle_id": self.bundle_id,
            "registered_at": self.registered_at,
            "last_seen_at": self.last_seen_at,
        }


def _db_path(home: Path | str) -> Path:
    return Path(home) / "push_devices.db"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS push_devices (
    install_id    TEXT PRIMARY KEY,
    token         TEXT NOT NULL,
    platform      TEXT NOT NULL,
    bundle_id     TEXT NOT NULL,
    registered_at REAL NOT NULL,
    last_seen_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS push_devices_token ON push_devices(token);
"""


def _connect(home: Path | str) -> sqlite3.Connection:
    path = _db_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def register(
    home: Path | str,
    *,
    token: Any,
    install_id: Any,
    platform: str = "ios",
    bundle_id: str = "",
) -> RegisteredDevice:
    """Record a device, or refresh what is already recorded.

    Keyed by **install id**, not by token: Apple issues a new token on
    reinstall and periodically thereafter, and keying by token would leave a
    row per rotation, every one of them dead. One install is one row, and its
    token is updated in place.

    Idempotent, so the client can call it on every connect — which it does —
    without the registry growing.
    """
    if not isinstance(token, str) or not _TOKEN_RE.match(token.strip()):
        raise PushRegistrationRefused("invalid_token", "That is not a device token.")
    if not isinstance(install_id, str) or not install_id.strip():
        raise PushRegistrationRefused("invalid_install", "No install id was given.")

    token = token.strip().lower()
    install_id = install_id.strip()
    now = time.time()

    conn = _connect(home)
    try:
        existing = conn.execute(
            "SELECT registered_at FROM push_devices WHERE install_id=?", (install_id,)
        ).fetchone()
        registered_at = float(existing["registered_at"]) if existing else now
        with conn:
            conn.execute(
                """INSERT INTO push_devices(
                       install_id, token, platform, bundle_id, registered_at, last_seen_at
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(install_id) DO UPDATE SET
                       token=excluded.token,
                       platform=excluded.platform,
                       bundle_id=excluded.bundle_id,
                       last_seen_at=excluded.last_seen_at""",
                (install_id, token, str(platform), str(bundle_id), registered_at, now),
            )
    finally:
        conn.close()

    return RegisteredDevice(
        token=token, platform=str(platform), install_id=install_id,
        bundle_id=str(bundle_id), registered_at=registered_at, last_seen_at=now,
    )


def devices(home: Path | str, *, include_stale: bool = False) -> list[RegisteredDevice]:
    """Every device that should receive a push."""
    if not _db_path(home).exists():
        return []
    conn = _connect(home)
    try:
        rows = conn.execute("SELECT * FROM push_devices").fetchall()
    finally:
        conn.close()
    cutoff = time.time() - _STALE_AFTER_SECONDS
    out = []
    for row in rows:
        device = RegisteredDevice(
            token=row["token"], platform=row["platform"], install_id=row["install_id"],
            bundle_id=row["bundle_id"], registered_at=float(row["registered_at"]),
            last_seen_at=float(row["last_seen_at"]),
        )
        if include_stale or device.last_seen_at >= cutoff:
            out.append(device)
    return out


def forget(home: Path | str, *, token: str = "", install_id: str = "") -> int:
    """Remove a registration.

    Called on an explicit sign-out, and on APNs' own rejection of a token —
    Apple's feedback is the authoritative "this device is gone", and a sender
    that ignores it keeps paying to talk to nobody.
    """
    if not token and not install_id:
        return 0
    conn = _connect(home)
    try:
        with conn:
            if install_id:
                cursor = conn.execute(
                    "DELETE FROM push_devices WHERE install_id=?", (install_id,)
                )
            else:
                cursor = conn.execute(
                    "DELETE FROM push_devices WHERE token=?", (token.strip().lower(),)
                )
            return cursor.rowcount
    finally:
        conn.close()


# --- eligibility ----------------------------------------------------------

#: Events that may wake a phone. Narrow on purpose — see the module docstring.
NOTIFIABLE = frozenset({
    "decision_required",
    "blocked_on_owner",
    "task_complete",
    "material_failure",
})


def is_notifiable(
    *, kind: str, role: str = "assistant", content: Any = "",
    owner_visible: bool = True, display_kind: Optional[str] = None, display_metadata=None,
) -> bool:
    """Whether this event is worth waking the owner for.

    Two gates, and both must pass. The event has to be one of the few kinds
    that genuinely need a person, **and** it has to be something the owner
    would have been shown anyway — because an event the transcript hides must
    never reach them by a louder route. That second condition is the same rule
    Scope 4 applies to the transcript and Scope 7 to unread; a push that
    disagreed with both would be the loudest possible way to be wrong.
    """
    if not owner_visible:
        return False
    from agent.message_projection import owner_attention
    if not owner_attention(role, content, display_kind, display_metadata):
        return False
    return kind in NOTIFIABLE


def payload(
    *, kind: str, title: str, body: str, endpoint: str, agent: str,
    message_id: str = "", event_id: str = "", hide_preview: bool = False,
) -> dict[str, Any]:
    """The APNs payload, in the shape Asera already knows how to route.

    Routing ids travel; content is minimal. With `hide_preview` the body is
    withheld entirely rather than truncated — a summary of a private message is
    still the private message, and the phone is showing it on a lock screen.

    `event_id` is the dedup key the client already uses to suppress a remote
    alert it has shown locally, so one event cannot arrive twice.
    """
    alert: dict[str, Any] = {"title": title}
    if not hide_preview and body:
        alert["body"] = body

    return {
        "aps": {
            "alert": alert,
            "sound": "default",
            "thread-id": agent,
            # Decisions and blocks interrupt; a finished task waits for the
            # owner to look. Getting this wrong is what teaches people to turn
            # notifications off.
            "interruption-level": (
                "time-sensitive" if kind in {"decision_required", "blocked_on_owner"}
                else "active"
            ),
        },
        "hermes": {
            "endpoint": endpoint,
            "agent": agent,
            **({"message": message_id} if message_id else {}),
            **({"event": event_id} if event_id else {}),
        },
    }
