"""Hermes Autonomous Workforce V1 — smallest native-compatible kernel.

Standing Missions live in SOUL + ``$HERMES_HOME/autonomy/mission.md``.
Initiative is woken by native cron and native webhooks. Durable work and
idempotency live in a per-profile SQLite file. Collaboration uses the
existing ``hermes -p <bot> chat`` Bot Chat transport.

This package must not import kernel/store at module import time (CLI
submodules import constants from here).
"""

from __future__ import annotations

OBSERVE_JOB_NAME = "autonomy-observe"
SOUL_BEGIN = "<!-- hermes-autonomy:begin -->"
SOUL_END = "<!-- hermes-autonomy:end -->"
SOUL_HEADING = "## Standing Mission"
SILENT_TOKEN = "[SILENT]"
PILOT_PROFILES = ("abu-saud", "badr", "sami", "nasser")

__all__ = [
    "OBSERVE_JOB_NAME",
    "PILOT_PROFILES",
    "SILENT_TOKEN",
    "SOUL_BEGIN",
    "SOUL_END",
    "SOUL_HEADING",
]
