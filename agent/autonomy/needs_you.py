"""Needs you is only for a decision or secret the owner alone can supply."""
from __future__ import annotations

import re

_INTERNAL = re.compile(
    r"execution_scope|original instruction|structured execution|"
    r"guardian|policy unavailable|policy judge|"
    r"autonomy-observe|bot-chat delivery|"
    r"provider authentication|provider fallback|rate.?limit|"
    r"timed? ?out|retry|cron|scheduler|"
    r"source download|build failed|compileall|"
    r"transport_exhausted|authorization_not_started|"
    r"connection (reset|refused|broke)|revoked credential|"
    r"^until:|"
    r"^external:\s*process:|"
    r"computer capacity|active computer limit|slots? (are )?occup|capacity.?exhaust|"
    r"schema (mismatch|version)|_config_version|"
    r"local checkout|stale (profile|process|venv)|"
    r"systemd user bus|failed to connect to bus",
    re.IGNORECASE,
)

_OWNER_REQUIRED_KINDS = frozenset({
    "owner_input",
    "owner_decision",
    "secret",
    "credential",
    "business_decision",
    "catastrophic_consequence",
})


def is_false_needs_you(reason: str | None = None, *, outcome_kind: str | None = None) -> bool:
    if outcome_kind in {"transport_exhausted", "authorization_not_started"}:
        return True
    if outcome_kind in _OWNER_REQUIRED_KINDS:
        return False
    return bool(_INTERNAL.search(str(reason or "")))


def coerce_terminal(terminal: str, reason: str | None = None, *, outcome_kind: str | None = None) -> str:
    if terminal == "needs_owner" and is_false_needs_you(reason, outcome_kind=outcome_kind):
        return "failed"
    return terminal
