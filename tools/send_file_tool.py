"""Send the owner a file, as a file.

An agent could create a document and then only be able to tell the owner where
it was — a `/home/hermes/...` path the owner has no way to act on — and, asked
to send the file itself, say it could not. This is the capability that was
missing.

The tool does not deliver anything by itself. It **publishes**: the file is
validated, copied into the profile's artifact store, given a server-minted id,
and recorded. The turn then carries that id on the message it produces, and the
client renders a file card and downloads by id. That split is deliberate — the
agent decides *what* to send, and the runtime decides how it reaches the owner,
so the same publication works for a 1:1 chat, a group room, or a future client
none of this code knows about.

It deliberately does not accept bytes. Everything an agent wants to send it has
already written to disk, and a base64 round-trip through the model's context
would cost far more than it buys.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)


SEND_FILE_SCHEMA = {
    "name": "send_file",
    "description": (
        "Send a file you have created or can read to the owner as a real "
        "attachment they can open, preview and share. Use this whenever the "
        "owner asks for a file itself — a document, a report, an export, an "
        "image — instead of replying with its path. Give the absolute path of "
        "the file on this machine. The file is registered and delivered as an "
        "attachment on your reply; the owner never sees the path. Credential "
        "and configuration files cannot be sent."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Absolute path of the file to send, e.g. "
                    "'/home/hermes/reports/q3.docx'."
                ),
            },
            "filename": {
                "type": "string",
                "description": (
                    "Optional name to show the owner. Defaults to the file's "
                    "own name. Purely a label — it never affects what is read."
                ),
            },
        },
        "required": ["path"],
    },
}


def _profile_home(kw: dict[str, Any]) -> Path:
    """The profile whose store this publication belongs to.

    Uses the turn's bound `HERMES_HOME`, which the gateway sets per turn, so a
    file published during one agent's turn lands in that agent's store and
    nowhere else.
    """
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home())


def _profile_name(home: Path) -> str:
    # `<root>/profiles/<name>` for a profile; the default profile's home is the
    # root itself.
    parent = home.parent
    return home.name if parent.name == "profiles" else "default"


def send_file_tool(args: dict[str, Any], **kw: Any) -> str:
    """Publish a file and tell the agent what happened.

    **Returns a string**, because that is the tool contract. The registry
    normalises handler results (`_normalize_handler_result`) and accepts only a
    string or the `_multimodal` envelope; a bare dict is refused with
    "Tool handler returned unsupported result type: dict" — which is exactly
    what the first real attempt hit, after the agent had correctly chosen the
    tool and the file had been published. `tool_result` and `tool_error` are the
    canonical helpers and produce the JSON string the pipeline expects.

    The attachment reference does **not** travel in this return value. It goes
    through `note_pending` to `finalize_turn`, which stamps it onto the message
    the turn produces — a tool handler cannot reach the agent, and the model
    does not need to relay a routing id it should never see in prose.
    """
    from gateway import published_artifacts

    path = args.get("path")
    home = _profile_home(kw)

    try:
        record = published_artifacts.publish(
            home,
            source_path=path if isinstance(path, str) else "",
            profile=_profile_name(home),
            session_id=str(kw.get("session_id") or ""),
            filename=args.get("filename"),
        )
    except published_artifacts.PublishRefused as refusal:
        # A sentence the agent can pass on, carrying no path and no traceback:
        # a refusal that quotes the filesystem is its own small disclosure, and
        # the owner cannot act on a stack trace anyway.
        return tool_error(str(refusal), reason=refusal.reason)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("send_file failed: %s", exc)
        return tool_error("The file could not be sent.", reason="failed")

    reference = published_artifacts.message_reference([record])[0]
    # The side channel `finalize_turn` drains. This is how the attachment
    # actually reaches the message; the return value below only tells the agent
    # what it did, so it can describe the file in its own words.
    published_artifacts.note_pending(kw.get("session_id"), reference)
    return tool_result(
        ok=True,
        filename=record.filename,
        size_bytes=record.size_bytes,
        message=(
            f"Attached {record.filename} ({record.size_bytes:,} bytes). "
            "The owner will see it as a file on your reply — do not repeat the "
            "path or a link."
        ),
    )


registry.register(
    name="send_file",
    toolset="file",
    schema=SEND_FILE_SCHEMA,
    handler=send_file_tool,
    emoji="📎",
    description="Send a file to the owner as a real attachment.",
)
