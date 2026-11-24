"""WorkspaceSession: one client connection to ONE workspace compositor.

Everything an agent can do to a workspace goes through this class, and a session is only ever
built from the workspace compositor's own socket path. There is no code path here that reads
WAYLAND_DISPLAY or DISPLAY, and no xdotool/ydotool/uinput.
"""
from __future__ import annotations

import os
import select
import struct
import threading
import time
import zlib
from typing import Any, Dict, List, Optional

from . import wl, xkb

BTN = {"left": 0x110, "right": 0x111, "middle": 0x112, "back": 0x113, "forward": 0x114}
TEXT_MIMES = ("text/plain;charset=utf-8", "text/plain", "UTF8_STRING", "STRING", "TEXT")

STATE_MAXIMIZED, STATE_MINIMIZED, STATE_ACTIVATED, STATE_FULLSCREEN = 0, 1, 2, 3


class SessionError(Exception):
    pass


def encode_png(width: int, height: int, rgb: bytes, level: int = 1) -> bytes:
    stride = width * 3
    raw = bytearray(height * (stride + 1))
    mv = memoryview(rgb)
    for y in range(height):
        o = y * (stride + 1)
        raw[o + 1:o + 1 + stride] = mv[y * stride:(y + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), level)) + chunk(b"IEND", b""))


def to_rgb(buf, width: int, height: int, stride: int, fourcc: int, y_invert: bool = False) -> bytes:
    """Convert a 32-bit wl_shm buffer to packed RGB. Supports the 8-bit-per-channel formats."""
    # wl_shm: 0=argb8888, 1=xrgb8888 -> bytes B,G,R,x ; DRM 'AB24'/'XB24' -> bytes R,G,B,x
    if fourcc in (0x34324742, 0x34324752):  # 'BG24' (memory R,G,B) / 'RG24' (memory B,G,R), 3 bytes/pixel
        mv = memoryview(buf)
        out = bytearray(width * height * 3)
        rows = range(height - 1, -1, -1) if y_invert else range(height)
        for oy, y in enumerate(rows):
            row = mv[y * stride:y * stride + width * 3]
            o = oy * width * 3
            if fourcc == 0x34324742:
                out[o:o + width * 3] = row
            else:
                out[o + 0:o + width * 3:3] = row[2::3]
                out[o + 1:o + width * 3:3] = row[1::3]
                out[o + 2:o + width * 3:3] = row[0::3]
        return bytes(out)
    if fourcc in (0, 1, 0x34325258, 0x34324152):  # argb/xrgb (+ drm XR24/AR24 aliases)
        ri, gi, bi = 2, 1, 0
    elif fourcc in (0x34324241, 0x34324258):  # AB24 / XB24
        ri, gi, bi = 0, 1, 2
    else:
        raise SessionError(f"unsupported shm format 0x{fourcc:08x}")
    out = bytearray(width * height * 3)
    mv = memoryview(buf)
    rows = range(height - 1, -1, -1) if y_invert else range(height)
    row_out = width * 3
    for oy, y in enumerate(rows):
        row = mv[y * stride:y * stride + width * 4]
        o = oy * row_out
        out[o + 0:o + row_out:3] = row[ri::4]
        out[o + 1:o + row_out:3] = row[gi::4]
        out[o + 2:o + row_out:3] = row[bi::4]
    return bytes(out)


