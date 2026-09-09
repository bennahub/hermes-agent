"""Resolve natural owner follow-ups to the message they already named.

Reply/quote metadata is the first source. When the owner says something like
"أقصد هذي" or "كمل" without a new goal, the current thread, the replied-to
row, or the unique recent owner-visible assistant proposal is the referent.
Ambiguity is only reported when two materially different live targets remain.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

FOLLOW_THROUGH = re.compile(
    r"^(?:"
    r"كمل|صلح(?:ه|ها)?|نفذ|وش الحل|هذي|هذا|"
    r"أقصد\s+هذي|اقصد\s+هذي|أقصد\s+هذا|اقصد\s+هذا|"
    r"نفس اللي فوق|نفذ اقتراحك|"
    r"continue|proceed|fix(?:\s+it)?|do it|go ahead|"
    r"same as above|i mean this|this one|that one"
    r")[.!؟?،, ]*$",
    re.IGNORECASE,
)

_REPLY_QUOTE_CHARS = 500


def is_contextual_followup(text: Any) -> bool:
    return bool(FOLLOW_THROUGH.fullmatch(str(text or "").strip()))


def _excerpt(content: Any) -> str:
    return " ".join(str(content or "").split())[:_REPLY_QUOTE_CHARS]


def _assistant_proposal(row: dict) -> bool:
    if row.get("role") != "assistant" or row.get("display_kind") or not row.get("active", True):
        return False
    try:
        from agent.message_projection import projection
        native = projection(row.get("display_metadata") or {})
    except Exception:
        native = None
    if native:
        return (
            native.get("origin") == "agent"
            and native.get("audience") == "owner"
            and native.get("purpose") in {"message", "result"}
        )
    return bool(str(row.get("content") or "").strip())


def _materially_different(left: dict, right: dict) -> bool:
    a = _excerpt(left.get("content"))
    b = _excerpt(right.get("content"))
    if not a or not b:
        return False
    return hashlib.sha256(a.encode()).hexdigest() != hashlib.sha256(b.encode()).hexdigest()


def unique_conversation_referent(rows: list[dict], *, reply_row: dict | None = None) -> dict | None:
    """Return the single live referent, or None when nothing unique exists."""
    if reply_row and str(reply_row.get("content") or "").strip():
        return reply_row
    proposals = [row for row in rows if _assistant_proposal(row)]
    if not proposals:
        return None
    latest = proposals[-1]
    prior = next((row for row in reversed(proposals[:-1]) if _materially_different(row, latest)), None)
    if prior is not None:
        return None
    return latest


class AmbiguousReferent(ValueError):
    """Two live targets remain and neither is named by a reply/quote."""


def resolve_conversation_referent(
    rows: list[dict],
    text: Any,
    *,
    reply_row: dict | None = None,
) -> dict | None:
    if reply_row and str(reply_row.get("content") or "").strip():
        return reply_row
    if not is_contextual_followup(text):
        return None
    referent = unique_conversation_referent(rows, reply_row=reply_row)
    if referent is not None:
        return referent
    proposals = [row for row in rows if _assistant_proposal(row)]
    distinct = []
    for row in reversed(proposals):
        if not any(not _materially_different(row, seen) for seen in distinct):
            distinct.append(row)
        if len(distinct) == 2:
            raise AmbiguousReferent("Two different live targets remain")
    return None


def followup_quote(row: dict | None) -> str | None:
    if not row:
        return None
    excerpt = _excerpt(row.get("content"))
    row_id = row.get("id") or row.get("_row_id")
    if excerpt and row_id is not None:
        return f"[Continuing the referenced message #{row_id}: \"{excerpt}\"]\n\n"
    if excerpt:
        return f"[Continuing the referenced message: \"{excerpt}\"]\n\n"
    return None
