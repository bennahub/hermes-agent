"""Owner takeover stream: bounded private CDP frames + direct input.

Frames stay on the authenticated Hermes hop. Loopback CDP is never part
of the public payload. Side REST observe/act remains fallback only.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from .adapter import RuntimeHandle
from .models import project_control
from .pointer import map_owner_pointer

DEFAULT_VIEWPORT_WIDTH = 1440
DEFAULT_VIEWPORT_HEIGHT = 900
DEFAULT_JPEG_QUALITY = 85
MAX_INFLIGHT_FRAMES = 2
MAX_VIEWPORT_WIDTH = 1920
MAX_VIEWPORT_HEIGHT = 1200
MIN_VIEWPORT_WIDTH = 800
MIN_VIEWPORT_HEIGHT = 500


def clamp_viewport(width: int, height: int) -> tuple[int, int]:
    return (
        max(MIN_VIEWPORT_WIDTH, min(MAX_VIEWPORT_WIDTH, int(width or DEFAULT_VIEWPORT_WIDTH))),
        max(MIN_VIEWPORT_HEIGHT, min(MAX_VIEWPORT_HEIGHT, int(height or DEFAULT_VIEWPORT_HEIGHT))),
    )


@dataclass
class StreamFrame:
    session_id: int
    mime: str
    data: str
    width: int
    height: int
    seq: int
    generation: int
    location: dict[str, Any] = field(default_factory=dict)


@dataclass
class FrameBroker:
    """Ack/backpressure. Unacked frames are capped; extras are dropped."""

    max_inflight: int = MAX_INFLIGHT_FRAMES
    pending: deque[int] = field(default_factory=deque)
    acked: deque[int] = field(default_factory=lambda: deque(maxlen=128))
    dropped: int = 0
    emitted: int = 0

    def offer(self, session_id: int) -> bool:
        if len(self.pending) >= self.max_inflight:
            self.dropped += 1
            return False
        self.pending.append(int(session_id))
        self.emitted += 1
        return True

    def ack(self, session_id: int) -> bool:
        sid = int(session_id)
        if sid in self.pending:
            self.pending.remove(sid)
            self.acked.append(sid)
            return True
        return False

    @property
    def inflight(self) -> int:
        return len(self.pending)


@dataclass
class OwnerStreamSession:
    computer_id: str
    identity_id: str | None
    generation: int
    lease_id: str
    fencing_epoch: int
    viewport_width: int = DEFAULT_VIEWPORT_WIDTH
    viewport_height: int = DEFAULT_VIEWPORT_HEIGHT
    jpeg_quality: int = DEFAULT_JPEG_QUALITY
    broker: FrameBroker = field(default_factory=FrameBroker)
    frames: deque[StreamFrame] = field(default_factory=deque)
    closed: bool = False
    last_input_kind: str = ""
    _seq: int = 0
    location: dict[str, Any] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _stop_cdp: Callable[[], None] | None = None

    def push_frame(self, session_id: int, data: str, width: int, height: int, mime: str = "image/jpeg") -> StreamFrame | None:
        with self._lock:
            if self.closed or not self.broker.offer(session_id):
                return None
            self._seq += 1
            frame = StreamFrame(
                session_id=session_id,
                mime=mime,
                data=data,
                width=width or self.viewport_width,
                height=height or self.viewport_height,
                seq=self._seq,
                generation=self.generation,
                location=dict(self.location),
            )
            self.frames.append(frame)
            return frame

    def pop_frame(self) -> StreamFrame | None:
        with self._lock:
            if not self.frames:
                return None
            return self.frames.popleft()

    def ack(self, session_id: int) -> bool:
        with self._lock:
            return self.broker.ack(session_id)

    def public_hello(self, *, control: str, url: str = "", title: str = "") -> dict[str, Any]:
        return {
            "type": "hello",
            "computer_id": self.computer_id,
            "identity_id": self.identity_id,
            "generation": self.generation,
            "lease_id": self.lease_id,
            "fencing_epoch": self.fencing_epoch,
            "control": project_control(control) or control,
            "control_authority": control,
            "viewport": {
                "width": self.viewport_width,
                "height": self.viewport_height,
            },
            "quality": self.jpeg_quality,
            "url": url,
            "title": title,
            "public_cdp": False,
        }

    def public_frame(self, frame: StreamFrame) -> dict[str, Any]:
        return {
            "type": "frame",
            "session_id": frame.session_id,
            "mime": frame.mime,
            "data": frame.data,
            "width": frame.width,
            "height": frame.height,
            "seq": frame.seq,
            "generation": frame.generation,
            "location": frame.location,
        }

    def close(self) -> None:
        with self._lock:
            self.closed = True
        stop = self._stop_cdp
        if stop:
            try:
                stop()
            except Exception:
                pass


class StreamHub:
    """One live Owner stream per computer. Reconnect replaces the session."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, OwnerStreamSession] = {}
        self._generation: dict[str, int] = {}

    def next_generation(self, computer_id: str) -> int:
        with self._lock:
            nxt = int(self._generation.get(computer_id, 0)) + 1
            self._generation[computer_id] = nxt
            return nxt

    def attach(self, session: OwnerStreamSession) -> OwnerStreamSession:
        with self._lock:
            previous = self._sessions.get(session.computer_id)
            self._sessions[session.computer_id] = session
        if previous and previous.generation != session.generation:
            previous.close()
        return session

    def get(self, computer_id: str) -> OwnerStreamSession | None:
        with self._lock:
            return self._sessions.get(computer_id)

    def drop(self, computer_id: str, generation: int) -> bool:
        with self._lock:
            current = self._sessions.get(computer_id)
            if current is None or current.generation != generation:
                return False
            self._sessions.pop(computer_id, None)
        current.close()
        return True


