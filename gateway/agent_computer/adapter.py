"""Computer runtime adapter.

Reuses the existing Hermes Chromium/CDP launch shape from
``tools.browser_tool._real_profile_cdp`` (real binary, ``--user-data-dir``,
loopback DevTools port). It does **not** call ``snapshot_real_profile``:
that path re-syncs the OS last-used profile and would destroy an
agent-owned BrowserIdentity.

Tests use ``InMemoryRuntime``. The Chromium adapter is opt-in when a
binary exists; production takeover still goes through this same handle,
not a second browser.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .models import AgentComputer, BrowserIdentity, Observation
from .pointer import jpeg_dimensions, map_screenshot_to_viewport


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def chromium_needs_sandbox_bypass() -> bool:
    """Same conditions as ``tools.browser_tool._needs_chromium_sandbox_bypass``.

    Hosted Linux with AppArmor userns restrictions cannot start Chromium
    without ``--no-sandbox``. Reuse that existing Hermes rule; do not invent
    a second sandbox policy.
    """
    import os

    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return True
    if Path("/.dockerenv").exists():
        return True
    userns_restrict = "/proc/sys/kernel/apparmor_restrict_unprivileged_userns"
    try:
        if Path(userns_restrict).read_text(encoding="utf-8").strip() == "1":
            return True
    except OSError:
        pass
    return False


def read_devtools_port(user_data: str) -> int | None:
    """Read Chromium's loopback DevTools port from a user-data-dir."""
    try:
        line = Path(user_data).joinpath("DevToolsActivePort").read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError):
        return None
    return int(line) if line.isdigit() else None


def chromium_launch_argv(
    binary: str,
    user_data: str,
    *,
    sandbox_bypass: bool | None = None,
    extra_args: list[str] | None = None,
) -> list[str]:
    """Loopback-only Chromium argv for a BrowserIdentity user-data-dir."""
    import os

    argv = [
        binary,
        f"--user-data-dir={user_data}",
        "--remote-debugging-port=0",
        "--remote-debugging-address=127.0.0.1",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-background-networking",
        "--disable-default-apps",
        "--force-device-scale-factor=1",
        "--window-size=1440,900",
        "--headless=new",
    ]
    extras = [item.strip() for item in (extra_args or []) if item and item.strip()]
    env_args = (
        os.environ.get("AGENT_BROWSER_ARGS")
        or os.environ.get("AGENT_BROWSER_CHROME_FLAGS")
        or ""
    )
    extras.extend(item.strip() for item in env_args.split(",") if item.strip())
    if sandbox_bypass is None:
        sandbox_bypass = chromium_needs_sandbox_bypass()
    if sandbox_bypass:
        if "--no-sandbox" not in extras:
            extras.append("--no-sandbox")
        if "--disable-dev-shm-usage" not in extras:
            extras.append("--disable-dev-shm-usage")
    argv.extend(extras)
    argv.append("about:blank")
    return argv


@dataclass
class RuntimeHandle:
    computer_id: str
    identity_id: str | None
    user_data_dir: str
    cdp_loopback: str | None = None
    process_id: int | None = None
    backend: str = "in_memory"
    headed_same_host: bool = False
    last_pointer_x: float = 0.0
    last_pointer_y: float = 0.0
    workspace_root: str = ""
    screenshot_width: int = 0
    screenshot_height: int = 0
    viewport_width: int = 0
    viewport_height: int = 0
    target_id: str | None = None


class ComputerRuntime(Protocol):
    def wake(self, computer: AgentComputer, identity: BrowserIdentity | None) -> RuntimeHandle: ...

    def observe(self, handle: RuntimeHandle) -> Observation: ...

    def act(
        self,
        handle: RuntimeHandle,
        *,
        kind: str,
        target: str = "",
        text: str = "",
        action_class: str = "",
        x: float | None = None,
        y: float | None = None,
        key: str = "",
        code: str = "",
        delta_x: float = 0,
        delta_y: float = 0,
    ) -> Observation: ...

    def sleep(self, handle: RuntimeHandle) -> None: ...

    def alive(self, handle: RuntimeHandle) -> bool: ...


@dataclass
class _Page:
    url: str = "about:blank"
    title: str = ""
    text: str = ""
    cookies: dict[str, str] = field(default_factory=dict)
    last_x: float = 0.0
    last_y: float = 0.0
    input_value: str = ""
    history: list[str] = field(default_factory=list)
    forward: list[str] = field(default_factory=list)


