"""One consistent window list and window management for a workspace.

Sources, merged:
  * Wayland toplevels announced by the workspace compositor (wlr-foreign-toplevel, via WorkspaceSession).
    labwc announces an X11 window there too, but only when the X11 client accepts keyboard focus
    (ICCCM "input model" other than No Input; xeyes/xclock do not, xmessage/xterm/Chrome do).
  * X11 windows of THAT workspace's private Xwayland (aw/x11.py, EWMH). Authoritative for every X11
    window: exact geometry, class, instance, pid, state.

Merging: an X11 window and the Wayland toplevel labwc announced for it are one window (matched on
title + app id == WM_CLASS class/instance, paired in creation order) and are reported once, as `x11`.

Window management:
  * X11 windows: EWMH client messages and ConfigureWindow, which the window manager (labwc) honours.
  * Wayland windows: focus/close/maximize/minimize/fullscreen through wlr-foreign-toplevel. Move and
    resize have no protocol in labwc 0.9.3, so they use labwc's own `MoveTo`/`ResizeTo` actions bound to
    a key in the workspace's private rc.xml and fired through the workspace's own virtual keyboard
    (see Placer). Nothing outside the workspace is addressed.

Geometry contract: `geometry` is the content rectangle in logical output pixels (what a screenshot
shows of the app, excluding titlebar and border). X11: read from the X server (exact). Wayland:
labwc has no way to report it, so it is derived for maximized/fullscreen windows, remembered for
windows this daemon placed (`geometry_source: "tracked"`), and otherwise null ("unknown").
"""
from __future__ import annotations

import os
import signal
import threading
import time
from typing import Any, Callable, Dict, List, Optional
from xml.sax.saxutils import quoteattr

from . import x11

# labwc "Clearlooks" theme as written by aw/manager.py: 1 px border, 26 px titlebar (measured; see docs/windows.md).
BORDER = 1
TITLE_H = 26

NO_GEOMETRY = {"x": None, "y": None, "width": None, "height": None}


class WindowError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def render_rc(window_policy: str, extra_keybinds: str = "") -> str:
    rule = ('<windowRules><windowRule identifier="*"><action name="Maximize"/></windowRule></windowRules>'
            if window_policy == "maximized" else "")
    return f"""<?xml version="1.0"?>
<labwc_config>
  <core><gap>0</gap></core>
  <theme><name>Clearlooks</name><cornerRadius>0</cornerRadius></theme>
  <focus><followMouse>no</followMouse><raiseOnFocus>yes</raiseOnFocus></focus>
  <keyboard><default /><keybind key="A-Tab"><action name="NextWindow"/></keybind>
    <keybind key="A-F4"><action name="Close"/></keybind>
    <keybind key="W-Up"><action name="ToggleMaximize"/></keybind>
    {extra_keybinds}</keyboard>
  <mouse><default /></mouse>
  {rule}
</labwc_config>
"""


def find_xwayland(display: Optional[str], hint: Optional[int] = None) -> Optional[int]:
    """pid of the running Xwayland serving `display` (':N'), or None. labwc starts Xwayland lazily and
    connecting to its socket would start it, so the X11 side is only consulted when it already runs."""
    if not display:
        return None
    want = [b"Xwayland", display.encode()]

    def check(pid) -> bool:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                return f.read().split(b"\0")[:2] == want
        except OSError:
            return False
    if hint and check(hint):
        return hint
    me = os.getuid()
    for ent in os.listdir("/proc"):
        if ent.isdigit() and check(ent):
            try:
                if os.stat(f"/proc/{ent}").st_uid == me:
                    return int(ent)
            except OSError:
                pass
    return None


