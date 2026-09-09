"""Private X11 input/capture. Imported only inside the per-display worker."""
from __future__ import annotations

import base64
import ctypes as C
import ctypes.util
import io
import time


NATIVE_KEY_NAMES = {
    "Enter": "Return", "Backspace": "BackSpace", "ArrowLeft": "Left", "ArrowRight": "Right",
    "ArrowUp": "Up", "ArrowDown": "Down", "Escape": "Escape", "Tab": "Tab", "Delete": "Delete",
    "Control": "Control_L", "Meta": "Control_L", "Alt": "Alt_L", "Shift": "Shift_L",
    "Home": "Home", "End": "End", "PageUp": "Prior", "PageDown": "Next", "Insert": "Insert",
    "CapsLock": "Caps_Lock", "ContextMenu": "Menu", **{f"F{i}": f"F{i}" for i in range(1, 13)},
}


class WindowAttributes(C.Structure):
    _fields_ = [(name, kind) for name, kind in (
        ("x", C.c_int), ("y", C.c_int), ("width", C.c_int), ("height", C.c_int),
        ("border_width", C.c_int), ("depth", C.c_int), ("visual", C.c_void_p),
        ("root", C.c_ulong), ("window_class", C.c_int), ("bit_gravity", C.c_int),
        ("win_gravity", C.c_int), ("backing_store", C.c_int), ("backing_planes", C.c_ulong),
        ("backing_pixel", C.c_ulong), ("save_under", C.c_int), ("colormap", C.c_ulong),
        ("map_installed", C.c_int), ("map_state", C.c_int), ("all_event_masks", C.c_long),
        ("your_event_mask", C.c_long), ("do_not_propagate_mask", C.c_long),
        ("override_redirect", C.c_int), ("screen", C.c_void_p))]