class InMemoryRuntime:
    """Shared mutable page per identity/computer. Same object = same environment."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._pages: dict[str, _Page] = {}
        self._alive: dict[str, bool] = {}

    def _key(self, computer: AgentComputer, identity: BrowserIdentity | None) -> str:
        if identity:
            return f"id:{identity.id}"
        return f"pc:{computer.id}"

    def _page(self, key: str) -> _Page:
        page = self._pages.get(key)
        if page is None:
            page = _Page()
            self._pages[key] = page
        return page

    def wake(self, computer: AgentComputer, identity: BrowserIdentity | None) -> RuntimeHandle:
        key = self._key(computer, identity)
        with self._lock:
            self._alive[key] = True
            page = self._page(key)
            if computer.workspace_url and page.url == "about:blank":
                page.url = computer.workspace_url
                page.title = computer.workspace_title
        return RuntimeHandle(
            computer_id=computer.id,
            identity_id=identity.id if identity else None,
            user_data_dir=identity.profile_ref if identity else computer.persistence_ref,
            cdp_loopback=None,
            backend="in_memory",
            workspace_root=str(Path(computer.persistence_ref) / "workspace"),
        )

    def ensure_workspace(self, handle: RuntimeHandle, workspace_url: str) -> None:
        target = safe_workspace_url(workspace_url)
        if not target:
            return
        page = self._page(f"id:{handle.identity_id}" if handle.identity_id else f"pc:{handle.computer_id}")
        if page_needs_restore(page.url):
            page.url = target

    def observe(self, handle: RuntimeHandle) -> Observation:
        key = f"id:{handle.identity_id}" if handle.identity_id else f"pc:{handle.computer_id}"
        with self._lock:
            page = self._page(key)
            return Observation(
                url=page.url,
                title=page.title,
                text=page.text,
                fencing_epoch=0,
                controller="",
                observed_at=_now(),
                viewport_width=handle.viewport_width or 1440,
                viewport_height=handle.viewport_height or 900,
            )

    def act(
        self,
        handle: RuntimeHandle,
        *,
        kind: str,
        target: str = "",
        text: str = "",
        action_class: str = "",
        x: float | None = None,
        y: float | None = None,
        key: str = "",
        code: str = "",
        delta_x: float = 0,
        delta_y: float = 0,
    ) -> Observation:
        page_key = f"id:{handle.identity_id}" if handle.identity_id else f"pc:{handle.computer_id}"
        with self._lock:
            page = self._page(page_key)
            if kind == "navigate":
                page.url = target
                page.title = target
                page.text = f"opened {target}"
            elif kind == "type":
                page.input_value = text
                page.text = (page.text + " typed").strip()
            elif kind == "click":
                page.text = (page.text + f" clicked:{target}").strip()
            elif kind == "pointer_move":
                page.last_x = float(x or 0)
                page.last_y = float(y or 0)
            elif kind == "pointer_click":
                page.last_x = float(x or 0)
                page.last_y = float(y or 0)
                if 400 <= page.last_x <= 600 and 160 <= page.last_y <= 208:
                    page.text = "owner-pixel-clicked"
                elif 16 <= page.last_x <= 216 and 160 <= page.last_y <= 208:
                    page.text = "agent-ready"
                else:
                    page.text = (page.text + f" pixel:{int(page.last_x)},{int(page.last_y)}").strip()
            elif kind == "text":
                page.input_value = text
                if text:
                    page.text = (page.text + " " + text).strip()
            elif kind == "scroll":
                page.text = (page.text + " scrolled").strip()
            elif kind == "key":
                page.text = (page.text + f" key:{key or code}").strip()
            elif kind == "upload":
                page.text = (page.text + f" uploaded:{text}").strip()
            elif kind == "set_cookie":
                # Test-only durable auth stand-in. Never returned to clients.
                page.cookies[target] = text
                page.text = (page.text + f" auth:{target}").strip()
            else:
                raise ValueError(f"unsupported computer action: {kind}")
            return Observation(
                url=page.url,
                title=page.title,
                text=page.text,
                fencing_epoch=0,
                controller="",
                observed_at=_now(),
                viewport_width=handle.viewport_width or 1440,
                viewport_height=handle.viewport_height or 900,
            )

    def stream_pointer(
        self,
        handle: RuntimeHandle,
        *,
        phase: str,
        x: float,
        y: float,
        click_count: int = 1,
        buttons: int = 0,
    ) -> None:
        _ = buttons
        if phase == "move":
            self.act(handle, kind="pointer_move", x=x, y=y)
        else:
            self.act(handle, kind="pointer_click", x=x, y=y)
            if click_count > 1:
                self.act(handle, kind="pointer_click", x=x, y=y)

    def stream_wheel(
        self,
        handle: RuntimeHandle,
        *,
        x: float,
        y: float,
        delta_x: float,
        delta_y: float,
    ) -> None:
        self.act(handle, kind="scroll", x=x, y=y, delta_x=delta_x, delta_y=delta_y)

    def stream_key(
        self,
        handle: RuntimeHandle,
        *,
        phase: str,
        key: str,
        code: str = "",
        modifiers: int = 0,
    ) -> None:
        _ = modifiers
        if phase == "down":
            self.act(handle, kind="key", key=key, code=code)

    def stream_nav(self, handle: RuntimeHandle, action: str, url: str = "") -> dict[str, str]:
        page = self._page(f"id:{handle.identity_id}" if handle.identity_id else f"pc:{handle.computer_id}")
        if action == "open":
            target = safe_workspace_url(url)
            if target:
                page.history.append(page.url)
                page.forward.clear()
                page.url = target
                page.title = target
        elif action == "back" and page.history:
            page.forward.append(page.url)
            page.url = page.history.pop()
        elif action == "forward" and page.forward:
            page.history.append(page.url)
            page.url = page.forward.pop()
        elif action == "reload":
            page.title = page.title or page.url
        return {"url": page.url, "title": page.title}

    def probe_cursor(self, handle: RuntimeHandle, x: float, y: float) -> str:
        page = self._page(f"id:{handle.identity_id}" if handle.identity_id else f"pc:{handle.computer_id}")
        _ = page
        if 80 <= x <= 400 and 80 <= y <= 200:
            return "pointer"
        if 80 <= x <= 560 and 220 <= y <= 268:
            return "text"
        return "default"

    def apply_viewport(self, handle: RuntimeHandle, width: int, height: int) -> None:
        handle.viewport_width = int(width)
        handle.viewport_height = int(height)

    def sleep(self, handle: RuntimeHandle) -> None:
        key = f"id:{handle.identity_id}" if handle.identity_id else f"pc:{handle.computer_id}"
        with self._lock:
            self._alive[key] = False

    def alive(self, handle: RuntimeHandle) -> bool:
        key = f"id:{handle.identity_id}" if handle.identity_id else f"pc:{handle.computer_id}"
        with self._lock:
            return bool(self._alive.get(key))

    def cookies_for_test(self, identity_id: str) -> dict[str, str]:
        with self._lock:
            return dict(self._page(f"id:{identity_id}").cookies)


class HermesChromiumRuntime:
    """Launch the host Chromium on an identity-owned user-data-dir.

    Launch flags match ``_real_profile_cdp`` (loopback debug port, no
    mock-keychain). Does not snapshot the OS default profile.
    """

    def __init__(self) -> None:
        self._procs: dict[str, Any] = {}

    def wake(self, computer: AgentComputer, identity: BrowserIdentity | None) -> RuntimeHandle:
        from hermes_cli.browser_connect import chromium_executable, detect_default_chromium

        import os

        override = os.environ.get("AGENT_BROWSER_EXECUTABLE_PATH", "").strip()
        binary = override if override and Path(override).is_file() else ""
        if not binary:
            browser = detect_default_chromium() or "chromium"
            binary = chromium_executable(browser) or ""
        if not binary:
            raise RuntimeError("no Chromium-family binary on this host")
        user_data = identity.profile_ref if identity else computer.persistence_ref
        Path(user_data).mkdir(parents=True, exist_ok=True)
        # Import locally so unit tests never spawn Chrome.
        import subprocess
        import time

        user_data = str(Path(user_data).resolve())
        self._reap_dead_profile_browser(user_data, computer.id)
        port_file = os.path.join(user_data, "DevToolsActivePort")
        try:
            os.unlink(port_file)
        except OSError:
            pass
        downloads = Path(computer.persistence_ref) / "workspace" / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        argv = chromium_launch_argv(
            binary,
            user_data,
            extra_args=[f"--download-default-directory={downloads.resolve()}"],
        )
        stderr_path = Path(user_data) / "chromium.stderr"
        try:
            stderr_fh = stderr_path.open("w", encoding="utf-8")
        except OSError:
            stderr_fh = subprocess.DEVNULL
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=stderr_fh,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        if stderr_fh is not subprocess.DEVNULL:
            stderr_fh.close()
        deadline = time.monotonic() + 30
        port = None
        while time.monotonic() < deadline:
            try:
                with open(port_file, encoding="utf-8") as fh:
                    line = fh.readline().strip()
                if line.isdigit():
                    port = int(line)
                    break
            except OSError:
                pass
            if proc.poll() is not None:
                hint = ""
                try:
                    hint = Path(user_data).joinpath("chromium.stderr").read_text(encoding="utf-8")[-400:]
                except OSError:
                    hint = ""
                raise RuntimeError(f"chromium exited during startup {hint}".strip())
            time.sleep(0.2)
        if port is None:
            proc.terminate()
            raise RuntimeError("chromium did not expose a loopback debug port")
        try:
            Path(user_data).joinpath("chromium.pid").write_text(str(proc.pid), encoding="utf-8")
        except OSError:
            pass
        self._procs[computer.id] = proc
        handle = RuntimeHandle(
            computer_id=computer.id,
            identity_id=identity.id if identity else None,
            user_data_dir=user_data,
            cdp_loopback=f"http://127.0.0.1:{port}",
            process_id=proc.pid,
            backend="hermes_chromium",
            headed_same_host=False,
            workspace_root=str(Path(computer.persistence_ref) / "workspace"),
        )
        try:
            loopback_cdp(
                handle,
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": 1440,
                    "height": 900,
                    "deviceScaleFactor": 1,
                    "mobile": False,
                },
            )
            handle.viewport_width = 1440
            handle.viewport_height = 900
        except Exception:
            pass
        downloads = Path(handle.workspace_root) / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        try:
            loopback_cdp(handle, "Page.enable", {})
        except Exception:
            pass
        try:
            loopback_cdp(
                handle,
                "Browser.setDownloadBehavior",
                {
                    "behavior": "allow",
                    "downloadPath": str(downloads.resolve()),
                    "eventsEnabled": True,
                },
            )
        except Exception:
            pass
        try:
            loopback_cdp(
                handle,
                "Page.setDownloadBehavior",
                {
                    "behavior": "allow",
                    "downloadPath": str(downloads.resolve()),
                },
            )
        except Exception:
            pass
        try:
            self.ensure_workspace(handle, computer.workspace_url)
        except Exception:
            pass
        return handle

    def _reap_dead_profile_browser(self, user_data: str, computer_id: str) -> None:
        """Reap a leftover Chromium that still holds this profile after CDP died.

        ``hermes-serve`` restarts leave chrome in the cgroup. Attach then
        fails (``DevToolsActivePort`` present, TCP dead) and the next
        ``wake()`` waits 30s on SingletonLock without exposing a port —
        which blocked the Owner live-view handshake.
        """
        port = read_devtools_port(user_data)
        pid = None
        try:
            raw = Path(user_data).joinpath("chromium.pid").read_text(encoding="utf-8").strip()
            if raw.isdigit():
                pid = int(raw)
        except OSError:
            pid = None
        if port:
            probe = RuntimeHandle(
                computer_id=computer_id,
                identity_id=None,
                user_data_dir=user_data,
                cdp_loopback=f"http://127.0.0.1:{port}",
                process_id=pid,
                backend="hermes_chromium",
                headed_same_host=False,
                workspace_root=str(Path(user_data) / "workspace"),
            )
            if self.alive(probe):
                return
        if not pid:
            return
        try:
            self.sleep(
                RuntimeHandle(
                    computer_id=computer_id,
                    identity_id=None,
                    user_data_dir=user_data,
                    cdp_loopback=None,
                    process_id=pid,
                    backend="hermes_chromium",
                    headed_same_host=False,
                    workspace_root=str(Path(user_data) / "workspace"),
                )
            )
        except Exception:
            pass

    def attach(self, computer: AgentComputer, identity: BrowserIdentity | None) -> RuntimeHandle | None:
        """Reconnect to an already-running loopback Chromium for this identity."""
        user_data = identity.profile_ref if identity else computer.persistence_ref
        port = read_devtools_port(user_data)
        if port is None:
            return None
        pid = None
        try:
            raw = Path(user_data).joinpath("chromium.pid").read_text(encoding="utf-8").strip()
            if raw.isdigit():
                pid = int(raw)
        except OSError:
            pid = None
        handle = RuntimeHandle(
            computer_id=computer.id,
            identity_id=identity.id if identity else None,
            user_data_dir=str(Path(user_data).resolve()),
            cdp_loopback=f"http://127.0.0.1:{port}",
            process_id=pid,
            backend="hermes_chromium",
            headed_same_host=False,
            workspace_root=str(Path(computer.persistence_ref) / "workspace"),
        )
        if not self.alive(handle):
            return None
        return handle

    def ensure_workspace(self, handle: RuntimeHandle, workspace_url: str) -> None:
        target = safe_workspace_url(workspace_url)
        if not target:
            return
        current = "about:blank"
        try:
            result = loopback_cdp(
                handle,
                "Runtime.evaluate",
                {"expression": "location.href", "returnByValue": True},
            ) or {}
            current = str(((result.get("result") or {}).get("value")) or "about:blank")
        except Exception:
            current = "about:blank"
        if not page_needs_restore(current):
            return
        import time

        loopback_cdp(handle, "Page.enable", {})
        loopback_cdp(handle, "Page.navigate", {"url": target})
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            try:
                result = loopback_cdp(
                    handle,
                    "Runtime.evaluate",
                    {"expression": "location.href", "returnByValue": True},
                ) or {}
                href = str(((result.get("result") or {}).get("value")) or "")
                if href.startswith(("https://", "http://", "file://")):
                    try:
                        loopback_cdp(
                            handle,
                            "Emulation.setDeviceMetricsOverride",
                            {
                                "width": 1440,
                                "height": 900,
                                "deviceScaleFactor": 1,
                                "mobile": False,
                            },
                        )
                        handle.viewport_width = 1440
                        handle.viewport_height = 900
                    except Exception:
                        pass
                    return
            except Exception:
                pass
            time.sleep(0.2)

    def observe(self, handle: RuntimeHandle) -> Observation:
        page = loopback_cdp(handle, "Runtime.evaluate", {
            "expression": (
                "({url: location.href, title: document.title, "
                "text: document.body ? document.body.innerText.slice(0, 2000) : '', "
                "viewportWidth: window.innerWidth, viewportHeight: window.innerHeight, "
                "devicePixelRatio: window.devicePixelRatio || 1})"
            ),
            "returnByValue": True,
        })
        value = ((page or {}).get("result") or {}).get("value") or {}
        shot = {}
        try:
            shot = loopback_cdp(
                handle,
                "Page.captureScreenshot",
                {"format": "jpeg", "quality": 80},
            ) or {}
        except Exception:
            shot = {}
        raw_b64 = str(shot.get("data") or "")
        shot_w = shot_h = 0
        if raw_b64:
            try:
                import base64

                shot_w, shot_h = jpeg_dimensions(base64.b64decode(raw_b64))
            except Exception:
                shot_w = shot_h = 0
        vp_w = int(value.get("viewportWidth") or 0)
        vp_h = int(value.get("viewportHeight") or 0)
        handle.screenshot_width = shot_w
        handle.screenshot_height = shot_h
        # Keep the designed source viewport. innerHeight on a restored tab
        # is often 757 — writing that back made stream clicks remapped.
        if handle.viewport_width <= 0:
            handle.viewport_width = vp_w or 1440
        if handle.viewport_height <= 0:
            handle.viewport_height = vp_h or 900
        return Observation(
            url=str(value.get("url") or ""),
            title=str(value.get("title") or ""),
            text=str(value.get("text") or ""),
            fencing_epoch=0,
            controller="",
            observed_at=_now(),
            screenshot_b64=raw_b64,
            screenshot_mime="image/jpeg" if raw_b64 else "",
            screenshot_width=shot_w,
            screenshot_height=shot_h,
            viewport_width=vp_w,
            viewport_height=vp_h,
            device_pixel_ratio=float(value.get("devicePixelRatio") or 1),
        )

    def act(self, handle: RuntimeHandle, **kwargs: Any) -> Observation:
        import time

        kind = str(kwargs.get("kind") or "")
        target = str(kwargs.get("target") or "")
        text = str(kwargs.get("text") or "")
        key = str(kwargs.get("key") or "")
        code = str(kwargs.get("code") or "")
        x = kwargs.get("x")
        y = kwargs.get("y")
        delta_x = float(kwargs.get("delta_x") or 0)
        delta_y = float(kwargs.get("delta_y") or 0)
        if kind == "navigate":
            loopback_cdp(handle, "Page.navigate", {"url": target})
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                obs = self.observe(handle)
                if target in obs.url or (obs.url and obs.url != "about:blank"):
                    return obs
                time.sleep(0.2)
        elif kind in ("type", "text"):
            if target:
                loopback_cdp(
                    handle,
                    "Runtime.evaluate",
                    {
                        "expression": (
                            f"(() => {{ const el = document.querySelector({target!r}); "
                            "if (!el) return false; el.focus(); return true; }})()"
                        ),
                        "userGesture": True,
                    },
                )
            else:
                loopback_cdp(
                    handle,
                    "Runtime.evaluate",
                    {
                        "expression": (
                            "(() => { const cur = document.activeElement; "
                            "if (cur && cur !== document.body && cur !== document.documentElement "
                            "&& ('value' in cur || cur.isContentEditable)) { cur.focus(); return 'keep'; } "
                            "const el = document.querySelector('input:not([type=hidden]):not([type=file]):not([type=button]):not([type=submit]),textarea,[contenteditable=true]'); "
                            "if (!el) return false; el.focus(); return 'first'; })()"
                        ),
                        "userGesture": True,
                    },
                )
            loopback_cdp(handle, "Input.insertText", {"text": text})
            loopback_cdp(
                handle,
                "Runtime.evaluate",
                {
                    "expression": (
                        "(() => { const el = "
                        + (f"document.querySelector({target!r}) || " if target else "")
                        + "document.activeElement; if (!el || el === document.body) return false; "
                        "if ('value' in el && !(el.value || '').includes("
                        + repr(text)
                        + ")) el.value = (el.value || '') + "
                        + repr(text)
                        + "; el.dispatchEvent(new Event('input', {bubbles:true})); "
                        "el.dispatchEvent(new Event('change', {bubbles:true})); "
                        "return true; })()"
                    ),
                    "userGesture": True,
                },
            )
        elif kind == "click":
            clicked = False
            if target:
                box = loopback_cdp(
                    handle,
                    "Runtime.evaluate",
                    {
                        "expression": (
                            f"(() => {{ const el = document.querySelector({target!r}); "
                            "if (!el) return null; el.scrollIntoView({{block:'center'}}); "
                            "el.focus(); const r = el.getBoundingClientRect(); "
                            "return {{x: r.x + r.width/2, y: r.y + r.height/2}}; }})()"
                        ),
                        "returnByValue": True,
                        "userGesture": True,
                    },
                ) or {}
                point = ((box.get("result") or {}).get("value")) or {}
                if isinstance(point, dict) and point.get("x") is not None:
                    vx, vy = float(point["x"]), float(point["y"])
                    self._mouse(handle, "mousePressed", vx, vy, click_count=1)
                    self._mouse(handle, "mouseReleased", vx, vy, click_count=1)
                    clicked = True
            if not clicked:
                expr = (
                    f"document.querySelector({target!r})?.click()"
                    if target
                    else "undefined"
                )
                loopback_cdp(
                    handle,
                    "Runtime.evaluate",
                    {"expression": expr, "userGesture": True},
                )
        elif kind == "pointer_move":
            vx, vy = self._viewport_point(handle, x, y)
            self._mouse(handle, "mouseMoved", vx, vy)
        elif kind == "pointer_click":
            vx, vy = self._viewport_point(handle, x, y)
            self._mouse(handle, "mousePressed", vx, vy, click_count=1)
            self._mouse(handle, "mouseReleased", vx, vy, click_count=1)
        elif kind == "scroll":
            vx = handle.last_pointer_x or (handle.viewport_width / 2 if handle.viewport_width else 400)
            vy = handle.last_pointer_y or (handle.viewport_height / 2 if handle.viewport_height else 300)
            if x is not None and y is not None:
                vx, vy = self._viewport_point(handle, x, y)
            loopback_cdp(
                handle,
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseWheel",
                    "x": vx,
                    "y": vy,
                    "deltaX": delta_x,
                    "deltaY": delta_y,
                },
            )
        elif kind == "key":
            name = key or code
            loopback_cdp(
                handle,
                "Input.dispatchKeyEvent",
                {"type": "keyDown", "key": name, "code": code or name},
            )
            loopback_cdp(
                handle,
                "Input.dispatchKeyEvent",
                {"type": "keyUp", "key": name, "code": code or name},
            )
        elif kind == "upload":
            if not target or not text:
                raise ValueError("upload requires target selector and workspace filename")
            root = Path(handle.workspace_root or ".").resolve()
            name = Path(text).name
            if not name or name != text:
                raise ValueError("upload filename must be a workspace basename")
            chosen = None
            for folder in ("uploads", "downloads"):
                candidate = (root / folder / name).resolve()
                try:
                    candidate.relative_to(root)
                except ValueError:
                    continue
                if candidate.is_file():
                    chosen = candidate
                    break
            if chosen is None:
                raise ValueError("authorized workspace file not found")
            cdp_set_file_input(handle, target, chosen)
        else:
            raise ValueError(f"unsupported computer action: {kind}")
        if kind == "click" and target:
            href = loopback_cdp(
                handle,
                "Runtime.evaluate",
                {
                    "expression": f"document.querySelector({target!r})?.href || location.href",
                    "returnByValue": True,
                },
            ) or {}
            self._maybe_save_loopback_download(
                handle, str(((href.get("result") or {}).get("value") or ""))
            )
        obs = self.observe(handle)
        self._maybe_save_loopback_download(handle, obs.url)
        return obs

    def _maybe_save_loopback_download(self, handle: RuntimeHandle, url: str) -> None:
        """If headless navigated to a loopback file, store it in the workspace."""
        if not url or not url.startswith("http://127.0.0.1:"):
            return
        from urllib.parse import urlparse
        import urllib.request

        parsed = urlparse(url)
        name = Path(parsed.path).name
        suffix = Path(name).suffix.lower()
        if not name or suffix in {"", ".html", ".htm"}:
            return
        root = Path(handle.workspace_root or ".").resolve()
        dest = (root / "downloads" / name).resolve()
        try:
            dest.relative_to(root / "downloads")
        except ValueError:
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                ctype = str(resp.headers.get("Content-Type") or "")
                if "text/html" in ctype:
                    return
                disp = str(resp.headers.get("Content-Disposition") or "")
                if "filename=" in disp:
                    offered = disp.split("filename=", 1)[1].strip().strip('"')
                    offered = Path(offered).name
                    if offered:
                        dest = (root / "downloads" / offered).resolve()
                        dest.relative_to(root / "downloads")
                dest.write_bytes(resp.read())
        except Exception:
            return

    def _viewport_point(self, handle: RuntimeHandle, x: Any, y: Any) -> tuple[float, float]:
        sx = float(x or 0)
        sy = float(y or 0)
        vx, vy = map_screenshot_to_viewport(
            sx,
            sy,
            screenshot_width=handle.screenshot_width,
            screenshot_height=handle.screenshot_height,
            viewport_width=handle.viewport_width,
            viewport_height=handle.viewport_height,
        )
        handle.last_pointer_x = vx
        handle.last_pointer_y = vy
        return vx, vy

    def _mouse(
        self,
        handle: RuntimeHandle,
        event: str,
        x: float,
        y: float,
        *,
        click_count: int = 0,
        buttons: int = 0,
    ) -> None:
        params: dict[str, Any] = {"type": event, "x": x, "y": y, "buttons": buttons}
        if event in ("mousePressed", "mouseReleased"):
            params["button"] = "left"
            params["clickCount"] = click_count or 1
        loopback_cdp(handle, "Input.dispatchMouseEvent", params)

    def stream_pointer(
        self,
        handle: RuntimeHandle,
        *,
        phase: str,
        x: float,
        y: float,
        click_count: int = 1,
        buttons: int = 0,
    ) -> None:
        # Already mapped to Chromium CSS pixels by normalize_owner_event.
        # Do not run screenshot→viewport again — that misses clicks whenever
        # the last JPEG / innerHeight disagrees with the pinned 1440×900.
        vx, vy = float(x), float(y)
        handle.last_pointer_x = vx
        handle.last_pointer_y = vy
        held = 1 if int(buttons or 0) else (1 if phase in ("down", "click") else 0)
        if phase == "move":
            self._mouse(handle, "mouseMoved", vx, vy, buttons=held)
            return
        if phase == "down":
            self._mouse(handle, "mouseMoved", vx, vy, buttons=0)
            self._mouse(handle, "mousePressed", vx, vy, click_count=click_count, buttons=1)
            return
        if phase == "up":
            self._mouse(handle, "mouseReleased", vx, vy, click_count=click_count, buttons=0)
            return
        self._mouse(handle, "mouseMoved", vx, vy, buttons=0)
        self._mouse(handle, "mousePressed", vx, vy, click_count=click_count, buttons=1)
        self._mouse(handle, "mouseReleased", vx, vy, click_count=click_count, buttons=0)

    def stream_wheel(
        self,
        handle: RuntimeHandle,
        *,
        x: float,
        y: float,
        delta_x: float,
        delta_y: float,
    ) -> None:
        vx, vy = float(x), float(y)
        handle.last_pointer_x = vx
        handle.last_pointer_y = vy
        loopback_cdp(
            handle,
            "Input.dispatchMouseEvent",
            {"type": "mouseWheel", "x": vx, "y": vy, "deltaX": delta_x, "deltaY": delta_y},
        )

    def stream_key(
        self,
        handle: RuntimeHandle,
        *,
        phase: str,
        key: str,
        code: str = "",
        modifiers: int = 0,
    ) -> None:
        from .keys import cdp_key_params, is_printable_key

        if phase == "down" and is_printable_key(key, modifiers):
            loopback_cdp(handle, "Input.insertText", {"text": key})
            return
        loopback_cdp(handle, "Input.dispatchKeyEvent", cdp_key_params(phase=phase, key=key, code=code, modifiers=modifiers))

    def current_location(self, handle: RuntimeHandle) -> dict[str, str]:
        try:
            result = loopback_cdp(
                handle,
                "Runtime.evaluate",
                {
                    "expression": "({url: location.href, title: document.title || ''})",
                    "returnByValue": True,
                },
            ) or {}
            value = ((result.get("result") or {}).get("value")) or {}
            return {"url": str(value.get("url") or ""), "title": str(value.get("title") or "")}
        except Exception:
            return {"url": "", "title": ""}

    def stream_nav(self, handle: RuntimeHandle, action: str, url: str = "") -> dict[str, str]:
        import time

        if action in ("back", "forward"):
            history = loopback_cdp(handle, "Page.getNavigationHistory", {})
            entries = history.get("entries") or []
            index = int(history.get("currentIndex", -1)) + (-1 if action == "back" else 1)
            if 0 <= index < len(entries):
                loopback_cdp(handle, "Page.navigateToHistoryEntry", {"entryId": entries[index]["id"]})
        elif action == "reload":
            loopback_cdp(handle, "Page.reload", {})
        elif action == "open":
            target = safe_workspace_url(url)
            if target:
                loopback_cdp(handle, "Page.navigate", {"url": target})
        time.sleep(0.35)
        try:
            loopback_cdp(
                handle,
                "Emulation.setDeviceMetricsOverride",
                {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False},
            )
            handle.viewport_width = 1440
            handle.viewport_height = 900
        except Exception:
            pass
        return self.current_location(handle)

    def probe_cursor(self, handle: RuntimeHandle, x: float, y: float) -> str:
        from .cursor import cursor_probe_expression

        result = loopback_cdp(
            handle,
            "Runtime.evaluate",
            {"expression": cursor_probe_expression(x, y), "returnByValue": True},
        ) or {}
        value = ((result.get("result") or {}).get("value"))
        return str(value or "default")

    def apply_viewport(self, handle: RuntimeHandle, width: int, height: int) -> None:
        handle.viewport_width = int(width)
        handle.viewport_height = int(height)
        try:
            loopback_cdp(
                handle,
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": handle.viewport_width,
                    "height": handle.viewport_height,
                    "deviceScaleFactor": 1,
                    "mobile": False,
                },
            )
        except Exception:
            pass

    def alive(self, handle: RuntimeHandle) -> bool:
        import urllib.request

        if not handle.cdp_loopback or not handle.cdp_loopback.startswith("http://127.0.0.1:"):
            return False
        try:
            with urllib.request.urlopen(handle.cdp_loopback.rstrip("/") + "/json/version", timeout=1):
                return True
        except Exception:
            return False

    def sleep(self, handle: RuntimeHandle) -> None:
        """Stop the Chromium process tree and wait until it is reaped.

        Process/CDP/DOM are ephemeral. The managed user-data-dir stays.
        """
        proc = self._procs.pop(handle.computer_id, None)
        pid = handle.process_id or (proc.pid if proc is not None else None)
        if not pid:
            return
        if proc is not None and proc.poll() is not None:
            return
        # Persisted PIDs can be reused after a service/host restart. A stale
        # marker never authorizes terminating an unrelated process tree.
        if proc is None:
            try:
                import psutil

                args = psutil.Process(pid).cmdline()
                if f"--user-data-dir={handle.user_data_dir}" not in args:
                    return
            except (psutil.Error, OSError):
                return
        import os
        import signal
        import time

        children: list[Any] = []
        try:
            import psutil

            parent = psutil.Process(pid)
            children = parent.children(recursive=True)
            for child in children:
                try:
                    child.terminate()
                except psutil.Error:
                    pass
            try:
                parent.terminate()
            except psutil.Error:
                pass
            psutil.wait_procs([parent, *children], timeout=6)
            still = []
            for proc_ref in [parent, *children]:
                try:
                    if proc_ref.is_running():
                        proc_ref.kill()
                        still.append(proc_ref)
                except psutil.Error:
                    pass
            if still:
                psutil.wait_procs(still, timeout=3)
        except Exception:
            try:
                os.killpg(pid, signal.SIGTERM)
            except OSError:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            if proc is not None:
                try:
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    try:
                        proc.wait(timeout=3)
                    except Exception:
                        pass
        if proc is not None:
            try:
                proc.wait(timeout=1)
            except Exception:
                pass
        # Reap a possible zombie so os.kill(pid, 0) fails for callers.
        try:
            os.waitpid(pid, os.WNOHANG)
        except OSError:
            pass
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                return
            time.sleep(0.05)


def safe_workspace_url(url: str) -> str:
    """Allow only http(s)/file workspace restores. Never javascript/data/chrome."""
    raw = (url or "").strip()
    if raw.startswith(("https://", "http://", "file://")):
        return raw
    return ""


def page_needs_restore(url: str) -> bool:
    raw = (url or "").strip()
    if not raw or raw == "about:blank":
        return True
    return raw.startswith(("chrome://", "chrome-error://", "chrome-untrusted://", "devtools://", "about:"))


def pick_page_target(pages: list[Any]) -> dict[str, Any] | None:
    """Prefer the visible working page over about:blank / chrome:// leftovers."""
    candidates = [p for p in pages if isinstance(p, dict) and p.get("type") == "page"]
    if not candidates:
        return None

    def score(page: dict[str, Any]) -> tuple[int, int]:
        url = str(page.get("url") or "")
        if url.startswith(("chrome://", "chrome-untrusted://", "devtools://")):
            return (0, 0)
        if url in ("", "about:blank"):
            return (1, 0)
        if url.startswith(("file:", "http://", "https://")):
            return (3, 1 if page.get("webSocketDebuggerUrl") else 0)
        return (2, 0)

    return max(candidates, key=score)


