"""Bounded X11 clipboard delivery inside the private native input worker.

Only the current controller's text owns the selection. The payload never enters argv,
disk or logs. Completion means the requestor consumed its UTF-8 property, not
merely that a clipboard provider was started or TARGETS was requested. The worker
continues serving repeat reads until replacement or a fenced input release.
"""
from __future__ import annotations

import ctypes as C
import time


class SelectionRequest(C.Structure):
    _fields_ = [("type", C.c_int), ("serial", C.c_ulong), ("send_event", C.c_int),
                ("display", C.c_void_p), ("owner", C.c_ulong), ("requestor", C.c_ulong),
                ("selection", C.c_ulong), ("target", C.c_ulong), ("property", C.c_ulong),
                ("time", C.c_ulong)]


class SelectionNotify(C.Structure):
    _fields_ = [("type", C.c_int), ("serial", C.c_ulong), ("send_event", C.c_int),
                ("display", C.c_void_p), ("requestor", C.c_ulong),
                ("selection", C.c_ulong), ("target", C.c_ulong), ("property", C.c_ulong),
                ("time", C.c_ulong)]


class PropertyNotify(C.Structure):
    _fields_ = [("type", C.c_int), ("serial", C.c_ulong), ("send_event", C.c_int),
                ("display", C.c_void_p), ("window", C.c_ulong), ("atom", C.c_ulong),
                ("time", C.c_ulong), ("state", C.c_int)]


class Event(C.Union):
    _fields_ = [("type", C.c_int), ("request", SelectionRequest),
                ("selection", SelectionNotify), ("property", PropertyNotify),
                ("pad", C.c_long * 24)]