class NativeX11:
    def __init__(self, display: str, library: str = ""):
        from PIL import features
        if not features.check_feature("xcb"):
            raise RuntimeError("Pillow XCB capture support is required")
        path = library or ctypes.util.find_library("xdo")
        if not path:
            raise RuntimeError("libxdo is required for native desktop input")
        self.lib = C.CDLL(path)
        self.lib.xdo_new.argtypes, self.lib.xdo_new.restype = [C.c_char_p], C.c_void_p
        self.lib.xdo_free.argtypes = [C.c_void_p]
        self.lib.xdo_move_mouse.argtypes = [C.c_void_p, C.c_int, C.c_int, C.c_int]
        self.lib.xdo_mouse_down.argtypes = [C.c_void_p, C.c_ulong, C.c_int]
        self.lib.xdo_mouse_up.argtypes = [C.c_void_p, C.c_ulong, C.c_int]
        self.lib.xdo_enter_text_window.argtypes = [C.c_void_p, C.c_ulong, C.c_char_p, C.c_uint]
        self.lib.xdo_activate_window.argtypes = [C.c_void_p, C.c_ulong]
        for name in ("xdo_send_keysequence_window_down", "xdo_send_keysequence_window_up"):
            getattr(self.lib, name).argtypes = [C.c_void_p, C.c_ulong, C.c_char_p, C.c_uint]
        self.ctx = self.lib.xdo_new(display.encode())
        if not self.ctx:
            raise RuntimeError("private X display is unavailable")
        # xdo_t exposes Display *xdpy as its first ABI field (libxdo xdo.h).
        self.xlib = C.CDLL(ctypes.util.find_library("X11"))
        self.xlib.XSync.argtypes = [C.c_void_p, C.c_int]
        self.xlib.XInternAtom.argtypes = [C.c_void_p, C.c_char_p, C.c_int]
        self.xlib.XInternAtom.restype = C.c_ulong
        self.xlib.XDefaultRootWindow.argtypes = [C.c_void_p]
        self.xlib.XDefaultRootWindow.restype = C.c_ulong
        self.xlib.XGetInputFocus.argtypes = [C.c_void_p, C.POINTER(C.c_ulong), C.POINTER(C.c_int)]
        self.xdisplay = C.cast(self.ctx, C.POINTER(C.c_void_p))[0]
        # Xlib's default BadWindow handler exits the process immediately. A
        # requestor can close during clipboard delivery; retain normal bounded
        # worker cleanup instead of abandoning the owned browser/display.
        self._xerrors = []
        self._xerror_callback = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_void_p)(self._record_xerror)
        self.xlib.XSetErrorHandler.argtypes = [C.c_void_p]
        self.xlib.XSetErrorHandler.restype = C.c_void_p
        self._previous_xerror = self.xlib.XSetErrorHandler(C.cast(self._xerror_callback, C.c_void_p))
        self.display = display
        self.keys: set[str] = set()
        self.buttons: set[int] = set()
        self.xkb = C.CDLL(ctypes.util.find_library("xkbcommon"))
        self.xkb.xkb_utf32_to_keysym.argtypes = [C.c_uint]
        self.xkb.xkb_utf32_to_keysym.restype = C.c_uint
        self.xkb.xkb_keysym_to_utf32.argtypes = [C.c_uint]
        self.xkb.xkb_keysym_to_utf32.restype = C.c_uint
        self.xlib.XKeysymToString.argtypes = [C.c_ulong]
        self.xlib.XKeysymToString.restype = C.c_char_p
        self.xlib.XDisplayKeycodes.argtypes = [C.c_void_p, C.POINTER(C.c_int), C.POINTER(C.c_int)]
        self.xlib.XGetKeyboardMapping.argtypes = [C.c_void_p, C.c_ubyte, C.c_int, C.POINTER(C.c_int)]
        self.xlib.XGetKeyboardMapping.restype = C.POINTER(C.c_ulong)
        self.xlib.XFree.argtypes = [C.c_void_p]
        self.xlib.XGetWindowProperty.argtypes = [C.c_void_p, C.c_ulong, C.c_ulong, C.c_long, C.c_long,
                                                C.c_int, C.c_ulong, C.POINTER(C.c_ulong), C.POINTER(C.c_int),
                                                C.POINTER(C.c_ulong), C.POINTER(C.c_ulong), C.POINTER(C.c_void_p)]
        self.xlib.XGetWindowAttributes.argtypes = [C.c_void_p, C.c_ulong, C.POINTER(WindowAttributes)]
        low, high, per = C.c_int(), C.c_int(), C.c_int()
        self.xlib.XDisplayKeycodes(self.xdisplay, C.byref(low), C.byref(high))
        mapping = self.xlib.XGetKeyboardMapping(self.xdisplay, low.value, high.value - low.value + 1, C.byref(per))
        if not mapping or per.value <= 0:
            raise RuntimeError("native keyboard mapping unavailable")
        try:
            self._mapped_keysyms = set(mapping[:(high.value - low.value + 1) * per.value])
        finally:
            self.xlib.XFree(mapping)
        self.key_characters = "".join(sorted({chr(value) for sym in self._mapped_keysyms
                                              if (value := self.xkb.xkb_keysym_to_utf32(sym)) >= 32
                                              and chr(value).isprintable()}))
        from .native_clipboard import NativeClipboard
        self.clipboard = NativeClipboard(self.xlib, self.xdisplay, self.check_errors)

    def _record_xerror(self, *_):
        self._xerrors.append(True)
        return 0

    def check_errors(self):
        if self._xerrors:
            self._xerrors.clear()
            raise RuntimeError("private X operation failed")

    def _character_symbol(self, char):
        # The US/Arabic XKB map is installed before this client and Chrome.
        # libxkbcommon converts Arabic Unicode to the legacy keysyms present in
        # that standard map; a Uxxxx alias would trigger libxdo's temporary map.
        keysym = self.xkb.xkb_utf32_to_keysym(ord(char))
        name = self.xlib.XKeysymToString(keysym)
        if keysym not in self._mapped_keysyms or not name:
            raise ValueError("character is outside the native US/Arabic keyboard; use text or paste")
        return name.decode("ascii")

    def _call(self, name, *args):
        if getattr(self.lib, name)(self.ctx, *args):
            raise RuntimeError("native input failed")

    def focused_window(self):
        window, revert = C.c_ulong(), C.c_int()
        self.xlib.XGetInputFocus(self.xdisplay, C.byref(window), C.byref(revert))
        return window.value

    def _property(self, window, name):
        atom = self.xlib.XInternAtom(self.xdisplay, name.encode(), 0)
        actual, fmt, count, remain, data = C.c_ulong(), C.c_int(), C.c_ulong(), C.c_ulong(), C.c_void_p()
        status = self.xlib.XGetWindowProperty(self.xdisplay, window, atom, 0, 4096, 0, 0,
                                              C.byref(actual), C.byref(fmt), C.byref(count), C.byref(remain), C.byref(data))
        self.check_errors()
        if status or not data:
            return None
        try:
            if fmt.value == 32:
                return list(C.cast(data, C.POINTER(C.c_ulong))[:count.value])
            if fmt.value == 8:
                return C.string_at(data, count.value)
            return None
        finally:
            self.xlib.XFree(data)

    def browser_window(self, pid):
        root = self.xlib.XDefaultRootWindow(self.xdisplay)
        focused, first = self.focused_window(), None
        for window in self._property(root, "_NET_CLIENT_LIST") or []:
            if (self._property(window, "_NET_WM_PID") == [pid]
                    and self._property(window, "WM_WINDOW_ROLE") == b"browser"):
                attrs = WindowAttributes()
                if self.xlib.XGetWindowAttributes(self.xdisplay, window, C.byref(attrs)) and attrs.map_state == 2:
                    if window == focused:
                        return window
                    if first is None:
                        first = window
        return first

    def focus_browser(self, pid):
        window = self.browser_window(pid)
        if not window:
            return False
        if self.focused_window() != window:
            self._call("xdo_activate_window", window)
            self.sync()
        return self.focused_window() == window

    def sync(self):
        self.xlib.XSync(self.xdisplay, 0)
        self.check_errors()

    def capture(self):
        from PIL import ImageGrab
        frame = ImageGrab.grab(xdisplay=self.display)
        buf = io.BytesIO()
        frame.convert("RGB").save(buf, format="JPEG", quality=85)
        return {"data": base64.b64encode(buf.getvalue()).decode("ascii"),
                "width": frame.width, "height": frame.height, "mime": "image/jpeg"}

    def pointer(self, phase, x, y, buttons=0, **_):
        self._call("xdo_move_mouse", int(x), int(y), 0)
        # DOM buttons uses 1=left, 2=right, 4=middle; X uses 1,3,2.
        desired = {xbutton for mask, xbutton in ((1, 1), (2, 3), (4, 2)) if int(buttons) & mask}
        if phase == "click":
            self._call("xdo_mouse_down", 0, 1)
            self._call("xdo_mouse_up", 0, 1)
            return
        for button in self.buttons - desired:
            self._call("xdo_mouse_up", 0, button)
        for button in desired - self.buttons:
            self._call("xdo_mouse_down", 0, button)
        self.buttons = desired

    def wheel(self, x=0, y=0, delta_x=0, delta_y=0):
        self._call("xdo_move_mouse", int(x), int(y), 0)
        for delta, positive, negative in ((delta_y, 5, 4), (delta_x, 7, 6)):
            for _ in range(min(30, max(1, round(abs(delta) / 100))) if delta else 0):
                button = positive if delta > 0 else negative
                self._call("xdo_mouse_down", 0, button)
                self._call("xdo_mouse_up", 0, button)

    def key(self, phase, key, code="", modifiers=0):
        symbol = NATIVE_KEY_NAMES.get(key, key)
        if len(key) == 1:
            symbol = self._character_symbol(key)
        if not symbol or len(symbol) > 40 or any(c in symbol for c in ("+", "\n", "\x00")):
            raise ValueError("unsupported native key")
        # Reconcile modifiers supplied with printable events (e.g. macOS Cmd→Ctrl).
        if key not in ("Control", "Meta", "Alt", "Shift"):
            for mask, mod in ((1, "Alt_L"), (2, "Control_L"), (8, "Shift_L")):
                active = bool(int(modifiers) & mask)
                if active and mod not in self.keys:
                    self._call("xdo_send_keysequence_window_down", 0, mod.encode(), 0)
                    self.keys.add(mod)
                if not active and mod in self.keys:
                    self._call("xdo_send_keysequence_window_up", 0, mod.encode(), 0)
                    self.keys.discard(mod)
        self._call("xdo_send_keysequence_window_" + ("up" if phase == "up" else "down"), 0, symbol.encode(), 0)
        if phase == "up":
            self.keys.discard(symbol)
        else:
            self.keys.add(symbol)

    def type_mapped(self, text: str, deadline: float):
        # Ordered physical keys are used for runtime-owned destinations. Unlike
        # clipboard delivery, their subsequent Enter shares the same key queue.
        self.release()
        for char in text:
            if time.monotonic() >= deadline:
                raise TimeoutError("native key sequence expired")
            self.key("down", char)
            self.key("up", char)
        self.sync()

    def destination(self, url: str, deadline: float):
        # X server delivery is not Chrome omnibox completion: its focus update
        # can drop early keys. Copy the actual selected text back before Enter.
        # A retry only replaces uncommitted text; it never repeats navigation.
        expected = url.encode("utf-8")
        for _ in range(2):
            self.clipboard.clear()
            self.chord("l", 2)
            self.type_mapped(url, deadline)
            self.chord("a", 2)
            self.chord("c", 2)
            self.sync()
            read_deadline = min(deadline, time.monotonic() + 2)
            if self.clipboard.read_utf8(read_deadline) == expected:
                self.chord("Enter")
                return
        raise RuntimeError("native destination could not be verified")

    def text(self, text: str, deadline: float):
        self.release()
        return self.clipboard.paste(text, deadline, self.chord)

    def chord(self, key, modifiers=0):
        self.release()
        self.key("down", key, modifiers=modifiers)
        self.key("up", key, modifiers=modifiers)
        self.release()

    def release(self):
        for symbol in list(self.keys):
            self._call("xdo_send_keysequence_window_up", 0, symbol.encode(), 0)
            self.keys.discard(symbol)
        for button in list(self.buttons):
            self._call("xdo_mouse_up", 0, button)
            self.buttons.discard(button)

    def release_inputs(self):
        self.release()
        self.clipboard.clear()

    def close(self):
        try:
            self.release()
            self.clipboard.close()
        finally:
            self.lib.xdo_free(self.ctx)
            self.xlib.XSetErrorHandler(self._previous_xerror)