def _loopback_ws(handle: RuntimeHandle) -> tuple[str, str | None]:
    if not handle.cdp_loopback or not handle.cdp_loopback.startswith("http://127.0.0.1:"):
        raise RuntimeError("refusing CDP on a non-loopback handle")
    import json
    import time
    import urllib.request

    base = handle.cdp_loopback.rstrip("/")
    version = None
    last_err = None
    for _ in range(20):
        try:
            with urllib.request.urlopen(f"{base}/json/version", timeout=5) as resp:
                version = json.load(resp)
            break
        except Exception as exc:
            last_err = exc
            time.sleep(0.15)
    if version is None:
        raise RuntimeError(f"chromium loopback CDP not ready: {last_err}")
    ws_url = version.get("webSocketDebuggerUrl")
    if not ws_url or not str(ws_url).startswith(("ws://127.0.0.1:", "ws://localhost:")):
        raise RuntimeError("chromium did not expose a loopback websocket")
    target_id = None
    try:
        with urllib.request.urlopen(f"{base}/json/list", timeout=5) as resp:
            pages = json.load(resp)
        if isinstance(pages, list):
            page = next((p for p in pages if isinstance(p, dict) and p.get("type") == "page" and p.get("id") == handle.target_id), None)
            page = page or pick_page_target(pages)
            if page:
                target_id = page.get("id")
                handle.target_id = target_id
    except Exception:
        target_id = None
    return str(ws_url), target_id


