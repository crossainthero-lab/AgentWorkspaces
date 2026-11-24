"""REAL INTEGRATION TESTS: application compatibility matrix (docs/compatibility.md).

Each test starts a real application inside a real workspace compositor, waits for its window, takes a
screenshot, sends real pointer/keyboard input through the workspace's virtual devices and checks the effect
the application itself reports (event log, stdout, local web server) -- not a mock. Results (including
startup time, screenshot latency and workspace memory) are collected in RESULTS and written as JSON to
$AW_COMPAT_REPORT when that variable is set.

Applications that are not installed are skipped with the reason. Optional binaries:
  AW_TEST_FIREFOX=/path/to/firefox          non-snap Firefox (the Ubuntu snap is refused by design)
  AW_TEST_ELECTRON_BIN=/path/to/electron    stock Electron from npm (the test app is tests/apps/electron_probe)
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as K  # noqa: E402
from aw.client import ApiError  # noqa: E402
from apps.web_probe import WebProbe  # noqa: E402

C = None
WEB = None
FAKE_HOME = None
RESULTS = []
FF = os.environ.get("AW_TEST_FIREFOX")
EL = os.environ.get("AW_TEST_ELECTRON_BIN")
ROOT = K.ROOT
GTK4 = os.path.join(ROOT, "tests", "apps", "gtk_probe.py")
GTK3 = os.path.join(ROOT, "tests", "apps", "gtk3_probe.py")
ELECTRON_APP = os.path.join(ROOT, "tests", "apps", "electron_probe")


def setUpModule():
    global C, WEB, FAKE_HOME
    if not K.have("labwc"):
        raise unittest.SkipTest("labwc not installed")
    FAKE_HOME = tempfile.mkdtemp(prefix="awt-fakehome-", dir=f"/run/user/{os.getuid()}")
    os.chmod(FAKE_HOME, 0o700)
    extra = {}
    if FF and os.access(FF, os.X_OK):
        extra["AW_BROWSER_EXTRA"] = json.dumps([{"id": "firefox-tarball", "label": "Firefox", "family": "firefox",
                                                "exe": FF}])
    C = K.start_daemon("cmp", home=FAKE_HOME, extra_env=extra)
    WEB = WebProbe(os.path.join(K.BASE, "web.log")).start()


def tearDownModule():
    path = os.environ.get("AW_COMPAT_REPORT")
    if path:
        with open(path, "w") as f:
            json.dump(RESULTS, f, indent=1)
    K.stop_daemon(C)
    if WEB:
        WEB.stop()
    shutil.rmtree(FAKE_HOME, ignore_errors=True)


def shot(ws, **kw):
    r = C.call("screen.screenshot", workspace=ws, inline=True, **kw)
    return base64.b64decode(r["base64"]), r


def find_box(png, rgb, tol=6, step=2):
    """Bounding box (x0, y0, x1, y1) of pixels within `tol` of rgb, or None. Every column is examined (x is
    exact); rows are sampled every `step` pixels (y is exact to `step`)."""
    w, h, rows = K.decode_png(png)
    x0 = y0 = 10 ** 9
    x1 = y1 = -1
    for y in range(0, h, step):
        row = rows[y]
        for x in range(0, w):
            p = row[x * 3:x * 3 + 3]
            if abs(p[0] - rgb[0]) <= tol and abs(p[1] - rgb[1]) <= tol and abs(p[2] - rgb[2]) <= tol:
                x0, y0, x1, y1 = min(x0, x), min(y0, y), max(x1, x), max(y1, y)
    return None if x1 < 0 else (x0, y0, x1, y1)


def distinct_colours(png, n=400):
    w, h, rows = K.decode_png(png)
    seen = set()
    for y in range(0, h, max(1, h // 40)):
        for x in range(0, w, max(1, w // 40)):
            seen.add(rows[y][x * 3:x * 3 + 3])
    return len(seen)


def first_window(ws, timeout=30, pred=None):
    t0 = time.monotonic()
    w = K.wait_for(lambda: [x for x in C.call("window.list", workspace=ws)["windows"]
                            if (pred is None or pred(x))], timeout)
    return (w[0] if w else None), time.monotonic() - t0


def usage_mib(ws):
    return round(C.call("workspace.get", workspace=ws)["usage"].get("memory_bytes", 0) / 2 ** 20, 1)


def gtk_version(api):
    """GTK version string, asked in a subprocess (one process cannot load both GTK 3 and 4)."""
    code = (f"import gi; gi.require_version('Gtk','{api}'); from gi.repository import Gtk; "
            "print(f'GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()} (PyGObject probe)')")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env={"PATH": os.environ["PATH"]})
    return r.stdout.strip()


def record(**kw):
    RESULTS.append(kw)


class Matrix(unittest.TestCase):
    def mk(self, name, w=1280, h=800, **kw):
        C.call("workspace.create", name=name, width=w, height=h, **kw)
        self.addCleanup(lambda: C.call("workspace.destroy", workspace=name))
        return name

    def measure(self, ws, family, app, version, backend, t_first, extra=None):
        time.sleep(1.0)
        png, r = shot(ws)
        self.assertGreaterEqual(distinct_colours(png), 3, "screenshot is blank")
        lat = []
        for _ in range(5):
            lat.append(C.call("screen.screenshot", workspace=ws, inline=False)["capture_ms"])
        d = dict(family=family, app=app, version=version, backend=backend, result="pass",
                 startup_s=round(t_first, 2), screenshot_ms=round(sorted(lat)[len(lat) // 2], 1),
                 workspace_mib=usage_mib(ws))
        d.update(extra or {})
        record(**d)
        return png

    # ---------------------------------------------------------------- GTK
    def _gtk_probe(self, family, script, version, env_prefix=(), backend="wayland"):
        ws = self.mk(f"cmp-{family}-{backend}")
        log = os.path.join(K.BASE, f"{family}-{backend}.log")
        open(log, "w").close()
        t0 = time.monotonic()
        C.call("workspace.launch", workspace=ws, argv=[*env_prefix, sys.executable, script, family.upper(), log,
                                                        "#2b6cb0"])
        self.assertTrue(K.wait_for(lambda: any(e.get("ev") == "ready" for e in K.log_events(log)), 30))
        win, t_first = first_window(ws)
        self.assertIsNotNone(win)
        self.assertEqual(win["kind"], "x11" if backend == "x11" else "wayland")
        t_first = time.monotonic() - t0
        # place it where we know the content rectangle and verify against the screenshot
        win = C.call("window.set_geometry", workspace=ws, window=win["id"], x=300, y=200, width=640, height=420)["window"]
        time.sleep(0.6)
        png = self.measure(ws, f"GTK{3 if 'gtk3' in script else 4}", os.path.basename(script), version, backend, t_first)
        box = find_box(png, (0x2b, 0x6c, 0xb0))
        self.assertIsNotNone(box, "banner colour not found")
        self.assertEqual(box[0], 300, box)                      # reported x == pixels
        self.assertAlmostEqual(box[2] - box[0] + 1, 640, delta=3)
        self.assertAlmostEqual(box[1], 200, delta=2)            # content top == reported y
        # pointer: click inside the window, the app reports window-relative coordinates
        n0 = len(K.log_events(log))
        C.call("input.click", workspace=ws, x=700, y=560)       # empty area below the widgets
        got = K.wait_for(lambda: [e for e in K.log_events(log)[n0:] if e["ev"] in ("press", "button", "motion")], 5)
        self.assertTrue(got, "app saw no pointer event")
        # keyboard: type into the focused entry, including characters outside the US layout
        C.call("input.type", workspace=ws, text="Grüße ab")
        self.assertTrue(K.wait_for(lambda: any(e.get("ev") == "text" and e["text"] == "Grüße ab"
                                               for e in K.log_events(log)), 8), K.log_events(log)[-3:])
        return ws, log, win

    def test_gtk4_wayland(self):
        ws, log, win = self._gtk_probe("gtk4", GTK4, gtk_version("4.0"))
        # reported geometry is "tracked" for a Wayland window we placed
        self.assertEqual(win["geometry_source"], "tracked")

    def test_gtk3_wayland(self):
        ws, log, win = self._gtk_probe("gtk3", GTK3, gtk_version("3.0"))
        self.assertTrue(any(e.get("gdk_backend") == "GdkWaylandDisplay" for e in K.log_events(log)))

    def test_gtk3_on_xwayland(self):
        ws, log, win = self._gtk_probe("gtk3", GTK3, gtk_version("3.0"), env_prefix=("/usr/bin/env", "GDK_BACKEND=x11"), backend="x11")
        self.assertEqual(win["geometry_source"], "x11")
        self.assertTrue(any(e.get("gdk_backend") == "GdkX11Display" for e in K.log_events(log)))

    @unittest.skipUnless(K.have("gnome-calculator"), "gnome-calculator not installed")
    def test_gtk4_libadwaita_calculator(self):
        ws = self.mk("cmp-calc")
        t0 = time.monotonic()
        C.call("workspace.launch", workspace=ws, argv=["gnome-calculator"])
        win, _ = first_window(ws, 30, lambda w: "Calculator" in w["title"])
        self.assertIsNotNone(win)
        t_first = time.monotonic() - t0
        time.sleep(1.5)
        C.call("input.type", workspace=ws, text="6*7")
        C.call("input.key", workspace=ws, combo="Return")
        time.sleep(0.8)
        # the app answers through the clipboard: Ctrl+C copies the displayed result
        C.call("input.key", workspace=ws, combo="ctrl+a")
        C.call("input.key", workspace=ws, combo="ctrl+c")
        time.sleep(0.5)
        self.assertEqual(C.call("clipboard.get", workspace=ws)["text"].strip(), "42")
        r_ = subprocess.run(["gnome-calculator", "--version"], capture_output=True, text=True,
                            env={"PATH": os.environ["PATH"]})
        out = (r_.stdout or r_.stderr).strip()
        self.measure(ws, "GTK4 + libadwaita", "gnome-calculator", out, "wayland", t_first)

    # ---------------------------------------------------------------- Qt
    @unittest.skipUnless(K.have("kdialog"), "kdialog (Qt6/KDE) not installed")
    def test_qt6_wayland_kdialog(self):
        ws = self.mk("cmp-qt6")
        t0 = time.monotonic()
        r = C.call("workspace.launch", workspace=ws, argv=["kdialog", "--title", "QtProbe", "--inputbox", "Qt6", ""])
        win, _ = first_window(ws, 30)
        self.assertIsNotNone(win)
        self.assertEqual(win["kind"], "wayland")                 # native Qt Wayland plugin
        t_first = time.monotonic() - t0
        self.measure(ws, "Qt6 (KDE Frameworks 6)", "kdialog", "KDE 25.12", "wayland", t_first)
        C.call("input.type", workspace=ws, text="qt typed ünï")
        C.call("input.key", workspace=ws, combo="Return")
        self.assertTrue(K.wait_for(lambda: "qt typed ünï" in open(r["log"]).read(), 8), open(r["log"]).read())

    @unittest.skipUnless(K.have("vlc"), "vlc (Qt5) not installed")
    def test_qt5_on_xwayland_vlc(self):
        ws = self.mk("cmp-qt5")
        t0 = time.monotonic()
        C.call("workspace.launch", workspace=ws, argv=["vlc", "--no-video-title-show", "--intf", "qt"])
        win, _ = first_window(ws, 40, lambda w: "Privacy" in w["title"])
        self.assertIsNotNone(win, C.call("window.list", workspace=ws))
        self.assertEqual(win["kind"], "x11")                     # no Qt5 Wayland plugin installed: xcb via XWayland
        t_first = time.monotonic() - t0
        self.measure(ws, "Qt5", "vlc", "3.0.23", "xwayland", t_first)
        # the "Continue" button of the first-run dialog (maximized in a 1280x800 workspace): pointer input
        # through XWayland
        C.call("input.click", workspace=ws, x=1228, y=603)
        self.assertTrue(K.wait_for(lambda: not [w for w in C.call("window.list", workspace=ws)["windows"]
                                                if "Privacy" in w["title"]], 8), "dialog did not close")

    # ---------------------------------------------------------------- XWayland
    @unittest.skipUnless(K.have("xmessage"), "xmessage not installed")
    def test_xwayland_xmessage(self):
        ws = self.mk("cmp-x11")
        t0 = time.monotonic()
        r = C.call("workspace.launch", workspace=ws, argv=["xmessage", "-print", "-buttons", "yes:10", "pick"])
        win, _ = first_window(ws, 20)
        self.assertIsNotNone(win)
        self.assertEqual(win["kind"], "x11")
        t_first = time.monotonic() - t0
        self.measure(ws, "X11 (Athena)", "xmessage", "-", "xwayland", t_first)
        # the only button sits at the bottom left of the (maximized) window: a real click through XWayland
        g = win["geometry"]
        C.call("input.click", workspace=ws, x=g["x"] + 20, y=g["y"] + g["height"] - 14)
        self.assertTrue(K.wait_for(lambda: "yes" in open(r["log"]).read(), 6), open(r["log"]).read())

    @unittest.skipUnless(K.have("xeyes"), "xeyes not installed")
    def test_xwayland_window_labwc_does_not_announce(self):
        ws = self.mk("cmp-xeyes")
        C.call("workspace.launch", workspace=ws, argv=["xeyes"])
        win, _ = first_window(ws, 20)
        self.assertIsNotNone(win, "xeyes must be listed (via the workspace X server)")
        self.assertEqual((win["kind"], win["class"], win["geometry_source"]), ("x11", "XEyes", "x11"))
        self.assertGreater(win["geometry"]["width"], 0)

    # ---------------------------------------------------------------- browsers / Electron
    @unittest.skipUnless(K.have("google-chrome"), "google-chrome not installed")
    def test_chromium_wayland_and_x11(self):
        ver = subprocess.run(["google-chrome", "--version"], capture_output=True, text=True,
                             env={"PATH": os.environ["PATH"]}).stdout.strip()
        for backend in ("wayland", "x11"):
            ws = self.mk(f"cmp-chr-{backend}", 1200, 760)
            n0 = len([e for e in WEB.events() if e.get("ev") == "loaded"])
            t0 = time.monotonic()
            C.call("browser.launch", workspace=ws, browser="google-chrome", url=WEB.url + "/?b=CHR-" + backend,
                   backend=backend)
            self.assertTrue(K.wait_for(lambda: len([e for e in WEB.events() if e.get("ev") == "loaded"]) > n0, 30))
            t_first = time.monotonic() - t0
            win, _ = first_window(ws, 10)
            self.assertEqual(win["kind"], "x11" if backend == "x11" else "wayland")
            self.measure(ws, "Chromium-based", "Google Chrome", ver, backend, t_first)
            before = len(WEB.events())
            # focus the page's input (autofocus) by clicking it, type, press the button
            C.call("input.click", workspace=ws, x=200, y=224 + (27 if backend == "x11" else 0))
            C.call("input.type", workspace=ws, text="agent says hi")
            self.assertTrue(K.wait_for(lambda: any(e.get("v") == "agent says hi" for e in WEB.events()[before:]), 8),
                            WEB.events()[before:][-3:])

    @unittest.skipUnless(FF and os.access(FF or "", os.X_OK), "set AW_TEST_FIREFOX to a non-snap Firefox")
    def test_firefox_wayland(self):
        ws = self.mk("cmp-ff", 1280, 800)
        n0 = len([e for e in WEB.events() if e.get("ev") == "loaded"])
        t0 = time.monotonic()
        C.call("browser.launch", workspace=ws, browser="firefox-tarball", url=WEB.url + "/?b=FIREFOX")
        win, _ = first_window(ws, 40)
        self.assertIsNotNone(win)
        self.assertEqual(win["kind"], "wayland")
        self.assertTrue(K.wait_for(lambda: len([e for e in WEB.events() if e.get("ev") == "loaded"]) > n0, 40))
        t_first = time.monotonic() - t0
        time.sleep(2)
        out = subprocess.run([FF, "--version"], capture_output=True, text=True, env={"PATH": os.environ["PATH"]}).stdout.strip()
        self.measure(ws, "Firefox", "Firefox (vendor tarball)", out, "wayland", t_first)
        # Firefox 160 asks for the terms of use on first run: a person clicks "Continue"
        C.call("input.click", workspace=ws, x=640, y=326)
        time.sleep(1.5)
        before = len(WEB.events())
        C.call("input.click", workspace=ws, x=180, y=240)
        C.call("input.type", workspace=ws, text="ff typed")
        self.assertTrue(K.wait_for(lambda: any(e.get("v") == "ff typed" for e in WEB.events()[before:]), 8),
                        WEB.events()[before:][-3:])

    @unittest.skipUnless(EL and os.access(EL or "", os.X_OK), "set AW_TEST_ELECTRON_BIN to a stock Electron")
    def test_electron_wayland(self):
        ws = self.mk("cmp-el", 1100, 700)
        log = os.path.join(K.BASE, "el.log")
        open(log, "w").close()
        t0 = time.monotonic()
        C.call("workspace.launch", workspace=ws, argv=[EL, "--no-sandbox", "--ozone-platform=wayland",
                                                       ELECTRON_APP, log])
        self.assertTrue(K.wait_for(lambda: any(e.get("ev") == "ready" for e in K.log_events(log)), 30))
        win, _ = first_window(ws, 20)
        self.assertEqual(win["kind"], "wayland")
        t_first = time.monotonic() - t0
        ver = [e for e in K.log_events(log) if e.get("ev") == "ready"][0].get("electron")
        self.measure(ws, "Electron", "stock Electron + probe app", ver, "wayland", t_first)
        C.call("input.click", workspace=ws, x=180, y=234)
        C.call("input.type", workspace=ws, text="electron typed")
        self.assertTrue(K.wait_for(lambda: any(e.get("ev") == "text electron typed" for e in K.log_events(log)), 8))

    # ---------------------------------------------------------------- scaling / cursor
    def test_scale_2_pointer_coordinates_are_logical(self):
        ws = self.mk("cmp-hidpi", 1000, 640, scale=2.0)
        log = os.path.join(K.BASE, "hidpi.log")
        open(log, "w").close()
        C.call("workspace.launch", workspace=ws, argv=[sys.executable, GTK4, "HIDPI", log, "#2b6cb0"])
        self.assertTrue(K.wait_for(lambda: any(e.get("ev") == "ready" for e in K.log_events(log)), 30))
        win, _ = first_window(ws, 10)
        time.sleep(1.0)
        info = C.call("screen.info", workspace=ws)["outputs"][0]
        self.assertEqual(info["scale"], 2.0)
        png, r = shot(ws)
        self.assertEqual((r["width"], r["height"]), (1000, 640))        # configured size is physical pixels
        g = win["geometry"]
        C.call("input.click", workspace=ws, x=250, y=300)               # logical coordinates (output is 500x320)
        got = K.wait_for(lambda: [e for e in K.log_events(log) if e["ev"] == "motion"], 5)
        self.assertTrue(got)


if __name__ == "__main__":
    unittest.main()