class Placer:
    """Absolute move/resize of a Wayland window with labwc's MoveTo/ResizeTo actions."""

    ACK_KEY, GEO_KEY = "super+alt+F8", "super+alt+F9"

    def __init__(self, ws_dir: str, window_policy: str, compositor_pid: Callable[[], Optional[int]]):
        self.dir, self.policy, self.cpid = ws_dir, window_policy, compositor_pid
        self.lock = threading.Lock()
        self.gen = 0

    def _write(self, extra: str):
        rc = os.path.join(self.dir, "labwc", "rc.xml")
        tmp = rc + ".tmp"
        with open(tmp, "w") as f:
            f.write(render_rc(self.policy, extra))
        os.replace(tmp, rc)

    def place(self, session, window: int, frame_x=None, frame_y=None, width=None, height=None) -> None:
        cpid = self.cpid()
        if not cpid:
            raise WindowError("E_NOT_RUNNING", "workspace compositor is not running")
        acts = ""
        if frame_x is not None and frame_y is not None:
            acts += f'<action name="MoveTo" x="{int(frame_x)}" y="{int(frame_y)}"/>'
        if width is not None and height is not None:
            acts += f'<action name="ResizeTo" width="{int(width)}" height="{int(height)}"/>'
        if not acts:
            raise WindowError("E_BAD_ARGS", "need x and y, and/or width and height")
        with self.lock:
            self.gen += 1
            ack = os.path.join(self.dir, f"ack.{self.gen}")
            binds = (f'<keybind key="W-A-F8"><action name="Execute" command={quoteattr("/usr/bin/touch " + ack)}/>'
                     f'</keybind><keybind key="W-A-F9">{acts}</keybind>')
            self._write(binds)
            os.kill(cpid, signal.SIGHUP)             # exactly this workspace's compositor
            deadline = time.monotonic() + 4.0
            while not os.path.exists(ack):
                if time.monotonic() > deadline:
                    raise WindowError("E_TIMEOUT", "compositor did not apply the new configuration")
                session.key(self.ACK_KEY, hold_ms=10)
                time.sleep(0.12)
            try:
                os.unlink(ack)
            except OSError:
                pass
            session.window_action(window, "focus")
            time.sleep(0.05)
            session.key(self.GEO_KEY, hold_ms=10)
            time.sleep(0.15)
            # labwc's ResizeTo/MoveTo do not update the window's remembered ("natural") geometry: a later
            # MoveTo, unmaximize or reconfigure would bring back the old size. A maximize/unmaximize cycle
            # makes labwc store the current geometry as the natural one.
            session.window_action(window, "maximize")
            time.sleep(0.2)
            session.window_action(window, "unmaximize")
            time.sleep(0.2)
            with session.lock:       # the session's pump thread dispatches on the same connection: never without it
                session.conn.roundtrip()
            self._write("")          # leave a clean rc.xml for the next start (the running config is reloaded next time)