def _run_loopback_async(coro_factory):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro_factory())).result(timeout=20)


def cdp_set_file_input(handle: RuntimeHandle, selector: str, path: Path) -> None:
    """Set a file input using one loopback CDP websocket session.

    ``Runtime.evaluate`` objectIds are session-scoped. One-shot
    ``loopback_cdp`` calls cannot feed ``DOM.setFileInputFiles``.
    """
    import asyncio
    import json

    from tools.browser_cdp_tool import websockets

    ws_url, target_id = _loopback_ws(handle)
    resolved = str(path.resolve())

    async def _run() -> None:
        if websockets is None:
            raise RuntimeError("websockets is required for Chromium CDP")
        async with websockets.connect(
            ws_url,
            max_size=None,
            open_timeout=8,
            close_timeout=5,
            ping_interval=None,
        ) as ws:
            next_id = 1
            session_id = None
            if target_id:
                attach_id = next_id
                next_id += 1
                await ws.send(
                    json.dumps(
                        {
                            "id": attach_id,
                            "method": "Target.attachToTarget",
                            "params": {"targetId": target_id, "flatten": True},
                        }
                    )
                )
                while True:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
                    if msg.get("id") == attach_id:
                        if "error" in msg:
                            raise RuntimeError(f"Target.attachToTarget failed: {msg['error']}")
                        session_id = (msg.get("result") or {}).get("sessionId")
                        break

            async def send(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
                nonlocal next_id
                call_id = next_id
                next_id += 1
                req: dict[str, Any] = {"id": call_id, "method": method, "params": params or {}}
                if session_id:
                    req["sessionId"] = session_id
                await ws.send(json.dumps(req))
                while True:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
                    if msg.get("id") == call_id:
                        if "error" in msg:
                            raise RuntimeError(f"CDP error: {msg['error']}")
                        return msg.get("result") or {}

            await send("DOM.enable", {})
            remote = await send(
                "Runtime.evaluate",
                {
                    "expression": f"document.querySelector({selector!r})",
                    "objectGroup": "upload",
                },
            )
            object_id = ((remote.get("result") or {}).get("objectId"))
            if not object_id:
                raise ValueError("upload target not found")
            node = await send("DOM.requestNode", {"objectId": object_id})
            node_id = node.get("nodeId")
            payload: dict[str, Any] = {"files": [resolved]}
            if node_id:
                payload["nodeId"] = node_id
            else:
                payload["objectId"] = object_id
            await send("DOM.setFileInputFiles", payload)

    _run_loopback_async(_run)


def loopback_cdp(handle: RuntimeHandle, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Send one CDP method to the loopback DevTools endpoint.

    Reuses ``tools.browser_cdp_tool._cdp_call``. The HTTP origin must stay
    127.0.0.1 — this is not a public CDP surface.
    """
    from tools.browser_cdp_tool import _cdp_call

    ws_url, target_id = _loopback_ws(handle)

    async def _run() -> dict[str, Any]:
        return await _cdp_call(ws_url, method, params or {}, target_id, 8.0)

    return _run_loopback_async(_run)


def private_dir(path: Path) -> Path:
    """Create a Hermes-owned directory that is not world-readable."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    for parent in path.parents:
        if parent.name == "agent-computers":
            try:
                parent.chmod(0o700)
            except OSError:
                pass
            break
    return path


def new_identity_profile_dir(root: Path, identity_id: str) -> str:
    """Durable managed dir. Never the OS default Chromium user-data-dir."""
    path = private_dir(root / "identities" / identity_id)
    (path / ".hermes-identity").write_text(identity_id, encoding="utf-8")
    return str(path)
