"""Owner-visible location from Chromium truth.

Never leak filesystem paths, CDP endpoints, or profile dirs.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from .adapter import safe_workspace_url


def public_location(url: str, title: str = "") -> dict[str, object]:
    """Safe origin/URL for the takeover chrome. Server/Chromium truth only."""
    raw = (url or "").strip()
    title = (title or "").strip()
    if raw.startswith("file:"):
        name = Path(urlparse(raw).path).name or "fixture"
        return {
            "url": f"fixture://{name}",
            "origin": "fixture://local",
            "https": False,
            "title": title,
            "scheme": "fixture",
        }
    if raw.startswith(("http://", "https://")):
        parsed = urlparse(raw)
        host = parsed.hostname or ""
        if ":" in host:
            host = "[" + host + "]"
        try:
            if parsed.port is not None:
                host += ":" + str(parsed.port)
        except ValueError:
            return {"url": "", "origin": "", "https": False, "title": "", "scheme": ""}
        raw = parsed._replace(netloc=host).geturl()
        origin = f"{parsed.scheme}://{host}" if host else parsed.scheme + "://"
        return {
            "url": raw,
            "origin": origin,
            "https": parsed.scheme == "https",
            "title": title,
            "scheme": parsed.scheme,
        }
    if raw.startswith("about:"):
        return {
            "url": "about:blank",
            "origin": "about:blank",
            "https": False,
            "title": title,
            "scheme": "about",
        }
    return {
        "url": "",
        "origin": "",
        "https": False,
        "title": title,
        "scheme": "",
    }


def safe_navigate_url(url: str) -> str:
    """http(s) or file fixture only."""
    return safe_workspace_url(url)
