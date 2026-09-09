"""Credential redaction for durable historical summary text."""
import re

from agent.redact import redact_sensitive_text


def redact_compaction_text(text: str) -> str:
    """Keep fallback's full GitHub-token masking in every historical quote path."""
    redacted = redact_sensitive_text(text or "", force=True, redact_url_credentials=True)
    return re.sub(r"\bgh[pousr]_[A-Za-z0-9_.-]+", "[REDACTED]", redacted)
