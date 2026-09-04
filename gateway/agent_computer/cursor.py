"""Map remote CSS cursor values onto the Owner surface.

Unknown or debug values become a normal arrow. Never expose crosshair
as the product default.
"""

from __future__ import annotations

_ALLOWED = {
    "default",
    "auto",
    "pointer",
    "text",
    "vertical-text",
    "not-allowed",
    "no-drop",
    "grab",
    "grabbing",
    "col-resize",
    "row-resize",
    "ew-resize",
    "ns-resize",
    "nesw-resize",
    "nwse-resize",
    "move",
    "wait",
    "progress",
    "help",
    "cell",
    "copy",
    "alias",
    "context-menu",
}

_ALIASES = {
    "auto": "default",
    "vertical-text": "text",
    "no-drop": "not-allowed",
}


def map_remote_cursor(value: str) -> str:
    """Return a CSS cursor safe to apply on the Owner canvas."""
    raw = (value or "").split(",")[0].strip().lower()
    if raw.startswith("url(") or raw in ("", "none", "crosshair"):
        return "default"
    if raw not in _ALLOWED:
        return "default"
    return _ALIASES.get(raw, raw)


def cursor_probe_expression(x: float, y: float) -> str:
    """Runtime.evaluate expression. Coordinates only — never page text."""
    return (
        "(() => { const el = document.elementFromPoint("
        f"{float(x)}, {float(y)}"
        "); if (!el) return 'default';"
        " return getComputedStyle(el).cursor || 'default'; })()"
    )