_HUB = StreamHub()


def get_stream_hub() -> StreamHub:
    return _HUB


def reset_stream_hub_for_tests() -> None:
    global _HUB
    _HUB = StreamHub()


def normalize_owner_event(raw: dict[str, Any], *, viewport_width: int, viewport_height: int) -> dict[str, Any]:
    """Strip payload to actionable fields. Never keep secrets."""
    kind = str(raw.get("type") or raw.get("kind") or "")
    if kind == "ack":
        return {"kind": "ack", "session_id": int(raw.get("session_id") or 0)}
    if kind == "resize":
        width, height = clamp_viewport(int(raw.get("width") or 0), int(raw.get("height") or 0))
        return {"kind": "resize", "width": width, "height": height}
    if kind in ("pointer", "pointer_move", "pointer_down", "pointer_up", "pointer_click"):
        phase = str(raw.get("phase") or "")
        if kind == "pointer_click":
            phase = "click"
        elif kind == "pointer_move":
            phase = phase or "move"
        elif kind == "pointer_down":
            phase = phase or "down"
        elif kind == "pointer_up":
            phase = phase or "up"
        displayed_w = float(raw.get("client_width") or raw.get("displayed_width") or raw.get("surface_width") or viewport_width)
        displayed_h = float(raw.get("client_height") or raw.get("displayed_height") or raw.get("surface_height") or viewport_height)
        x, y = map_owner_pointer(
            float(raw.get("x") or 0),
            float(raw.get("y") or 0),
            displayed_width=displayed_w,
            displayed_height=displayed_h,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
            frame_width=int(raw.get("frame_width") or 0),
            frame_height=int(raw.get("frame_height") or 0),
        )
        return {
            "kind": "pointer",
            "phase": phase or "move",
            "x": x,
            "y": y,
            "buttons": int(raw.get("buttons") or 0),
            "click_count": int(raw.get("click_count") or 1),
        }
    if kind == "wheel":
        displayed_w = float(raw.get("client_width") or raw.get("displayed_width") or viewport_width)
        displayed_h = float(raw.get("client_height") or raw.get("displayed_height") or viewport_height)
        x, y = map_owner_pointer(
            float(raw.get("x") or 0),
            float(raw.get("y") or 0),
            displayed_width=displayed_w,
            displayed_height=displayed_h,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
            frame_width=int(raw.get("frame_width") or 0),
            frame_height=int(raw.get("frame_height") or 0),
        )
        return {
            "kind": "wheel",
            "x": x,
            "y": y,
            "delta_x": float(raw.get("delta_x") or 0),
            "delta_y": float(raw.get("delta_y") or 0),
        }
    if kind == "key":
        return {
            "kind": "key",
            "phase": str(raw.get("phase") or "down"),
            "key": str(raw.get("key") or ""),
            "code": str(raw.get("code") or ""),
            "modifiers": int(raw.get("modifiers") or 0),
        }
    if kind == "cursor":
        displayed_w = float(raw.get("client_width") or raw.get("displayed_width") or viewport_width)
        displayed_h = float(raw.get("client_height") or raw.get("displayed_height") or viewport_height)
        x, y = map_owner_pointer(
            float(raw.get("x") or 0),
            float(raw.get("y") or 0),
            displayed_width=displayed_w,
            displayed_height=displayed_h,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
            frame_width=int(raw.get("frame_width") or 0),
            frame_height=int(raw.get("frame_height") or 0),
        )
        return {"kind": "cursor", "x": x, "y": y}
    if kind == "text":
        return {"kind": "text", "text": str(raw.get("text") or "")}
    if kind == "ping":
        return {"kind": "ping"}
    if kind == "nav":
        action = str(raw.get("action") or "")
        if action not in ("back", "forward", "reload", "open"):
            raise ValueError(f"unsupported stream event: {kind}")
        from .location import safe_navigate_url

        return {
            "kind": "nav",
            "action": action,
            "url": safe_navigate_url(str(raw.get("url") or "")) if action == "open" else "",
        }
    raise ValueError(f"unsupported stream event: {kind}")


