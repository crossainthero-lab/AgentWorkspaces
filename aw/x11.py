"""Minimal pure-Python X11 client (stdlib only) for ONE workspace's private Xwayland.

Why: labwc 0.9.3 does not announce X11 windows through wlr-foreign-toplevel or
ext-foreign-toplevel-list, so XWayland windows are enumerated and managed over the workspace's own X
server using EWMH (the same way a pager or `wmctrl` would).

SAFETY (same discipline as aw/wl.py):
  * A connection is only made to an explicit display name given by the caller, resolved to
    `/tmp/.X11-unix/X<n>` (never to an abstract socket, never from $DISPLAY of this process).
  * `XConnection.connect_workspace()` additionally verifies, through SO_PEERCRED, that the process
    serving the socket is a descendant of the workspace's compositor. A display that belongs to
    anything else (the user's own Xwayland, another workspace) is refused.
"""
from __future__ import annotations

import os
import socket
import struct
import time
from typing import Any, Dict, List, Optional, Tuple

X_SOCKET_DIR = "/tmp/.X11-unix"


class X11Error(Exception):
    pass


def _pad4(n: int) -> int:
    return (4 - n % 4) % 4


def _ppid(pid: int) -> Optional[int]:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return int(f.read().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def descends_from(pid: int, ancestor: int) -> bool:
    seen = 0
    while pid and pid > 1 and seen < 64:
        if pid == ancestor:
            return True
        pid = _ppid(pid) or 0
        seen += 1
    return False


def socket_path(display: str) -> str:
    """':2' or ':2.0' -> /tmp/.X11-unix/X2. Anything else (host:2, unix:..., absolute) is refused."""
    if not display or not display.startswith(":"):
        raise X11Error(f"unsupported display {display!r} (only local :N displays)")
    num = display[1:].split(".")[0]
    if not num.isdigit():
        raise X11Error(f"unsupported display {display!r}")
    return os.path.join(X_SOCKET_DIR, f"X{int(num)}")


class XConnection:
    def __init__(self, display: str, expect_ancestor_pid: Optional[int] = None, timeout: float = 5.0):
        self.display = display
        path = socket_path(display)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(path)
        except OSError as e:
            s.close()
            raise X11Error(f"cannot connect to {display}: {e}")
        try:
            cred = s.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            self.server_pid = struct.unpack("3i", cred)[0]
        except OSError:
            self.server_pid = 0
        if expect_ancestor_pid is not None and not descends_from(self.server_pid, expect_ancestor_pid):
            s.close()
            raise X11Error(f"{display} is not served by this workspace's compositor tree "
                           f"(server pid {self.server_pid}); refusing to use it")
        self.sock = s
        self._buf = b""
        self._seq = 0
        self._atoms: Dict[str, int] = {}
        self._atom_names: Dict[int, str] = {}
        self._handshake()

    # ---- low level
    def _recv(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise X11Error("X server closed the connection")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _handshake(self):
        # No authorisation data: Xwayland started by the compositor accepts same-uid local clients.
        self.sock.sendall(struct.pack("<BxHHHHxx", 0x6C, 11, 0, 0, 0))
        head = self._recv(8)
        if head[0] != 1:
            extra = struct.unpack("<H", head[6:8])[0] * 4
            reason = self._recv(extra) if extra else b""
            raise X11Error(f"X server refused the connection: {reason[:head[1]].decode(errors='replace')}")
        extra = struct.unpack("<H", head[6:8])[0] * 4
        d = self._recv(extra)
        (self.release, self.rid_base, self.rid_mask, _mb, vlen, _maxreq, nroots, nfmt) = struct.unpack_from(
            "<IIIIHHBB", d, 0)
        off = 32 + vlen + _pad4(vlen) + nfmt * 8
        self.root = struct.unpack_from("<I", d, off)[0]
        self.screen_w, self.screen_h = struct.unpack_from("<HH", d, off + 20)
        self._next_id = 0

    def _request(self, opcode: int, data1: int, body: bytes, reply: bool = True) -> Optional[bytes]:
        total = 4 + len(body)
        assert total % 4 == 0, "request not padded"
        self._seq = (self._seq + 1) & 0xFFFF
        self.sock.sendall(struct.pack("<BBH", opcode, data1, total // 4) + body)
        if not reply:
            return None
        while True:
            h = self._recv(32)
            t = h[0] & 0x7F
            if t == 1:
                extra = struct.unpack_from("<I", h, 4)[0] * 4
                data = h + (self._recv(extra) if extra else b"")
                if struct.unpack_from("<H", h, 2)[0] == self._seq:
                    return data
            elif t == 0:
                if struct.unpack_from("<H", h, 2)[0] == self._seq:
                    code = h[1]
                    names = {1: "BadRequest", 2: "BadValue", 3: "BadWindow", 4: "BadPixmap", 5: "BadAtom",
                             8: "BadMatch", 9: "BadDrawable", 10: "BadAccess"}
                    raise X11Error(f"X error {names.get(code, code)} on request {opcode}")
            # events (and replies to older requests) are ignored: all our requests are synchronous

    def _flush_check(self):
        """Barrier: GetInputFocus round trip, so an earlier no-reply request has been processed and
        any X error for it has been seen."""
        self._request(43, 0, b"")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    # ---- atoms
    def atom(self, name: str) -> int:
        a = self._atoms.get(name)
        if a is None:
            nb = name.encode()
            r = self._request(16, 0, struct.pack("<HH", len(nb), 0) + nb + b"\0" * _pad4(len(nb)))
            a = struct.unpack_from("<I", r, 8)[0]
            self._atoms[name] = a
            self._atom_names[a] = name
        return a

    def atom_name(self, a: int) -> str:
        n = self._atom_names.get(a)
        if n is None:
            r = self._request(17, 0, struct.pack("<I", a))
            ln = struct.unpack_from("<H", r, 8)[0]
            n = r[32:32 + ln].decode(errors="replace")
            self._atom_names[a] = n
            self._atoms[n] = a
        return n

    # ---- properties
    def get_property(self, win: int, prop: str, ptype: int = 0, length: int = 4096) -> Tuple[int, int, bytes]:
        """-> (type atom, format, raw bytes). (0, 0, b'') when the property is absent."""
        r = self._request(20, 0, struct.pack("<IIIII", win, self.atom(prop), ptype, 0, length))
        fmt = r[1]
        typ = struct.unpack_from("<I", r, 8)[0]
        n = struct.unpack_from("<I", r, 16)[0]
        return typ, fmt, r[32:32 + n * (fmt // 8)] if fmt else b""

    def prop_cardinals(self, win: int, prop: str) -> List[int]:
        typ, fmt, raw = self.get_property(win, prop)
        if fmt != 32:
            return []
        return list(struct.unpack(f"<{len(raw) // 4}I", raw))

    def prop_text(self, win: int, prop: str) -> str:
        typ, fmt, raw = self.get_property(win, prop)
        return raw.decode("utf-8", errors="replace") if fmt == 8 else ""

    def prop_atoms(self, win: int, prop: str) -> List[str]:
        return [self.atom_name(a) for a in self.prop_cardinals(win, prop)]

    # ---- window info
    def geometry(self, win: int) -> Tuple[int, int, int, int]:
        r = self._request(14, 0, struct.pack("<I", win))
        x, y, w, h = struct.unpack_from("<hhHH", r, 12)
        return x, y, w, h

    def abs_position(self, win: int) -> Tuple[int, int]:
        r = self._request(40, 0, struct.pack("<IIhh", win, self.root, 0, 0))
        return struct.unpack_from("<hh", r, 12)

    def attributes(self, win: int) -> Dict[str, Any]:
        r = self._request(3, 0, struct.pack("<I", win))
        return {"map_state": r[26], "override_redirect": bool(r[27])}

    def children(self, win: Optional[int] = None) -> List[int]:
        r = self._request(15, 0, struct.pack("<I", win or self.root))
        n = struct.unpack_from("<H", r, 16)[0]
        return list(struct.unpack_from(f"<{n}I", r, 32))

    def input_focus(self) -> int:
        r = self._request(43, 0, b"")
        return struct.unpack_from("<I", r, 8)[0]

    # ---- requests that change things
    def configure(self, win: int, x=None, y=None, width=None, height=None):
        mask, vals = 0, []
        for bit, v in ((1, x), (2, y), (4, width), (8, height)):
            if v is not None:
                mask |= bit
                vals.append(int(v))
        if not mask:
            return
        body = struct.pack("<IHH", win, mask, 0) + b"".join(struct.pack("<i", v) for v in vals)
        self._request(12, 0, body, reply=False)
        self._flush_check()

    def send_client_message(self, win: int, mtype: str, data: List[int], fmt: int = 32,
                            to_root_redirect: bool = True):
        data = (list(data) + [0] * 5)[:5]
        ev = struct.pack("<BBHII5I", 33, fmt, 0, win, self.atom(mtype), *data)
        mask = 0x100000 | 0x80000 if to_root_redirect else 0     # SubstructureRedirect|Notify
        # SendEvent: destination, event mask, event(32); propagate flag (data1) = 0
        body = struct.pack("<II", self.root, mask) + ev
        self._request(25, 0, body, reply=False)
        self._flush_check()


# ---- EWMH layer ---------------------------------------------------------------------------------

class X11Windows:
    """EWMH view of one workspace's X server."""

    def __init__(self, conn: XConnection):
        self.c = conn

    @classmethod
    def for_workspace(cls, display: str, compositor_pid: int) -> "X11Windows":
        return cls(XConnection(display, expect_ancestor_pid=compositor_pid))

    def close(self):
        self.c.close()

    def client_list(self) -> List[int]:
        return self.c.prop_cardinals(self.c.root, "_NET_CLIENT_LIST")

    def active(self) -> int:
        a = self.c.prop_cardinals(self.c.root, "_NET_ACTIVE_WINDOW")
        return a[0] if a else 0

    def list(self) -> List[Dict[str, Any]]:
        c = self.c
        active = self.active()
        out = []
        for xid in self.client_list():
            try:
                out.append(self._info(xid, active))
            except X11Error:
                continue    # window vanished between the list and the query
        return out

    def _info(self, xid: int, active: int) -> Dict[str, Any]:
        c = self.c
        title = c.prop_text(xid, "_NET_WM_NAME") or c.prop_text(xid, "WM_NAME")
        cls_raw = c.prop_text(xid, "WM_CLASS").split("\0")
        instance = cls_raw[0] if cls_raw else ""
        klass = cls_raw[1] if len(cls_raw) > 1 else ""
        pid = (c.prop_cardinals(xid, "_NET_WM_PID") or [None])[0]
        state = set(c.prop_atoms(xid, "_NET_WM_STATE"))
        gx, gy, gw, gh = c.geometry(xid)
        try:
            ax, ay = c.abs_position(xid)
        except X11Error:
            ax, ay = gx, gy
        wm_state = c.get_property(xid, "WM_STATE", 0, 2)[2]
        iconic = len(wm_state) >= 4 and struct.unpack_from("<I", wm_state, 0)[0] == 3
        return {
            "xid": xid, "title": title, "class": klass, "instance": instance, "pid": pid,
            "focused": xid == active or "_NET_WM_STATE_FOCUSED" in state,
            "maximized": {"_NET_WM_STATE_MAXIMIZED_VERT", "_NET_WM_STATE_MAXIMIZED_HORZ"} <= state,
            "minimized": "_NET_WM_STATE_HIDDEN" in state or iconic,
            "fullscreen": "_NET_WM_STATE_FULLSCREEN" in state,
            "geometry": {"x": ax, "y": ay, "width": gw, "height": gh},
        }

    # ---- management (all through the window manager, i.e. labwc, as a pager would)
    def _state(self, xid: int, op: int, *atoms: str):
        a = [self.c.atom(n) for n in atoms] + [0, 0]
        self.c.send_client_message(xid, "_NET_WM_STATE", [op, a[0], a[1], 1, 0])

    def focus(self, xid: int):
        self.c.send_client_message(xid, "_NET_ACTIVE_WINDOW", [2, 0, 0])

    def close_window(self, xid: int):
        self.c.send_client_message(xid, "_NET_CLOSE_WINDOW", [0, 2])

    def maximize(self, xid: int, on: bool = True):
        self._state(xid, 1 if on else 0, "_NET_WM_STATE_MAXIMIZED_VERT", "_NET_WM_STATE_MAXIMIZED_HORZ")

    def fullscreen(self, xid: int, on: bool = True):
        self._state(xid, 1 if on else 0, "_NET_WM_STATE_FULLSCREEN")

    def minimize(self, xid: int):
        self.c.send_client_message(xid, "WM_CHANGE_STATE", [3])     # IconicState (ICCCM 4.1.4)

    def unminimize(self, xid: int):
        self._state(xid, 0, "_NET_WM_STATE_HIDDEN")
        self.focus(xid)

    def configure(self, xid: int, x=None, y=None, width=None, height=None):
        self.c.configure(xid, x, y, width, height)
