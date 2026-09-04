"""Owner takeover stream: CDP screencast frames + direct input.

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
from .keys import cdp_key_params, is_printable_key
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


@dataclass
class FrameBroker:
    """Ack/backpressure. Unacked frames are capped; extras are dropped."""

    max_inflight: int = MAX_INFLIGHT_FRAMES
    pending: deque[int] = field(default_factory=deque)
    acked: list[int] = field(default_factory=list)
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
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _dispatch: Callable[[dict[str, Any]], None] | None = None
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
    """Persistent loopback CDP frames. Requires an already-awake Chromium.

    Commands wait on a concurrent reader. Sending before that reader
    starts deadlocks every CDP call (8s timeout) and yields a black view.
    """
    from tools.browser_cdp_tool import websockets

    from .adapter import _loopback_ws

    if websockets is None:
        raise RuntimeError("websockets is required for Chromium screencast")
    if not handle.cdp_loopback:
        raise RuntimeError("chromium handle has no loopback CDP")
    ws_url, target_id = _loopback_ws(handle)
    stop = threading.Event()
    session._stop_cdp = stop.set

    async with websockets.connect(
        ws_url,
        max_size=None,
        open_timeout=8,
        close_timeout=5,
        ping_interval=None,
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
            loop = asyncio.get_running_loop()
            fut: asyncio.Future = loop.create_future()
            pending[call_id] = fut
            await ws.send(json.dumps(req))
            return await asyncio.wait_for(fut, timeout=8)

        async def reader() -> None:
            while not session.closed and not stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    return
                msg = json.loads(raw)
                mid = msg.get("id")
                if mid in pending:
                    fut = pending.pop(mid)
                    if "error" in msg:
                        if not fut.done():
                            fut.set_exception(RuntimeError(str(msg["error"])))
                    elif not fut.done():
                        fut.set_result(msg.get("result") or {})
                    continue
                if msg.get("method") == "Page.screencastFrame":
                    params = msg.get("params") or {}
                    sid = int(params.get("sessionId") or 0)
                    meta = params.get("metadata") or {}
                    accepted = session.push_frame(
                        sid,
                        str(params.get("data") or ""),
                        int(meta.get("deviceWidth") or session.viewport_width),
                        int(meta.get("deviceHeight") or session.viewport_height),
                    )
                    if accepted is None:
                        try:
                            await send("Page.screencastFrameAck", {"sessionId": sid})
                        except Exception:
                            return

        input_q: asyncio.Queue = asyncio.Queue(maxsize=128)
        input_pending = 0
        loop = asyncio.get_running_loop()

        async def screenshot_pump() -> None:
            while not session.closed and not stop.is_set():
                if input_pending:
                    await asyncio.sleep(0.01)
                    continue
                try:
                    shot = await send(
                        "Page.captureScreenshot",
                        {"format": "jpeg", "quality": session.jpeg_quality},
                    )
                    data = str((shot or {}).get("data") or "")
                    if data:
                        session.push_frame(
                            session.broker.emitted + 1,
                            data,
                            session.viewport_width,
                            session.viewport_height,
                        )
                except Exception:
                    if session.closed or stop.is_set():
                        return
                await asyncio.sleep(0.08)

        async def apply_input(event: dict[str, Any]) -> None:
            kind = event.get("kind")
            if kind == "ack":
                sid = int(event.get("session_id") or 0)
                session.ack(sid)
                try:
                    await send("Page.screencastFrameAck", {"sessionId": sid})
                except Exception:
                    return
                return
            if kind == "pointer":
                phase = event.get("phase")
                x, y = float(event.get("x") or 0), float(event.get("y") or 0)
                click_count = int(event.get("click_count") or 1)
                buttons = 1 if int(event.get("buttons") or 0) else (1 if phase in ("down", "click") else 0)
                try:
                    if phase == "move":
                        await send(
                            "Input.dispatchMouseEvent",
                            {"type": "mouseMoved", "x": x, "y": y, "buttons": buttons},
                        )
                        return
                    if phase in ("down", "click"):
                        await send(
                            "Input.dispatchMouseEvent",
                            {"type": "mouseMoved", "x": x, "y": y, "buttons": 0},
                        )
                        await send(
                            "Input.dispatchMouseEvent",
                            {
                                "type": "mousePressed",
                                "x": x,
                                "y": y,
                                "button": "left",
                                "clickCount": click_count,
                                "buttons": 1,
                            },
                        )
                    if phase in ("up", "click"):
                        await send(
                            "Input.dispatchMouseEvent",
                            {
                                "type": "mouseReleased",
                                "x": x,
                                "y": y,
                                "button": "left",
                                "clickCount": click_count,
                                "buttons": 0,
                            },
                        )
                except Exception:
                    return
                return
            if kind == "wheel":
                try:
                    await send(
                        "Input.dispatchMouseEvent",
                        {
                            "type": "mouseWheel",
                            "x": float(event.get("x") or 0),
                            "y": float(event.get("y") or 0),
                            "deltaX": float(event.get("delta_x") or 0),
                            "deltaY": float(event.get("delta_y") or 0),
                        },
                    )
                except Exception:
                    return
                return
            if kind == "key":
                phase = str(event.get("phase") or "down")
                key = str(event.get("key") or "")
                modifiers = int(event.get("modifiers") or 0)
                try:
                    if phase == "down" and is_printable_key(key, modifiers):
                        await send("Input.insertText", {"text": key})
                        return
                    await send(
                        "Input.dispatchKeyEvent",
                        cdp_key_params(
                            phase=phase,
                            key=key,
                            code=str(event.get("code") or ""),
                            modifiers=modifiers,
                        ),
                    )
                except Exception:
                    return
                return
            if kind == "text":
                text = str(event.get("text") or "")
                if text:
                    try:
                        await send("Input.insertText", {"text": text})
                    except Exception:
                        return

        async def input_worker() -> None:
            nonlocal input_pending
            while not session.closed and not stop.is_set():
                try:
                    event = await asyncio.wait_for(input_q.get(), timeout=0.2)
                except asyncio.TimeoutError:
                    continue
                input_pending += 1
                try:
                    await apply_input(event)
                finally:
                    input_pending -= 1

        def dispatch(event: dict[str, Any]) -> None:
            if session.closed or stop.is_set():
                return

            def _put() -> None:
                try:
                    input_q.put_nowait(event)
                except asyncio.QueueFull:
                    if event.get("kind") == "pointer" and event.get("phase") == "move":
                        return
                    try:
                        input_q.get_nowait()
                    except Exception:
                        return
                    try:
                        input_q.put_nowait(event)
                    except Exception:
                        return

            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is loop:
                _put()
            else:
                loop.call_soon_threadsafe(_put)

        session._dispatch = dispatch
        reader_task = asyncio.create_task(reader())
        input_task = asyncio.create_task(input_worker())
        fallback_task = None
        try:
            if target_id:
                attached = await send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
                cdp_session = (attached or {}).get("sessionId")
            await send("Page.enable", {})
            try:
                await send("Emulation.setFocusEmulationEnabled", {"enabled": True})
            except Exception:
                pass
            try:
                await send("Page.bringToFront", {})
            except Exception:
                pass
            await send(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": session.viewport_width,
                    "height": session.viewport_height,
                    "deviceScaleFactor": 1,
                    "mobile": False,
                },
            )
            try:
                await send(
                    "Page.startScreencast",
                    {
                        "format": "jpeg",
                        "quality": session.jpeg_quality,
                        "maxWidth": session.viewport_width,
                        "maxHeight": session.viewport_height,
                        "everyNthFrame": 1,
                    },
                )
            except Exception:
                pass
            fallback_task = asyncio.create_task(screenshot_pump())
            while not session.closed and not stop.is_set():
                await asyncio.sleep(0.2)
        except Exception:
            if session.closed or stop.is_set():
                return
            raise
        finally:
            if fallback_task:
                fallback_task.cancel()
            input_task.cancel()
            reader_task.cancel()
            try:
                await send("Page.stopScreencast", {})
            except Exception:
                pass