class NativeClipboard:
    def __init__(self, xlib, display, check_errors=lambda: None):
        self.x, self.d = xlib, display
        self.check_errors = check_errors
        declarations = {
            "XDefaultRootWindow": ([C.c_void_p], C.c_ulong),
            "XCreateSimpleWindow": ([C.c_void_p, C.c_ulong, C.c_int, C.c_int, C.c_uint,
                                     C.c_uint, C.c_uint, C.c_ulong, C.c_ulong], C.c_ulong),
            "XInternAtom": ([C.c_void_p, C.c_char_p, C.c_int], C.c_ulong),
            "XSetSelectionOwner": ([C.c_void_p, C.c_ulong, C.c_ulong, C.c_ulong], C.c_int),
            "XGetSelectionOwner": ([C.c_void_p, C.c_ulong], C.c_ulong),
            "XPending": ([C.c_void_p], C.c_int),
            "XNextRequest": ([C.c_void_p], C.c_ulong),
            "XNextEvent": ([C.c_void_p, C.POINTER(Event)], C.c_int),
            "XSelectInput": ([C.c_void_p, C.c_ulong, C.c_long], C.c_int),
            "XChangeProperty": ([C.c_void_p, C.c_ulong, C.c_ulong, C.c_ulong, C.c_int,
                                 C.c_int, C.c_void_p, C.c_int], C.c_int),
            "XSendEvent": ([C.c_void_p, C.c_ulong, C.c_int, C.c_long, C.POINTER(Event)], C.c_int),
            "XDestroyWindow": ([C.c_void_p, C.c_ulong], C.c_int),
            "XConvertSelection": ([C.c_void_p, C.c_ulong, C.c_ulong, C.c_ulong, C.c_ulong, C.c_ulong], C.c_int),
            "XDeleteProperty": ([C.c_void_p, C.c_ulong, C.c_ulong], C.c_int),
            "XGetWindowProperty": ([C.c_void_p, C.c_ulong, C.c_ulong, C.c_long, C.c_long, C.c_int, C.c_ulong,
                                    C.POINTER(C.c_ulong), C.POINTER(C.c_int), C.POINTER(C.c_ulong),
                                    C.POINTER(C.c_ulong), C.POINTER(C.c_void_p)], C.c_int),
            "XFree": ([C.c_void_p], C.c_int),
        }
        for name, (args, result) in declarations.items():
            fn = getattr(xlib, name)
            fn.argtypes, fn.restype = args, result
        self.text, self.data = "", b""
        self.pending, self.sent_payload, self.consumed = {}, False, False
        self._reading, self._read_done, self._read_data = False, False, None
        self.window = self.x.XCreateSimpleWindow(self.d, self.x.XDefaultRootWindow(self.d), 0, 0, 1, 1, 0, 0, 0)
        self.atoms = {s: self.x.XInternAtom(self.d, s.encode(), 0)
                      for s in ("CLIPBOARD", "TARGETS", "UTF8_STRING", "STRING", "TEXT", "HERMES_NATIVE_READBACK")}

    def paste(self, text, deadline, chord):
        data = text.encode("utf-8")
        if not data:
            return False
        if len(text) > 8192 or b"\0" in data:
            raise ValueError("native text exceeds supported size")
        self.clear()
        self.text, self.data = text, data
        self.pending, self.sent_payload, self.consumed = {}, False, False
        self.x.XSetSelectionOwner(self.d, self.atoms["CLIPBOARD"], self.window, 0)
        if self.x.XGetSelectionOwner(self.d, self.atoms["CLIPBOARD"]) != self.window:
            raise RuntimeError("native clipboard ownership failed")
        try:
            chord("v", 2)
            self.x.XSync(self.d, 0)
            self.check_errors()
            while time.monotonic() < deadline:
                self.pump()
                if self.consumed:
                    # Chrome can request the same selection again after reading
                    # it once. The worker continues serving it until release or
                    # replacement; delivery is not a DOM-completion assertion.
                    return True
                time.sleep(.002)
            if self.sent_payload:
                raise TimeoutError("native clipboard delivery expired")
            self.clear()  # Non-editable focus may legitimately request no data.
            return False
        except Exception:
            self.clear()
            raise

    def read_utf8(self, deadline):
        """Read a native application's copied selection, in memory only."""
        x, d, a = self.x, self.d, self.atoms
        while time.monotonic() < deadline:
            owner = x.XGetSelectionOwner(d, a["CLIPBOARD"])
            if owner and owner != self.window:
                break
            self.pump()
            time.sleep(.002)
        else:
            return None
        self._reading, self._read_done, self._read_data = True, False, None
        # Each read has its own requestor. A late reply from a timed-out copy
        # cannot be mistaken for a later destination's verification.
        self._read_window = x.XCreateSimpleWindow(d, x.XDefaultRootWindow(d), 0, 0, 1, 1, 0, 0, 0)
        prop = a["HERMES_NATIVE_READBACK"]
        x.XConvertSelection(d, a["CLIPBOARD"], a["UTF8_STRING"], prop, self._read_window, 0)
        x.XSync(d, 0)
        self.check_errors()
        try:
            while time.monotonic() < deadline:
                self.pump()
                if self._read_done:
                    return self._read_data
                time.sleep(.002)
            return None
        finally:
            self._reading, self._read_data = False, None
            x.XDestroyWindow(d, self._read_window)
            self._read_window = 0

    def _receive_readback(self, reply):
        x, d, a = self.x, self.d, self.atoms
        if reply.requestor != self._read_window or reply.selection != a["CLIPBOARD"]:
            return
        self._read_done, self._read_data = True, None
        if reply.property != a["HERMES_NATIVE_READBACK"] or reply.target != a["UTF8_STRING"]:
            return
        actual, fmt, count, remaining, data = C.c_ulong(), C.c_int(), C.c_ulong(), C.c_ulong(), C.c_void_p()
        status = x.XGetWindowProperty(d, self._read_window, reply.property, 0, 8192, 1, 0,
                                      C.byref(actual), C.byref(fmt), C.byref(count), C.byref(remaining), C.byref(data))
        try:
            self.check_errors()
            if (not status and actual.value == a["UTF8_STRING"] and fmt.value == 8
                    and remaining.value == 0 and count.value <= 32768):
                self._read_data = C.string_at(data, count.value) if data else b""
        finally:
            if data:
                x.XFree(data)

    def pump(self):
        x, d, a = self.x, self.d, self.atoms
        for _ in range(64):
            if not x.XPending(d):
                return
            event = Event()
            x.XNextEvent(d, C.byref(event))
            if event.type == 31 and getattr(self, "_reading", False):
                self._receive_readback(event.selection)
                continue
            if event.type == 28:
                p = event.property
                serial = self.pending.get((p.window, p.atom))
                if p.state == 1 and serial is not None and p.serial >= serial:
                    self.consumed = True
                continue
            if event.type != 30:
                continue
            request = event.request
            if request.owner != self.window or request.selection != a["CLIPBOARD"]:
                continue
            prop = request.property or request.target
            reply = Event()
            reply.selection = SelectionNotify(31, 0, 1, d, request.requestor,
                                               request.selection, request.target, 0, request.time)
            if self.data and request.target == a["TARGETS"]:
                targets = (C.c_ulong * 2)(a["TARGETS"], a["UTF8_STRING"])
                x.XChangeProperty(d, request.requestor, prop, 4, 32, 0, targets, 2)
                reply.selection.property = prop
            elif self.data and request.target in (a["UTF8_STRING"], a["TEXT"]):
                x.XSelectInput(d, request.requestor, 1 << 22)
                serial = x.XNextRequest(d)
                x.XChangeProperty(d, request.requestor, prop, a["UTF8_STRING"], 8, 0,
                                  C.c_char_p(self.data), len(self.data))
                self.sent_payload = True
                self.pending[(request.requestor, prop)] = serial
                reply.selection.property = prop
            # Unsupported or already-cleared requests receive an explicit
            # refusal, so an old requestor cannot wait forever for a response.
            x.XSendEvent(d, request.requestor, 0, 0, C.byref(reply))
            x.XSync(d, 0)
            self.check_errors()

    def clear(self):
        self._reading, self._read_done, self._read_data = False, False, None
        self.text, self.data = "", b""
        self.pending, self.sent_payload, self.consumed = {}, False, False
        self.x.XSetSelectionOwner(self.d, self.atoms["CLIPBOARD"], 0, 0)
        self.x.XSync(self.d, 0)
        self.pump()

    def close(self):
        self.clear()
        self.x.XDestroyWindow(self.d, self.window)
