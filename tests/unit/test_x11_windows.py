"""Unit tests (no compositor): the X11 client against a fake X server, and the merged window list."""
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from aw import windows, x11  # noqa: E402


class FakeXServer(threading.Thread):
    """Just enough X11 to serve XConnection + X11Windows: handshake, InternAtom, GetProperty, GetGeometry,
    TranslateCoordinates, ConfigureWindow, SendEvent, GetInputFocus. Records ConfigureWindow/SendEvent."""

    ROOT = 0x100

    def __init__(self, path, windows_):
        super().__init__(daemon=True)
        self.path = path
        self.win = windows_       # xid -> dict(title,class,instance,pid,state[list],geom=(x,y,w,h),active)
        self.atoms = {}
        self.configured, self.events = [], []
        self.active = 0
        self.srv = socket.socket(socket.AF_UNIX)
        self.srv.bind(path)
        self.srv.listen(4)

    def atom(self, name):
        return self.atoms.setdefault(name, 100 + len(self.atoms))

    def name_of(self, a):
        return next(k for k, v in self.atoms.items() if v == a)

    def run(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self.serve, args=(c,), daemon=True).start()

    @staticmethod
    def rd(c, n):
        b = b""
        while len(b) < n:
            d = c.recv(n - len(b))
            if not d:
                raise EOFError
            b += d
        return b

    def serve(self, c):
        try:
            self.rd(c, 12)
            vendor = b"fake"
            fixed = struct.pack("<IIIIHHBBBBBBBB", 1, 0x200000, 0x1fffff, 256, len(vendor), 65535, 1, 0,
                                0, 0, 0, 0, 8, 255) + b"\0" * 4
            screen = struct.pack("<IIIII", self.ROOT, 0, 0, 0, 0) + struct.pack("<HH", 1280, 720) + b"\0" * 12
            data = fixed + vendor + screen
            data += b"\0" * ((4 - len(data) % 4) % 4)
            c.sendall(struct.pack("<BBHHH", 1, 0, 11, 0, len(data) // 4) + data)
            seq = 0
            while True:
                h = self.rd(c, 4)
                op, d1, ln = h[0], h[1], struct.unpack("<H", h[2:4])[0]
                body = self.rd(c, ln * 4 - 4) if ln > 1 else b""
                seq = (seq + 1) & 0xFFFF
                rep = None
                if op == 16:                                            # InternAtom
                    n = struct.unpack_from("<H", body, 0)[0]
                    rep = (0, struct.pack("<I", self.atom(body[4:4 + n].decode())) + b"\0" * 20)
                elif op == 20:                                          # GetProperty
                    win, prop = struct.unpack_from("<II", body, 0)
                    rep = self.get_property(win, self.name_of(prop))
                elif op == 14:                                          # GetGeometry
                    win = struct.unpack_from("<I", body, 0)[0]
                    g = self.win[win]["geom"]
                    rep = (24, struct.pack("<IhhHHH", self.ROOT, g[0], g[1], g[2], g[3], 0) + b"\0" * 10)
                elif op == 40:                                          # TranslateCoordinates
                    win = struct.unpack_from("<I", body, 0)[0]
                    g = self.win[win]["geom"]
                    rep = (1, struct.pack("<IhhH", 0, g[0], g[1], 0) + b"\0" * 14)
                elif op == 12:                                          # ConfigureWindow
                    win, mask = struct.unpack_from("<IH", body, 0)
                    vals = struct.unpack_from(f"<{bin(mask).count('1')}i", body, 8)
                    self.configured.append((win, mask, vals))
                    g = list(self.win[win]["geom"])
                    i = 0
                    for bit, idx in ((1, 0), (2, 1), (4, 2), (8, 3)):
                        if mask & bit:
                            g[idx] = vals[i]
                            i += 1
                    self.win[win]["geom"] = tuple(g)
                elif op == 25:                                          # SendEvent
                    self.events.append(body)
                elif op == 17:                                          # GetAtomName
                    a = struct.unpack_from("<I", body, 0)[0]
                    nm = self.name_of(a).encode()
                    rep = (0, struct.pack("<H", len(nm)) + b"\0" * 22 + nm)
                elif op == 43:                                          # GetInputFocus
                    rep = (1, b"\0" * 24)
                if rep is not None:
                    fmt, payload = rep
                    fixed_part, extra = payload[:24], payload[24:]
                    extra += b"\0" * (-len(extra) % 4)
                    c.sendall(struct.pack("<BBHI", 1, fmt, seq, len(extra) // 4) + fixed_part + extra)
        except (EOFError, OSError):
            pass
        finally:
            c.close()

    def get_property(self, win, name):
        def cards(vals):
            raw = struct.pack(f"<{len(vals)}I", *vals)
            return (32, struct.pack("<III", 6, 0, len(vals)) + b"\0" * 12 + raw)

        def text(s):
            raw = s.encode()
            return (8, struct.pack("<III", 31, 0, len(raw)) + b"\0" * 12 + raw)
        none = (0, struct.pack("<III", 0, 0, 0) + b"\0" * 12)
        if win == self.ROOT:
            if name == "_NET_CLIENT_LIST":
                return cards(list(self.win))
            if name == "_NET_ACTIVE_WINDOW":
                return cards([self.active])
            return none
        w = self.win[win]
        if name in ("_NET_WM_NAME", "WM_NAME"):
            return text(w["title"])
        if name == "WM_CLASS":
            return text(w["instance"] + "\0" + w["class"] + "\0")
        if name == "_NET_WM_PID":
            return cards([w["pid"]]) if w.get("pid") else none
        if name == "_NET_WM_STATE":
            return cards([self.atom(s) for s in w["state"]])
        if name == "WM_PROTOCOLS" and w.get("protocols"):
            return cards([self.atom(a) for a in w["protocols"]])
        return none


class X11ClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="awx11-")
        self._old = x11.X_SOCKET_DIR
        x11.X_SOCKET_DIR = self.tmp
        self.srv = FakeXServer(os.path.join(self.tmp, "X9"), {
            0x400001: dict(title="Calculator", **{"class": "Calc", "instance": "calc"}, pid=4242,
                           state=["_NET_WM_STATE_MAXIMIZED_VERT", "_NET_WM_STATE_MAXIMIZED_HORZ"],
                           geom=(0, 26, 1280, 694)),
            0x400002: dict(title="Eyes", **{"class": "XEyes", "instance": "xeyes"}, pid=None, state=[],
                           geom=(200, 150, 500, 300), protocols=["WM_DELETE_WINDOW"]),
        })
        self.srv.active = 0x400001
        self.srv.start()

    def tearDown(self):
        x11.X_SOCKET_DIR = self._old
        self.srv.srv.close()

    def test_socket_path_accepts_only_local_displays(self):
        self.assertTrue(x11.socket_path(":2").endswith("/X2"))
        for bad in ("host:0", "unix:/tmp/x", "/tmp/.X11-unix/X0", "", ":x", "@/tmp/.X11-unix/X0"):
            with self.assertRaises(x11.X11Error):
                x11.socket_path(bad)

    def test_list_reads_exact_geometry_class_state(self):
        xw = x11.X11Windows(x11.XConnection(":9", expect_ancestor_pid=os.getpid()))
        wins = {w["xid"]: w for w in xw.list()}
        calc, eyes = wins[0x400001], wins[0x400002]
        self.assertEqual((calc["title"], calc["class"], calc["instance"], calc["pid"]), ("Calculator", "Calc", "calc", 4242))
        self.assertTrue(calc["maximized"] and calc["focused"] and not calc["minimized"])
        self.assertEqual(calc["geometry"], {"x": 0, "y": 26, "width": 1280, "height": 694})
        self.assertEqual(eyes["geometry"], {"x": 200, "y": 150, "width": 500, "height": 300})
        self.assertFalse(eyes["maximized"])
        xw.close()

    def test_configure_and_state_requests_are_sent_to_the_server(self):
        xw = x11.X11Windows(x11.XConnection(":9", expect_ancestor_pid=os.getpid()))
        xw.configure(0x400002, x=700, y=100, width=400, height=250)
        self.assertEqual(self.srv.configured[-1][0], 0x400002)
        self.assertEqual(self.srv.configured[-1][2], (700, 100, 400, 250))
        got = [w for w in xw.list() if w["xid"] == 0x400002][0]["geometry"]
        self.assertEqual(got, {"x": 700, "y": 100, "width": 400, "height": 250})
        xw.maximize(0x400002, True)
        xw.close_window(0x400002)
        self.assertEqual(len(self.srv.events), 2)
        # close: the window advertises WM_DELETE_WINDOW, so the message goes to the window itself (ICCCM),
        # not to the root window
        close = self.srv.events[1]
        self.assertEqual(struct.unpack_from("<II", close, 0), (0x400002, 0))
        self.assertEqual(self.srv.name_of(struct.unpack_from("<I", close, 8 + 8)[0]), "WM_PROTOCOLS")
        self.assertEqual(self.srv.name_of(struct.unpack_from("<I", close, 8 + 12)[0]), "WM_DELETE_WINDOW")
        ev = self.srv.events[0]                                   # destination, mask, 32-byte ClientMessage
        self.assertEqual(struct.unpack_from("<I", ev, 0)[0], FakeXServer.ROOT)
        self.assertEqual(ev[8], 33)                               # ClientMessage
        self.assertEqual(self.srv.name_of(struct.unpack_from("<I", ev, 8 + 8)[0]), "_NET_WM_STATE")
        xw.close()

    def test_server_not_owned_by_the_workspace_is_refused(self):
        other = subprocess.Popen(["sleep", "30"])
        try:
            with self.assertRaises(x11.X11Error) as cm:
                x11.XConnection(":9", expect_ancestor_pid=other.pid)
            self.assertIn("refusing", str(cm.exception))
        finally:
            other.kill()
            other.wait()


class FakeWsession:
    def __init__(self, wins):
        self.wins, self.epoch = wins, 1

    def list_windows(self):
        return [dict(w) for w in self.wins]


class FakeWs:
    id, dir, window_policy = "wtest", "/nonexistent", "maximized"
    xwayland, x_display, compositor_pid = True, ":9", 1
    width, height, scale = 1280, 720, 1.0
    apps = {"1": {"n": 1, "pid": 4242}}


class MergedListTests(unittest.TestCase):
    def mk(self, wl, xs):
        wm = windows.WindowManager(FakeWs(), lambda: FakeWsession(wl))
        wm._x_call = lambda fn: xs
        return wm

    @staticmethod
    def xwin(**kw):
        base = dict(xid=1, title="t", **{"class": "C"}, instance="i", pid=None, focused=False, maximized=False,
                    minimized=False, fullscreen=False, geometry={"x": 0, "y": 0, "width": 10, "height": 10})
        base.update(kw)
        return base

    @staticmethod
    def wl(i, title, app_id, **kw):
        base = dict(id=i, title=title, app_id=app_id, focused=False, maximized=False, minimized=False,
                    fullscreen=False)
        base.update(kw)
        return base

    def test_announced_x11_window_is_listed_once_as_x11(self):
        wm = self.mk([self.wl(1, "xmessage", "xmessage", focused=True)],
                     [self.xwin(xid=9, title="xmessage", **{"class": "Xmessage"}, instance="xmessage", pid=4242)])
        out = wm.list()
        self.assertEqual([(w["kind"], w["title"]) for w in out], [("x11", "xmessage")])
        self.assertTrue(out[0]["focused"])
        self.assertEqual(out[0]["app"], 1)
        self.assertEqual(out[0]["geometry_source"], "x11")

    def test_unannounced_x11_and_native_wayland_both_listed_with_stable_distinct_ids(self):
        wm = self.mk([self.wl(1, "Native", "org.gnome.Calculator")],
                     [self.xwin(xid=9, title="xeyes", instance="xeyes", **{"class": "XEyes"})])
        a = {w["title"]: w["id"] for w in wm.list()}
        b = {w["title"]: w["id"] for w in wm.list()}
        self.assertEqual(a, b)
        self.assertEqual(len(set(a.values())), 2)
        self.assertEqual({w["kind"] for w in wm.list()}, {"x11", "wayland"})

    def test_native_window_with_same_title_but_other_app_id_is_not_hidden(self):
        # Chrome on Wayland and Chrome on X11 share a title; app ids differ ("google-chrome" vs instance)
        wm = self.mk([self.wl(1, "Page", "google-chrome (/p)"), self.wl(2, "Page", "google-chrome")],
                     [self.xwin(xid=9, title="Page", instance="google-chrome (/p)", **{"class": "Google-chrome"})])
        kinds = sorted((w["kind"], w["app_id"]) for w in wm.list())
        self.assertEqual(kinds, [("wayland", "google-chrome"), ("x11", "Google-chrome")])

    def test_wayland_geometry_is_derived_tracked_or_unknown_never_invented(self):
        wm = self.mk([self.wl(1, "Max", "a", maximized=True), self.wl(2, "Full", "b", fullscreen=True),
                      self.wl(3, "Free", "c")], [])
        g = {w["title"]: (w["geometry"], w["geometry_source"]) for w in wm.list()}
        self.assertEqual(g["Full"], ({"x": 0, "y": 0, "width": 1280, "height": 720}, "derived"))
        self.assertEqual(g["Max"][1], "bounds")
        self.assertEqual(g["Free"][1], "unknown")
        self.assertIsNone(g["Free"][0]["width"])
        free_id = [w["id"] for w in wm.list() if w["title"] == "Free"][0]
        wm._tracked[free_id] = {"x": 10, "y": 20, "width": 300, "height": 200}
        again = [w for w in wm.list() if w["id"] == free_id][0]
        self.assertEqual((again["geometry_source"], again["geometry"]["width"]), ("tracked", 300))

    def test_only_one_window_is_reported_focused(self):
        wm = self.mk([self.wl(1, "A", "a", focused=True)], [self.xwin(xid=9, title="B", focused=True)])
        self.assertEqual(sum(w["focused"] for w in wm.list()), 1)

    def test_set_geometry_validation(self):
        wm = self.mk([self.wl(1, "A", "a")], [])
        wid = wm.list()[0]["id"]
        for kw in ({"x": 1}, {"width": 100}, {}, {"x": 1, "y": 2, "width": 5, "height": 5}):
            with self.assertRaises(windows.WindowError):
                wm.set_geometry(wid, **kw)
        with self.assertRaises(windows.WindowError) as cm:
            wm.set_geometry(999, x=1, y=1)
        self.assertEqual(cm.exception.code, "E_NOT_FOUND")

    def test_rc_xml_keeps_policy_and_adds_keybinds(self):
        rc = windows.render_rc("maximized", '<keybind key="W-A-F9"><action name="MoveTo" x="1" y="2"/></keybind>')
        self.assertIn('<action name="Maximize"/>', rc)
        self.assertIn("MoveTo", rc)
        self.assertNotIn('<action name="Maximize"/>', windows.render_rc("floating"))


if __name__ == "__main__":
    unittest.main()
