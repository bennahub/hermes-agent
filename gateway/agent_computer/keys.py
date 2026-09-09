"""KeyboardEvent → CDP Input.dispatchKeyEvent.

Never log or return the typed characters. Callers must pass only the
fields needed to synthesize the remote key and must not persist them
in audit.
"""

from __future__ import annotations

from typing import Any

# CDP modifier bitmask: Alt=1, Ctrl=2, Meta=4, Shift=8
_MOD_ALT = 1
_MOD_CTRL = 2
_MOD_META = 4
_MOD_SHIFT = 8

_NAMED: dict[str, dict[str, Any]] = {
    "Enter": {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13},
    "Tab": {"key": "Tab", "code": "Tab", "windowsVirtualKeyCode": 9, "nativeVirtualKeyCode": 9},
    "Backspace": {"key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8, "nativeVirtualKeyCode": 8},
    "Delete": {"key": "Delete", "code": "Delete", "windowsVirtualKeyCode": 46, "nativeVirtualKeyCode": 46},
    "Escape": {"key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27, "nativeVirtualKeyCode": 27},
    "ArrowLeft": {"key": "ArrowLeft", "code": "ArrowLeft", "windowsVirtualKeyCode": 37, "nativeVirtualKeyCode": 37},
    "ArrowUp": {"key": "ArrowUp", "code": "ArrowUp", "windowsVirtualKeyCode": 38, "nativeVirtualKeyCode": 38},
    "ArrowRight": {"key": "ArrowRight", "code": "ArrowRight", "windowsVirtualKeyCode": 39, "nativeVirtualKeyCode": 39},
    "ArrowDown": {"key": "ArrowDown", "code": "ArrowDown", "windowsVirtualKeyCode": 40, "nativeVirtualKeyCode": 40},
    "Home": {"key": "Home", "code": "Home", "windowsVirtualKeyCode": 36, "nativeVirtualKeyCode": 36},
    "End": {"key": "End", "code": "End", "windowsVirtualKeyCode": 35, "nativeVirtualKeyCode": 35},
    "PageUp": {"key": "PageUp", "code": "PageUp", "windowsVirtualKeyCode": 33, "nativeVirtualKeyCode": 33},
    "PageDown": {"key": "PageDown", "code": "PageDown", "windowsVirtualKeyCode": 34, "nativeVirtualKeyCode": 34},
    " ": {"key": " ", "code": "Space", "windowsVirtualKeyCode": 32, "nativeVirtualKeyCode": 32, "text": " "},
}


def modifier_mask(
    *,
    alt: bool = False,
    ctrl: bool = False,
    meta: bool = False,
    shift: bool = False,
) -> int:
    mask = 0
    if alt:
        mask |= _MOD_ALT
    if ctrl:
        mask |= _MOD_CTRL
    if meta:
        mask |= _MOD_META
    if shift:
        mask |= _MOD_SHIFT
    return mask


def is_printable_key(key: str, modifiers: int = 0) -> bool:
    """Ordinary text that should become page characters. Never log ``key``."""
    return len(key) == 1 and not (int(modifiers or 0) & (_MOD_CTRL | _MOD_META | _MOD_ALT))


def cdp_key_params(
    *,
    phase: str,
    key: str,
    code: str = "",
    modifiers: int = 0,
) -> dict[str, Any]:
    """Build one CDP key event. Do not store ``key`` outside this call."""
    named = _NAMED.get(key)
    event_type = "keyDown" if phase == "down" else "keyUp"
    params: dict[str, Any] = {
        "type": event_type,
        "modifiers": int(modifiers or 0),
    }
    if named:
        params.update(named)
        if event_type == "keyUp":
            params.pop("text", None)
        elif key == "Enter" and not (modifiers & (_MOD_CTRL | _MOD_META | _MOD_ALT)):
            # Native form submission and textarea newlines need Chromium's
            # character/default-action path, not only a raw keydown event.
            params.update(text="\r", unmodifiedText="\r")
        elif key != " ":
            params["type"] = "rawKeyDown"
        return params
    params["key"] = key
    params["code"] = code or (f"Key{key.upper()}" if len(key) == 1 and key.isalpha() else code)
    if len(key) == 1 and key.isascii() and key.isalnum():
        params["windowsVirtualKeyCode"] = ord(key.upper())
    if phase == "down" and key.lower() == "a" and modifiers & (_MOD_CTRL | _MOD_META):
        params["commands"] = ["selectAll"]
    if event_type == "keyDown" and is_printable_key(key, modifiers):
        params["text"] = key
        params["type"] = "char"
        params["unmodifiedText"] = key
    return params
