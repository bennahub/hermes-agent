"""Screenshot ↔ viewport coordinate mapping for Human Takeover.

``pointer_click(x, y)`` is accepted in the last-observed screenshot pixel
space. Chromium ``Input.dispatchMouseEvent`` uses CSS viewport pixels.
When screenshot and viewport sizes match (the designed headless config:
deviceScaleFactor=1), the mapping is identity.
"""

from __future__ import annotations


def jpeg_dimensions(data: bytes) -> tuple[int, int]:
    """Return (width, height) from a JPEG SOF marker. (0, 0) if unknown."""
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return 0, 0
    i = 2
    while i + 8 < len(data):
        if data[i] != 0xFF:
            break
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):
            height = int.from_bytes(data[i + 5 : i + 7], "big")
            width = int.from_bytes(data[i + 7 : i + 9], "big")
            return width, height
        if marker in (0xD8, 0xD9):
            i += 2
            continue
        seglen = int.from_bytes(data[i + 2 : i + 4], "big")
        if seglen < 2:
            break
        i += 2 + seglen
    return 0, 0


def map_screenshot_to_viewport(
    x: float,
    y: float,
    *,
    screenshot_width: int,
    screenshot_height: int,
    viewport_width: int,
    viewport_height: int,
) -> tuple[float, float]:
    """Map a screenshot pixel to CSS viewport coordinates.

    Identity when sizes match or either side is missing. Otherwise a
    deterministic uniform scale: ``viewport = screenshot * (vp / shot)``.
    """
    if (
        screenshot_width <= 0
        or screenshot_height <= 0
        or viewport_width <= 0
        or viewport_height <= 0
        or (screenshot_width == viewport_width and screenshot_height == viewport_height)
    ):
        return float(x), float(y)
    return (
        float(x) * viewport_width / screenshot_width,
        float(y) * viewport_height / screenshot_height,
    )


def map_client_to_viewport(
    x: float,
    y: float,
    *,
    client_width: int,
    client_height: int,
    viewport_width: int,
    viewport_height: int,
) -> tuple[float, float]:
    """Map CSS pixels on the Owner surface to Chromium viewport pixels.

    Uses the displayed content box, not CSS upscaling of a smaller source.
    Identity when sizes match or either side is missing.
    """
    if (
        client_width <= 0
        or client_height <= 0
        or viewport_width <= 0
        or viewport_height <= 0
        or (client_width == viewport_width and client_height == viewport_height)
    ):
        return float(x), float(y)
    return (
        float(x) * viewport_width / client_width,
        float(y) * viewport_height / client_height,
    )


def map_owner_pointer(
    x: float,
    y: float,
    *,
    displayed_width: float,
    displayed_height: float,
    viewport_width: int,
    viewport_height: int,
    frame_width: int = 0,
    frame_height: int = 0,
) -> tuple[float, float]:
    """Map a click on the displayed remote frame to Chromium CSS pixels.

    ``x`` / ``y`` are CSS pixels inside the painted canvas box (origin at the
    canvas top-left). Letterbox around the canvas is not part of this box.
    When the JPEG bitmap size is known and differs from the viewport, the
    path is display → bitmap → viewport. Otherwise display → viewport.
    """
    dw = float(displayed_width or 0)
    dh = float(displayed_height or 0)
    if dw <= 0 or dh <= 0 or viewport_width <= 0 or viewport_height <= 0:
        return float(x), float(y)
    fx = int(frame_width or 0)
    fy = int(frame_height or 0)
    if fx > 0 and fy > 0:
        return map_screenshot_to_viewport(
            float(x) * fx / dw,
            float(y) * fy / dh,
            screenshot_width=fx,
            screenshot_height=fy,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
        )
    return map_client_to_viewport(
        float(x),
        float(y),
        client_width=max(1, int(round(dw))),
        client_height=max(1, int(round(dh))),
        viewport_width=viewport_width,
        viewport_height=viewport_height,
    )


def mapping_kind(
    screenshot_width: int,
    screenshot_height: int,
    viewport_width: int,
    viewport_height: int,
) -> str:
    if screenshot_width <= 0 or viewport_width <= 0:
        return "unknown"
    if screenshot_width == viewport_width and screenshot_height == viewport_height:
        return "1:1"
    return "scale"