class WindowManager:
    """Per-workspace window service. Cheap to construct; holds the id registry."""

    def __init__(self, ws, session_fn: Callable[[], Any]):
        self.ws = ws
        self._session = session_fn
        self.lock = threading.RLock()
        self._next = 1
        self._ids: Dict[Any, int] = {}
        self._tracked: Dict[int, Dict[str, int]] = {}
        self._x: Optional[x11.X11Windows] = None
        self._xpid: Optional[int] = None
        self._cache: Optional[tuple] = None
        self.placer = Placer(ws.dir, ws.window_policy, lambda: ws.compositor_pid)

    # ---- ids
    def _id(self, key) -> int:
        with self.lock:
            i = self._ids.get(key)
            if i is None:
                i = self._next
                self._next += 1
                self._ids[key] = i
            return i

    # ---- X11 connection (workspace's own Xwayland, only if running)
    def _xconn(self) -> Optional[x11.X11Windows]:
        ws = self.ws
        self._xpid = find_xwayland(ws.x_display, self._xpid) if ws.xwayland else None
        if not ws.xwayland or not ws.x_display or not self._xpid:
            if self._x:
                self._x.close()
                self._x = None
            return None
        if self._x is None:
            try:
                self._x = x11.X11Windows.for_workspace(ws.x_display, ws.compositor_pid)
            except x11.X11Error:
                self._x = None
        return self._x

    def _x_call(self, fn: Callable[[x11.X11Windows], Any]):
        for attempt in (0, 1):
            xw = self._xconn()
            if xw is None:
                return None
            try:
                return fn(xw)
            except (x11.X11Error, OSError):
                try:
                    xw.close()
                finally:
                    self._x = None
                if attempt:
                    raise
        return None

    # ---- listing
    def _output_logical(self):
        ws = self.ws
        return int(round(ws.width / ws.scale)), int(round(ws.height / ws.scale))

    def list(self, max_age: float = 0.0) -> List[Dict[str, Any]]:
        with self.lock:
            if max_age and self._cache and time.monotonic() - self._cache[0] < max_age:
                return [dict(w) for w in self._cache[1]]
            wins = self._build()
            self._cache = (time.monotonic(), wins)
            return [dict(w) for w in wins]

    def _build(self) -> List[Dict[str, Any]]:
        ws = self.ws
        s = self._session()
        wl_wins = s.list_windows()
        xs = self._x_call(lambda xw: xw.list()) or []
        out: List[Dict[str, Any]] = []
        used = set()
        # Pair each X11 window with the Wayland toplevel labwc announced for it (labwc announces some X11
        # windows, e.g. xmessage and Chrome-on-X11, but not others, e.g. xeyes/xclock): same title and
        # app id == WM_CLASS instance (labwc's choice), in creation order. At most one toplevel is consumed
        # per X11 window, so a native Wayland window with the same title is hidden only if it also has
        # exactly the same app id.
        pairs = []
        for xw in xs:
            wl_match = None
            key = xw["instance"] or xw["class"]
            for w in wl_wins:
                if w["id"] not in used and w["title"] == xw["title"] and w["app_id"] == key:
                    used.add(w["id"])
                    wl_match = w
                    break
            pairs.append((xw, wl_match))
        lw, lh = self._output_logical()
        epoch = getattr(s, "epoch", 0)
        for xw, w in pairs:
            wid = self._id(("x11", xw["xid"]))
            focused = xw["focused"] or bool(w and w["focused"])
            out.append({
                "id": wid, "kind": "x11", "title": xw["title"], "app_id": xw["class"] or xw["instance"],
                "class": xw["class"], "instance": xw["instance"], "pid": xw["pid"],
                "app": self._app_of(xw["pid"]),
                "focused": focused, "maximized": xw["maximized"], "minimized": xw["minimized"],
                "fullscreen": xw["fullscreen"], "geometry": xw["geometry"], "geometry_source": "x11",
                "xid": xw["xid"],
            })
        for w in wl_wins:
            if w["id"] in used:
                continue
            wid = self._id(("wl", epoch, w["id"]))
            geo, src = self._wl_geometry(wid, w, lw, lh)
            out.append({
                "id": wid, "kind": "wayland", "title": w["title"], "app_id": w["app_id"],
                "class": w["app_id"], "instance": "", "pid": None, "app": None,
                "focused": w["focused"], "maximized": w["maximized"], "minimized": w["minimized"],
                "fullscreen": w["fullscreen"], "geometry": geo, "geometry_source": src,
                "toplevel": w["id"],
            })
        out.sort(key=lambda d: d["id"])
        # exactly one focused window (an announced X11 window can be both)
        seen = False
        for d in out:
            if d["focused"] and seen:
                d["focused"] = False
            seen = seen or d["focused"]
        return out

    def _app_of(self, pid: Optional[int]) -> Optional[int]:
        if not pid:
            return None
        for a in self.ws.apps.values():
            if a.get("pid") and x11.descends_from(pid, a["pid"]):
                return a["n"]
        return None

    def _wl_geometry(self, wid, w, lw, lh):
        if w["fullscreen"]:
            return {"x": 0, "y": 0, "width": lw, "height": lh}, "derived"
        if w["maximized"]:
            # the window fills the output; whether labwc adds a titlebar (server-side decoration) or the
            # client draws its own cannot be known for a Wayland window, so only the bounds are exact
            return {"x": 0, "y": 0, "width": lw, "height": lh}, "bounds"
        t = self._tracked.get(wid)
        if t:
            return {**NO_GEOMETRY, **t}, "tracked"
        return dict(NO_GEOMETRY), "unknown"

    # ---- management
    def _find(self, window: int) -> Dict[str, Any]:
        for w in self.list():
            if w["id"] == int(window):
                return w
        raise WindowError("E_NOT_FOUND", f"no such window {window}")

    ACTIONS = ("focus", "close", "maximize", "unmaximize", "restore", "minimize", "unminimize",
               "fullscreen", "unfullscreen")

    def action(self, window: int, action: str) -> Dict[str, Any]:
        if action not in self.ACTIONS:
            raise WindowError("E_BAD_ARGS", f"unknown window action {action!r}; one of {sorted(self.ACTIONS)}")
        w = self._find(window)
        if w["kind"] == "x11":
            xid = w["xid"]
            fn = {"focus": lambda x: x.focus(xid), "close": lambda x: x.close_window(xid),
                  "maximize": lambda x: x.maximize(xid, True),
                  "unmaximize": lambda x: x.maximize(xid, False), "restore": lambda x: x.maximize(xid, False),
                  "minimize": lambda x: x.minimize(xid), "unminimize": lambda x: x.unminimize(xid),
                  "fullscreen": lambda x: x.fullscreen(xid, True),
                  "unfullscreen": lambda x: x.fullscreen(xid, False)}[action]
            self._x_call(fn)
        else:
            self._session().window_action(w["toplevel"], action)
        self._after_state_change(window, action)
        time.sleep(0.12)
        return self._find(window) if action != "close" else {"id": int(window), "closing": True}

    def _after_state_change(self, window: int, action: str):
        # labwc keeps the placed ("natural") geometry while a window is maximized/fullscreen and restores it,
        # so a tracked placement stays valid; it is only reported while the window is not maximized/fullscreen
        pass

    def set_geometry(self, window: int, x=None, y=None, width=None, height=None) -> Dict[str, Any]:
        for k, v in (("x", x), ("y", y), ("width", width), ("height", height)):
            if v is not None and not isinstance(v, (int, float)):
                raise WindowError("E_BAD_ARGS", f"{k} must be a number")
        if (x is None) != (y is None) or (width is None) != (height is None) or (
                x is None and width is None):
            raise WindowError("E_BAD_ARGS", "give x and y, and/or width and height")
        if width is not None and (width < 20 or height < 20):
            raise WindowError("E_BAD_ARGS", "width and height must be at least 20")
        w = self._find(window)
        if w["maximized"] or w["fullscreen"]:
            self.action(window, "unfullscreen" if w["fullscreen"] else "unmaximize")
            time.sleep(0.15)
            w = self._find(window)
        if w["kind"] == "x11":
            def do(xw):
                xw.configure(w["xid"], x, y, width, height)
                # labwc applies ConfigureWindow without updating the window's remembered ("natural")
                # geometry, so a later reconfigure/unmaximize would snap it back. A maximize/unmaximize
                # cycle makes labwc store the new geometry as the natural one.
                time.sleep(0.12)
                xw.maximize(w["xid"], True)
                time.sleep(0.15)
                xw.maximize(w["xid"], False)
            self._x_call(do)
            time.sleep(0.15)
        else:
            self.placer.place(self._session(), w["toplevel"],
                              None if x is None else x - BORDER, None if y is None else y - BORDER - TITLE_H,
                              width, height)
            t = self._tracked.setdefault(int(window), {})
            for k, v in (("x", x), ("y", y), ("width", width), ("height", height)):
                if v is not None:
                    t[k] = int(v)
        time.sleep(0.1)
        return self._find(window)

    def close(self):
        if self._x:
            self._x.close()
            self._x = None