def apply_stream_event(runtime: Any, handle: RuntimeHandle, event: dict[str, Any]) -> None:
    """Dispatch a normalized event. Runtime must not log the payload."""
    kind = event.get("kind")
    if kind == "pointer":
        phase = event.get("phase")
        x = float(event.get("x") or 0)
        y = float(event.get("y") or 0)
        click_count = int(event.get("click_count") or 1)
        stream_pointer = getattr(runtime, "stream_pointer", None)
        if callable(stream_pointer):
            stream_pointer(handle, phase=phase, x=x, y=y, click_count=click_count, buttons=event.get("buttons") or 0)
            return
        if phase == "move":
            runtime.act(handle, kind="pointer_move", x=x, y=y)
        elif phase == "down":
            runtime.act(handle, kind="pointer_click", x=x, y=y)
        elif phase == "up":
            runtime.act(handle, kind="pointer_move", x=x, y=y)
        else:
            runtime.act(handle, kind="pointer_click", x=x, y=y)
        return
    if kind == "wheel":
        stream_wheel = getattr(runtime, "stream_wheel", None)
        if callable(stream_wheel):
            stream_wheel(
                handle,
                x=float(event.get("x") or 0),
                y=float(event.get("y") or 0),
                delta_x=float(event.get("delta_x") or 0),
                delta_y=float(event.get("delta_y") or 0),
            )
            return
        runtime.act(
            handle,
            kind="scroll",
            x=event.get("x"),
            y=event.get("y"),
            delta_x=float(event.get("delta_x") or 0),
            delta_y=float(event.get("delta_y") or 0),
        )
        return
    if kind == "key":
        stream_key = getattr(runtime, "stream_key", None)
        if callable(stream_key):
            stream_key(
                handle,
                phase=str(event.get("phase") or "down"),
                key=str(event.get("key") or ""),
                code=str(event.get("code") or ""),
                modifiers=int(event.get("modifiers") or 0),
            )
            return
        if event.get("phase") == "down":
            runtime.act(handle, kind="key", key=str(event.get("key") or ""), code=str(event.get("code") or ""))
        return
    if kind == "text":
        runtime.act(handle, kind="text", text=str(event.get("text") or ""))
        return
    if kind == "nav":
        nav = getattr(runtime, "stream_nav", None)
        if callable(nav):
            return nav(handle, str(event.get("action") or ""), str(event.get("url") or ""))
        if event.get("action") == "open" and event.get("url"):
            runtime.act(handle, kind="navigate", target=str(event.get("url")))
        return None
    if kind == "resize":
        apply = getattr(runtime, "apply_viewport", None)
        if callable(apply):
            apply(handle, int(event["width"]), int(event["height"]))
        return