class WorkspaceSession:
    def __init__(self, socket_path: str, layout: str = "us", expect_pid: Optional[int] = None):
        # raises if the socket is missing or (with expect_pid) served by another process: fail closed
        self.conn = wl.Connection(socket_path, expect_pid=expect_pid)
        self.epoch = time.monotonic_ns()         # distinguishes this connection's toplevel ids from a later one's
        self.on_windows = None                   # called (from the session thread) when a toplevel appears, changes or closes
        self.socket_path = socket_path
        self.lock = threading.RLock()
        self.keymap = xkb.Keymap(layout)
        self._running = True
        self._wake_r, self._wake_w = os.pipe()
        self._cond = threading.Condition()
        c = self.conn
        self.seat = c.bind("wl_seat", 7)
        self.shm = c.bind("wl_shm", 1)
        self.outputs: List[Dict[str, Any]] = []
        self._bind_outputs()
        self.screencopy = c.bind("zwlr_screencopy_manager_v1", 3)
        self.vptr_mgr = c.bind("zwlr_virtual_pointer_manager_v1", 2)
        self.vkb_mgr = c.bind("zwp_virtual_keyboard_manager_v1", 1)
        self.toplevel_mgr = c.bind("zwlr_foreign_toplevel_manager_v1", 3)
        self.data_mgr = c.bind("zwlr_data_control_manager_v1", 2)
        self.output_mgr = c.bind("zwlr_output_manager_v1", 4)
        for name, obj in (("screencopy", self.screencopy), ("virtual-pointer", self.vptr_mgr),
                          ("virtual-keyboard", self.vkb_mgr)):
            if obj is None:
                raise SessionError(f"workspace compositor lacks required protocol: {name}")
        self.pointer = None
        self.keyboard = None
        self.pos = (0.0, 0.0)
        self.mods = 0
        self._pressed_mods = 0
        self._shm_cache: Dict[tuple, Any] = {}
        self.toplevels: Dict[int, Dict[str, Any]] = {}
        self._tl_seq = 0
        self._selection = None
        self._sources: List[Any] = []
        self.heads: Dict[int, Dict[str, Any]] = {}
        self._out_serial = 0
        self._setup_toplevels()
        self._setup_data_device()
        self._setup_output_mgmt()
        c.roundtrip()
        c.roundtrip()
        self._thread = threading.Thread(target=self._pump, name="aw-session", daemon=True)
        self._thread.start()

    # ---- plumbing
    def _bind_outputs(self):
        c = self.conn
        g = c.globals.get("wl_output")
        if not g:
            raise SessionError("no wl_output")
        out = c.bind("wl_output", 4)
        info = {"proxy": out, "name": None, "w": 0, "h": 0, "scale": 1}
        out.on("name", lambda n: info.__setitem__("name", n))
        out.on("mode", lambda flags, w, h, r: info.update(w=w, h=h) if flags & 1 else None)
        out.on("scale", lambda s: info.__setitem__("scale", s))
        out.on("geometry", lambda *a: None)
        self.outputs.append(info)
        c.roundtrip()

    def _pump(self):
        while self._running:
            try:
                r, _, _ = select.select([self.conn.sock, self._wake_r], [], [], 1.0)
            except (OSError, ValueError):
                self._running = False
                return
            if not self._running:
                return
            if self.conn.sock in r:
                with self.lock:
                    try:
                        self.conn.dispatch(0)
                    except (wl.WaylandError, OSError, ValueError):
                        self._running = False
                        with self._cond:
                            self._cond.notify_all()
                        return
                with self._cond:
                    self._cond.notify_all()

    def _wait_for(self, pred, timeout=5.0, what="event"):
        deadline = time.monotonic() + timeout
        with self.lock:
            while not pred():
                left = deadline - time.monotonic()
                if left <= 0:
                    raise SessionError(f"timed out waiting for {what}")
                self.conn.dispatch(min(left, 0.2))

    @property
    def alive(self) -> bool:
        return self._running

    def close(self):
        self._running = False
        try:
            os.write(self._wake_w, b"x")
        except OSError:
            pass
        self.conn.close()
        for fd in (self._wake_r, self._wake_w):
            try:
                os.close(fd)
            except OSError:
                pass

    # ---- display info
    def display_info(self) -> Dict[str, Any]:
        with self.lock:
            heads = []
            for h in self.heads.values():
                if h.get("closed"):
                    continue
                heads.append({"name": h.get("name"), "enabled": h.get("enabled"),
                              "width": h.get("w"), "height": h.get("h"), "refresh_hz": h.get("refresh"),
                              "scale": h.get("scale"), "x": h.get("x", 0), "y": h.get("y", 0)})
            o = self.outputs[0]
            return {"outputs": heads or [{"name": o["name"], "width": o["w"], "height": o["h"],
                                          "scale": o["scale"]}],
                    "pointer": {"x": self.pos[0], "y": self.pos[1]}}

    def _setup_output_mgmt(self):
        if not self.output_mgr:
            return

        def on_head(head):
            st = {"proxy": head, "modes": {}, "closed": False}
            self.heads[head.id] = st
            head.on("name", lambda n: st.__setitem__("name", n))
            head.on("enabled", lambda e: st.__setitem__("enabled", bool(e)))
            head.on("scale", lambda s: st.__setitem__("scale", s))
            head.on("position", lambda x, y: st.update(x=x, y=y))
            head.on("finished", lambda: st.__setitem__("closed", True))

            def on_mode(mode):
                m = {}
                st["modes"][mode.id] = m
                mode.on("size", lambda w, h: m.update(w=w, h=h))
                mode.on("refresh", lambda r: m.__setitem__("refresh", r))
            head.on("mode", on_mode)

            def on_cur(mode):
                m = st["modes"].get(mode.id, {})
                st.update(w=m.get("w"), h=m.get("h"), refresh=(m.get("refresh") or 0) / 1000.0)
            head.on("current_mode", on_cur)
        self.output_mgr.on("head", on_head)
        self.output_mgr.on("done", lambda serial: setattr(self, "_out_serial", serial))

    def set_display(self, width: int, height: int, scale: float = 1.0, refresh_hz: float = 60.0):
        if not self.output_mgr:
            raise SessionError("compositor has no wlr-output-management")
        with self.lock:
            heads = [h for h in self.heads.values() if not h["closed"]]
            if not heads:
                raise SessionError("no output heads")
            head = heads[0]
            res: List[str] = []
            cfg = self.output_mgr.create_configuration(self._out_serial)
            cfg.on("succeeded", lambda: res.append("ok"))
            cfg.on("failed", lambda: res.append("failed"))
            cfg.on("cancelled", lambda: res.append("cancelled"))
            hc = cfg.enable_head(head["proxy"])
            hc.set_custom_mode(int(width), int(height), int(refresh_hz * 1000))
            hc.set_scale(float(scale))
            cfg.apply()
            self._wait_for(lambda: bool(res), 5, "output configuration result")
            if res[0] != "ok":
                raise SessionError(f"display configuration {res[0]}")
            self.conn.roundtrip()
            self._shm_cache.clear()
        time.sleep(0.05)
        with self.lock:
            self.conn.roundtrip()
        return self.display_info()

    # ---- screenshots
    def _shm_buffer(self, w, h, stride, fmt):
        key = (w, h, stride, fmt)
        ent = self._shm_cache.get(key)
        if ent:
            return ent
        for _, old in list(self._shm_cache.items()):
            old[3].close()
        self._shm_cache.clear()
        size = stride * h
        fd, mm = wl.make_shm_file(size)
        pool = self.shm.create_pool(fd, size)
        buf = pool.create_buffer(0, w, h, stride, fmt)
        pool.destroy()
        os.close(fd)
        ent = (buf, mm, size, mm)
        self._shm_cache[key] = ent
        return ent

    def capture_raw(self, region=None, cursor: bool = False):
        """Return (width, height, rgb_bytes) of the workspace output (optionally a region)."""
        with self.lock:
            out = self.outputs[0]["proxy"]
            if region:
                x, y, w, h = [int(v) for v in region]
                frame = self.screencopy.capture_output_region(1 if cursor else 0, out, x, y, w, h)
            else:
                frame = self.screencopy.capture_output(1 if cursor else 0, out)
            st: Dict[str, Any] = {}
            frame.on("buffer", lambda fmt, w, h, stride: st.update(fmt=fmt, w=w, h=h, stride=stride))
            frame.on("flags", lambda f: st.__setitem__("flags", f))
            frame.on("ready", lambda *a: st.__setitem__("ready", True))
            frame.on("failed", lambda: st.__setitem__("failed", True))
            frame.on("linux_dmabuf", lambda *a: None)
            frame.on("damage", lambda *a: None)
            frame.on("buffer_done", lambda: st.__setitem__("bdone", True))
            self._wait_for(lambda: ("bdone" in st) or ("fmt" in st and frame.version < 3) or "failed" in st,
                           5, "screencopy buffer info")
            if "failed" in st:
                raise SessionError("screencopy failed")
            buf, mm, size, _ = self._shm_buffer(st["w"], st["h"], st["stride"], st["fmt"])
            frame.copy(buf)
            self._wait_for(lambda: "ready" in st or "failed" in st, 5, "screencopy ready")
            if "failed" in st:
                self._shm_cache.clear()
                raise SessionError("screencopy copy failed")
            rgb = to_rgb(mm, st["w"], st["h"], st["stride"], st["fmt"], bool(st.get("flags", 0) & 1))
            frame.destroy()
            return st["w"], st["h"], rgb

    def capture_native(self, wait_damage: bool = False, timeout: float = 5.0, cursor: bool = True):
        """Return (width, height, stride, fourcc, bytes, y_invert) of the raw shm frame.
        With wait_damage the call blocks until the output changes (wlr-screencopy copy_with_damage),
        or returns None on timeout. Used by the viewer: no per-pixel Python work."""
        with self.lock:
            out = self.outputs[0]["proxy"]
            frame = self.screencopy.capture_output(1 if cursor else 0, out)
            st: Dict[str, Any] = {}
            frame.on("buffer", lambda fmt, w, h, stride: st.update(fmt=fmt, w=w, h=h, stride=stride))
            frame.on("flags", lambda f: st.__setitem__("flags", f))
            frame.on("ready", lambda *a: st.__setitem__("ready", True))
            frame.on("failed", lambda: st.__setitem__("failed", True))
            frame.on("linux_dmabuf", lambda *a: None)
            frame.on("damage", lambda *a: None)
            frame.on("buffer_done", lambda: st.__setitem__("bdone", True))
            self._wait_for(lambda: "bdone" in st or "failed" in st, 5, "screencopy buffer info")
            if "failed" in st:
                raise SessionError("screencopy failed")
            buf, mm, size, _ = self._shm_buffer(st["w"], st["h"], st["stride"], st["fmt"])
            if wait_damage:
                frame.copy_with_damage(buf)
            else:
                frame.copy(buf)
        # wait outside the long lock hold so other threads can still use this session
        deadline = time.monotonic() + timeout
        with self._cond:
            while not ("ready" in st or "failed" in st):
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._cond.wait(left)
        if "ready" not in st and "failed" not in st:
            with self.lock:
                frame.destroy()
            return None
        with self.lock:
            if "failed" in st:
                self._shm_cache.clear()
                raise SessionError("screencopy copy failed")
            data = bytes(mm[:size])
            frame.destroy()
        return st["w"], st["h"], st["stride"], st["fmt"], data, bool(st.get("flags", 0) & 1)

    def screenshot_png(self, region=None, cursor: bool = False) -> bytes:
        w, h, rgb = self.capture_raw(region, cursor)
        return encode_png(w, h, rgb)

    # ---- input: pointer
    def _ensure_pointer(self):
        if self.pointer is None:
            self.pointer = self.vptr_mgr.create_virtual_pointer_with_output(self.seat, self.outputs[0]["proxy"])
            self.conn.roundtrip()

    @staticmethod
    def _t() -> int:
        return int(time.monotonic() * 1000) & 0xFFFFFFFF

    def _logical_extent(self):
        o = self.outputs[0]
        for h in self.heads.values():
            if not h.get("closed") and h.get("w"):
                sc = h.get("scale") or 1.0
                return max(1, int(round(h["w"] / sc))), max(1, int(round(h["h"] / sc)))
        return max(o["w"], 1), max(o["h"], 1)

    def pointer_move(self, x: float, y: float):
        with self.lock:
            self._ensure_pointer()
            ew, eh = self._logical_extent()
            x = min(max(x, 0), ew - 1)
            y = min(max(y, 0), eh - 1)
            self.pointer.motion_absolute(self._t(), int(x), int(y), ew, eh)
            self.pointer.frame()
            self.pos = (x, y)

    def pointer_button(self, button: str = "left", down: bool = True):
        code = BTN[button] if isinstance(button, str) else int(button)
        with self.lock:
            self._ensure_pointer()
            self.pointer.button(self._t(), code, 1 if down else 0)
            self.pointer.frame()

    def click(self, x: Optional[float] = None, y: Optional[float] = None, button: str = "left",
              count: int = 1):
        if x is not None:
            self.pointer_move(x, y)
            time.sleep(0.04)
            # some compositors drop the very first absolute motion after a virtual pointer is created;
            # a 1px nudge makes sure the target surface has received enter + motion before the button
            self.pointer_move(x + (1 if x < self._logical_extent()[0] - 2 else -1), y)
            time.sleep(0.02)
            self.pointer_move(x, y)
            time.sleep(0.03)
        for _ in range(count):
            self.pointer_button(button, True)
            time.sleep(0.02)
            self.pointer_button(button, False)
            time.sleep(0.03)

    def drag(self, x1, y1, x2, y2, button="left", steps: int = 12):
        self.pointer_move(x1, y1)
        time.sleep(0.05)
        self.pointer_button(button, True)
        for i in range(1, steps + 1):
            self.pointer_move(x1 + (x2 - x1) * i / steps, y1 + (y2 - y1) * i / steps)
            time.sleep(0.01)
        self.pointer_button(button, False)

    def scroll(self, dx: float = 0, dy: float = 0, x: Optional[float] = None, y: Optional[float] = None):
        """dy > 0 scrolls down (content moves up). Units: wheel notches."""
        if x is not None:
            self.pointer_move(x, y)
        with self.lock:
            self._ensure_pointer()
            t = self._t()
            if dy:
                self.pointer.axis_source(0)
                self.pointer.axis_discrete(t, 0, dy * 15.0, int(round(dy)) or (1 if dy > 0 else -1))
            if dx:
                self.pointer.axis_source(0)
                self.pointer.axis_discrete(t, 1, dx * 15.0, int(round(dx)) or (1 if dx > 0 else -1))
            self.pointer.frame()

    # ---- input: keyboard
    def _ensure_keyboard(self):
        if self.keyboard is None:
            self.keyboard = self.vkb_mgr.create_virtual_keyboard(self.seat)
            fd, mm = wl.make_shm_file(len(self.keymap.text))
            mm[:] = self.keymap.text
            self.keyboard.keymap(1, fd, len(self.keymap.text))
            mm.close()
            os.close(fd)
            self.conn.roundtrip()

    def _set_mods(self, mask: int):
        self.keyboard.modifiers(mask, 0, 0, 0)

    def _key(self, code: int, down: bool):
        self.keyboard.key(self._t(), code, 1 if down else 0)

    def key(self, combo: str, hold_ms: int = 30):
        """combo like 'ctrl+shift+t', 'Return', 'alt+F4', 'a'."""
        parts = [p for p in combo.replace(" ", "").split("+") if p]
        if combo.endswith("++"):
            parts = [p for p in combo[:-2].split("+") if p] + ["plus"]
        mods = [p.lower() for p in parts[:-1]]
        name = parts[-1].lower()
        if name == "plus":
            name = "equal"
            mods.append("shift")
        with self.lock:
            self._ensure_keyboard()
            for m in mods:
                if m not in xkb.MODIFIER_KEYS:
                    raise SessionError(f"unknown modifier {m!r}")
            mask = self.keymap.mod_mask(*mods)
            code = xkb.KEYS.get(name)
            if code is None and len(name) == 1:
                ck = self.keymap.char_to_key(parts[-1])
                if ck:
                    code = ck[0]
                    mask |= ck[1]
            if code is None:
                raise SessionError(f"unknown key {parts[-1]!r}")
            for m in mods:
                self._key(xkb.MODIFIER_KEYS[m], True)
            self._set_mods(mask)
            self._key(code, True)
        time.sleep(hold_ms / 1000.0)
        with self.lock:
            self._key(code, False)
            for m in reversed(mods):
                self._key(xkb.MODIFIER_KEYS[m], False)
            self._set_mods(0)
            self.conn.roundtrip()

    def raw_key(self, code: int, down: bool, mods: int):
        """Forward one evdev key event with an explicit modifier mask (used by the viewer for the human)."""
        with self.lock:
            self._ensure_keyboard()
            self._set_mods(mods)
            self._key(code, down)

    def type_text(self, text: str, delay_ms: int = 4):
        with self.lock:
            self._ensure_keyboard()
        for ch in text:
            ck = self.keymap.char_to_key(ch)
            if ck is None:
                self._type_via_clipboard(ch)
                continue
            code, mask = ck
            with self.lock:
                if mask:
                    self._set_mods(mask)
                self._key(code, True)
                self._key(code, False)
                if mask:
                    self._set_mods(0)
            if delay_ms:
                time.sleep(delay_ms / 1000.0)
        with self.lock:
            self.conn.roundtrip()

    def _type_via_clipboard(self, ch: str):
        """Characters absent from the layout are pasted through this workspace's own clipboard."""
        old = None
        try:
            old = self.get_clipboard()
        except SessionError:
            pass
        self.set_clipboard(ch)
        time.sleep(0.05)
        self.key("ctrl+v")
        time.sleep(0.05)
        if old is not None:
            self.set_clipboard(old)

    # ---- windows
    def _setup_toplevels(self):
        if not self.toplevel_mgr:
            return

        def on_toplevel(h):
            self._tl_seq += 1
            info = {"id": self._tl_seq, "title": "", "app_id": "", "state": [], "proxy": h,
                    "closed": False, "ready": False}
            self.toplevels[info["id"]] = info
            h.on("title", lambda t: info.__setitem__("title", t or ""))
            h.on("app_id", lambda a: info.__setitem__("app_id", a or ""))

            def st(arr):
                info["state"] = list(struct.unpack(f"<{len(arr) // 4}I", arr))
            h.on("state", st)
            h.on("done", lambda: (info.__setitem__("ready", True), self._windows_changed()))
            h.on("closed", lambda: (info.update(closed=True), self._windows_changed()))
            h.on("output_enter", lambda o: None)
            h.on("output_leave", lambda o: None)
            h.on("parent", lambda p: None)
        self.toplevel_mgr.on("toplevel", on_toplevel)
        self.toplevel_mgr.on("finished", lambda: None)

    def _windows_changed(self):
        cb = self.on_windows
        if cb:
            try:
                cb()
            except Exception:
                pass

    def toplevels_snapshot(self) -> List[Dict[str, Any]]:
        """Windows as last reported by the compositor (no round trip; safe to call often)."""
        with self.lock:
            return [{"id": tid, "title": t["title"], "app_id": t["app_id"],
                     "focused": STATE_ACTIVATED in t["state"]}
                    for tid, t in self.toplevels.items() if not t["closed"]]

    def list_windows(self) -> List[Dict[str, Any]]:
        with self.lock:
            self.conn.roundtrip()
            out = []
            for tid, t in list(self.toplevels.items()):
                if t["closed"]:
                    del self.toplevels[tid]
                    continue
                out.append({"id": tid, "title": t["title"], "app_id": t["app_id"],
                            "focused": STATE_ACTIVATED in t["state"],
                            "maximized": STATE_MAXIMIZED in t["state"],
                            "minimized": STATE_MINIMIZED in t["state"],
                            "fullscreen": STATE_FULLSCREEN in t["state"]})
            return out

    def window_action(self, window_id: int, action: str):
        with self.lock:
            self.conn.roundtrip()
            t = self.toplevels.get(int(window_id))
            if not t or t["closed"]:
                raise SessionError(f"no such window {window_id}")
            h = t["proxy"]
            acts = {
                "focus": lambda: h.activate(self.seat), "activate": lambda: h.activate(self.seat),
                "close": h.close, "maximize": h.set_maximized, "unmaximize": h.unset_maximized,
                "restore": h.unset_maximized, "minimize": h.set_minimized,
                "unminimize": h.unset_minimized, "fullscreen": lambda: h.set_fullscreen(None),
                "unfullscreen": h.unset_fullscreen,
            }
            if action not in acts:
                raise SessionError(f"unknown window action {action!r}; one of {sorted(acts)}")
            acts[action]()
            self.conn.roundtrip()
        time.sleep(0.05)
        with self.lock:
            self.conn.roundtrip()

    # ---- clipboard (this workspace only)
    def _setup_data_device(self):
        if not self.data_mgr:
            return
        self._data_dev = self.data_mgr.get_data_device(self.seat)
        self._offers: Dict[int, List[str]] = {}

        def on_offer(offer):
            mimes: List[str] = []
            self._offers[offer.id] = mimes
            offer.on("offer", lambda m: mimes.append(m))
        self._data_dev.on("data_offer", on_offer)
        self._data_dev.on("selection", lambda o: setattr(self, "_selection", o))
        self._data_dev.on("primary_selection", lambda o: None)
        self._data_dev.on("finished", lambda: None)

    def get_clipboard(self, timeout: float = 2.0) -> Optional[str]:
        with self.lock:
            self.conn.roundtrip()
            off = self._selection
            if off is None:
                return None
            mimes = self._offers.get(off.id, [])
            mime = next((m for m in TEXT_MIMES if m in mimes), None)
            if mime is None:
                return None
            r, w = os.pipe()
            off.receive(mime, w)
            os.close(w)
            self.conn.roundtrip()
        try:
            data = b""
            deadline = time.monotonic() + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                rr, _, _ = select.select([r], [], [], left)
                if not rr:
                    break
                chunk = os.read(r, 65536)
                if not chunk:
                    break
                data += chunk
        finally:
            os.close(r)
        return data.decode("utf-8", "replace")

    def set_clipboard(self, text: str):
        if not self.data_mgr:
            raise SessionError("compositor has no data-control protocol")
        data = text.encode()
        with self.lock:
            src = self.data_mgr.create_data_source()
            for m in TEXT_MIMES[:3]:
                src.offer(m)

            def on_send(mime, fd):
                try:
                    with os.fdopen(fd, "wb", closefd=True) as f:
                        f.write(data)
                except OSError:
                    pass

            def on_cancel():
                if src in self._sources:
                    self._sources.remove(src)
            src.on("send", on_send)
            src.on("cancelled", on_cancel)
            self._sources.append(src)
            self._data_dev.set_selection(src)
            self.conn.roundtrip()
            while len(self._sources) > 4:
                self._sources.pop(0).destroy()