def emit_memory_frame(session: OwnerStreamSession, page_url: str = "") -> StreamFrame | None:
    """Tiny JPEG stand-in for InMemory tests. Not used as the live product path."""
    # 1x1 JPEG so tests never depend on Chromium.
    data = (
        "/9j/4AAQSkZJRgABAQAAAQABAAD/2wAAAAgIBwcIBwgICAgICAgICAgICAgI"
        "CAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI/8AA"
        "EQgAAQABAwERAAIRAQMRAf/EABQAAQAAAAAAAAAAAAAAAAAAAAj/xAAUEAE"
        "AAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIQAxAAAAGf/8QAFBABAAAAAAAAAA"
        "AAAAAAAAAA/9oACAEBAAEFAv/EABQRAQAAAAAAAAAAAAAAAAAAAAD/2gAIA"
        "QMBAT8B/8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPwH/xAAUEAEA"
        "AAAAAAAAAAAAAAAAAAAg/9oACAEBAAY/Al//xAAUEAEAAAAAAAAAAAAAAAA"
        "AAAAg/9oACAEBAAE/IV//2Q=="
    )
    _ = page_url
    return session.push_frame(session.broker.emitted + 1, data, session.viewport_width, session.viewport_height)

async def run_chromium_screencast(session: OwnerStreamSession, handle: RuntimeHandle) -> None:
    """Bounded live frames over one private CDP connection.

    The installed headless Chromium does not emit Page.startScreencast
    frames. Capture on the persistent socket, with only two unacknowledged
    frames. Owner input remains on the service's synchronous fenced path.
    """
    from tools.browser_cdp_tool import websockets
    from .adapter import _loopback_ws
    from .location import public_location

    if websockets is None:
        raise RuntimeError("websockets is required for Chromium streaming")
    if not handle.cdp_loopback:
        raise RuntimeError("chromium handle has no loopback CDP")
    ws_url, target_id = await asyncio.to_thread(_loopback_ws, handle)
    stop = threading.Event()
    session._stop_cdp = stop.set

    async with websockets.connect(
        ws_url, max_size=None, open_timeout=8, close_timeout=2, ping_interval=None,
    ) as ws:
        next_id = 1
        cdp_session = None
        pending: dict[int, asyncio.Future] = {}

        async def send(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            nonlocal next_id
            call_id = next_id
            next_id += 1
            req: dict[str, Any] = {"id": call_id, "method": method, "params": params or {}}
            if cdp_session:
                req["sessionId"] = cdp_session
            fut = asyncio.get_running_loop().create_future()
            pending[call_id] = fut
            try:
                await ws.send(json.dumps(req))
                return await asyncio.wait_for(fut, timeout=8)
            finally:
                pending.pop(call_id, None)

        async def reader() -> None:
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    fut = pending.get(msg.get("id"))
                    if fut is None or fut.done():
                        continue
                    if "error" in msg:
                        # CDP error text can contain page data. Keep it private.
                        fut.set_exception(RuntimeError("private browser command failed"))
                    else:
                        fut.set_result(msg.get("result") or {})
            finally:
                for fut in list(pending.values()):
                    if not fut.done():
                        fut.set_exception(RuntimeError("private browser connection closed"))

        reader_task = asyncio.create_task(reader())
        try:
            if target_id:
                attached = await send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
                cdp_session = attached.get("sessionId")
            await send("Page.enable")
            await send("Emulation.setDeviceMetricsOverride", {
                "width": session.viewport_width, "height": session.viewport_height,
                "deviceScaleFactor": 1, "mobile": False,
            })
            while not session.closed and not stop.is_set():
                if reader_task.done():
                    raise RuntimeError("private browser connection closed")
                if session.broker.inflight >= session.broker.max_inflight:
                    await asyncio.sleep(0.02)
                    continue
                page = await send("Runtime.evaluate", {
                    "expression": "({url: location.href, title: document.title || ''})",
                    "returnByValue": True,
                })
                value = (page.get("result") or {}).get("value") or {}
                session.location = public_location(str(value.get("url") or ""), str(value.get("title") or ""))
                shot = await send("Page.captureScreenshot", {
                    "format": "jpeg", "quality": session.jpeg_quality,
                })
                if shot.get("data"):
                    session.push_frame(
                        session._seq + 1, str(shot["data"]),
                        session.viewport_width, session.viewport_height,
                    )
                await asyncio.sleep(0.08)
        finally:
            reader_task.cancel()
            await asyncio.gather(reader_task, return_exceptions=True)
