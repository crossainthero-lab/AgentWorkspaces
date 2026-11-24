"""REAL INTEGRATION TESTS: window enumeration/management (Wayland + private XWayland) and per-workspace audio.

Windows: real Wayland and X11 windows in a real labwc; every geometry claim is checked against the pixels of a
screenshot. Audio: a real private PipeWire graph per workspace, real tones played by real clients (pw-cat for
the native protocol, ffmpeg for the Pulse protocol), captured from the workspace sink. The host's PipeWire is
only READ (pw-dump) before and after to prove nothing reached it; routing to "the host" is tested against a
stand-in PipeWire instance, never the real speakers.
"""
import json
import math
import os
import pty
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as K  # noqa: E402
from aw import audio as audiomod  # noqa: E402
from aw.client import ApiError, Client  # noqa: E402
from test_compat_real import find_box  # noqa: E402  (pixel helper)

C = None
STANDIN = None
STANDIN_PROCS = []
STANDIN_ENV = None
TMP = None
GTK4 = os.path.join(K.ROOT, "tests", "apps", "gtk_probe.py")
GTK3 = os.path.join(K.ROOT, "tests", "apps", "gtk3_probe.py")


def setUpModule():
    global C, STANDIN, STANDIN_ENV, TMP
    if not K.have("labwc"):
        raise unittest.SkipTest("labwc not installed")
    TMP = tempfile.mkdtemp(prefix="awt-wa-", dir=f"/run/user/{os.getuid()}")
    os.chmod(TMP, 0o700)
    if audiomod.have_pipewire():
        # stand-in "host" PipeWire: a second private instance in its own runtime dir
        STANDIN = audiomod.PrivateAudio(os.path.join(TMP, "standin"), os.path.join(TMP, "standin", "rt"))
        os.makedirs(STANDIN.rt, mode=0o700)
        STANDIN_ENV = audiomod.daemon_env(STANDIN.ws_dir, STANDIN.rt, os.environ["PATH"], "unix:path=/nonexistent")

        def spawn(unit, env, argv, out):
            p = subprocess.Popen(["/usr/bin/env", "-i"] + [f"{k}={v}" for k, v in env.items()] + argv,
                                 stdout=out, stderr=subprocess.STDOUT)
            STANDIN_PROCS.append(p)
            return p
        STANDIN.start(spawn, STANDIN_ENV)
    extra = {"AW_AUDIO_HOST_REMOTE": os.path.join(STANDIN.rt, "pipewire-0")} if STANDIN else {}
    C = K.start_daemon("wa", home=os.path.join(TMP, "home"), extra_env=extra) if os.makedirs(
        os.path.join(TMP, "home"), exist_ok=True) is None else None


def tearDownModule():
    K.stop_daemon(C)
    for p in STANDIN_PROCS:
        p.terminate()
    for p in STANDIN_PROCS:
        try:
            p.wait(3)
        except subprocess.TimeoutExpired:
            p.kill()
    shutil.rmtree(TMP, ignore_errors=True)


def shot(ws, **kw):
    import base64
    r = C.call("screen.screenshot", workspace=ws, inline=True, **kw)
    return base64.b64decode(r["base64"]), r


def windows(ws):
    return C.call("window.list", workspace=ws)["windows"]


def wait_window(ws, pred, timeout=20):
    return K.wait_for(lambda: [w for w in windows(ws) if pred(w)], timeout)


def box_of(ws, rgb):
    png, _ = shot(ws)
    return find_box(png, rgb)


class Windows(unittest.TestCase):
    def test_wayland_and_x11_windows_listed_moved_resized_and_reported_truthfully(self):
        C.call("workspace.create", name="win", width=1400, height=800)
        try:
            log4, log3 = os.path.join(K.BASE, "w4.log"), os.path.join(K.BASE, "w3.log")
            for lg in (log4, log3):
                open(lg, "w").close()
            C.call("workspace.launch", workspace="win", argv=[sys.executable, GTK4, "WL", log4, "#c05621"])
            C.call("workspace.launch", workspace="win", argv=["/usr/bin/env", "GDK_BACKEND=x11", sys.executable, GTK3,
                                                              "XL", log3, "#2f855a"])
            wl = wait_window("win", lambda w: w["kind"] == "wayland")[0]
            xw = wait_window("win", lambda w: w["kind"] == "x11")[0]
            self.assertEqual(wl["title"], "probe WL")
            self.assertEqual((xw["title"], xw["class"].lower()), ("gtk3 XL", "gtk3_probe.py"))
            self.assertEqual(xw["geometry_source"], "x11")
            self.assertEqual(xw["app"], 2)                                  # owner: second app launched
            ids = [w["id"] for w in windows("win")]
            self.assertEqual(len(ids), len(set(ids)))
            time.sleep(1.0)
            ORANGE, GREEN = (0xc0, 0x56, 0x21), (0x2f, 0x85, 0x5a)

            def place(w, **g):
                return C.call("window.set_geometry", workspace="win", window=w["id"], **g)["window"]

            # --- absolute placement, verified against the pixels of the screenshot
            wl = place(wl, x=60, y=80, width=520, height=380)
            xw = place(xw, x=700, y=120, width=500, height=360)
            time.sleep(0.8)
            png, _ = shot("win")
            bo, bg = find_box(png, ORANGE), find_box(png, GREEN)
            self.assertEqual((bo[0], bo[2] - bo[0] + 1), (60, 520), bo)       # reported == drawn (Wayland, tracked)
            self.assertAlmostEqual(bo[1], 80, delta=2)
            self.assertEqual((bg[0], bg[2] - bg[0] + 1), (700, 500), bg)      # reported == drawn (X11, exact)
            self.assertAlmostEqual(bg[1], 120, delta=2)
            self.assertEqual(wl["geometry"], {"x": 60, "y": 80, "width": 520, "height": 380})
            self.assertEqual(wl["geometry_source"], "tracked")
            xg = [w for w in windows("win") if w["id"] == xw["id"]][0]["geometry"]
            self.assertEqual(xg, {"x": 700, "y": 120, "width": 500, "height": 360})
            # --- move: geometry changes, size does not
            wl2 = C.call("window.move", workspace="win", window=wl["id"], x=200, y=300)["window"]
            xw2 = C.call("window.move", workspace="win", window=xw["id"], x=800, y=400)["window"]
            time.sleep(0.8)
            png, _ = shot("win")
            bo2, bg2 = find_box(png, ORANGE), find_box(png, GREEN)
            self.assertEqual(bo2[0], 200)
            self.assertAlmostEqual(bo2[1], 300, delta=2)
            self.assertEqual(bo2[2] - bo2[0], bo[2] - bo[0])
            self.assertEqual((bg2[0], bg2[2] - bg2[0] + 1), (800, 500))
            self.assertAlmostEqual(bg2[1], 400, delta=2)
            # --- resize
            C.call("window.resize", workspace="win", window=xw["id"], width=420, height=300)
            C.call("window.resize", workspace="win", window=wl["id"], width=400, height=300)
            time.sleep(0.8)
            png, _ = shot("win")
            bo3, bg3 = find_box(png, ORANGE), find_box(png, GREEN)
            self.assertEqual(bo3[2] - bo3[0] + 1, 400)
            self.assertEqual(bg3[2] - bg3[0] + 1, 420)
            self.assertEqual(bo3[0], 200)                                       # resize kept the origin
            xg = [w for w in windows("win") if w["id"] == xw["id"]][0]["geometry"]
            self.assertEqual((xg["x"], xg["width"], xg["height"]), (800, 420, 300))
            # --- the X11 placement survives the compositor reconfigure done for the Wayland placement
            C.call("window.move", workspace="win", window=wl["id"], x=100, y=100)
            xg = [w for w in windows("win") if w["id"] == xw["id"]][0]["geometry"]
            self.assertEqual((xg["x"], xg["width"], xg["height"]), (800, 420, 300))
            # --- states, both kinds
            for w in (wl, xw):
                r = C.call("window.action", workspace="win", window=w["id"], action="maximize")["window"]
                self.assertTrue(r["maximized"], r)
                r = C.call("window.action", workspace="win", window=w["id"], action="unmaximize")["window"]
                self.assertFalse(r["maximized"], r)
                r = C.call("window.action", workspace="win", window=w["id"], action="fullscreen")["window"]
                self.assertTrue(r["fullscreen"], r)
                self.assertEqual((r["geometry"]["width"], r["geometry"]["height"]), (1400, 800))
                r = C.call("window.action", workspace="win", window=w["id"], action="unfullscreen")["window"]
                self.assertFalse(r["fullscreen"], r)
                r = C.call("window.action", workspace="win", window=w["id"], action="minimize")["window"]
                self.assertTrue(r["minimized"], r)
                r = C.call("window.action", workspace="win", window=w["id"], action="unminimize")["window"]
                self.assertFalse(r["minimized"], r)
            # geometry restored after maximize/fullscreen cycles (X11 read back from the X server)
            xg = [w for w in windows("win") if w["id"] == xw["id"]][0]["geometry"]
            self.assertEqual((xg["x"], xg["y"], xg["width"], xg["height"]), (800, 400, 420, 300))
            # --- focus: exactly one window is focused and it is the one we asked for
            C.call("window.action", workspace="win", window=wl["id"], action="focus")
            self.assertEqual([w["id"] for w in windows("win") if w["focused"]], [wl["id"]])
            # --- close both
            C.call("window.action", workspace="win", window=xw["id"], action="close")
            C.call("window.action", workspace="win", window=wl["id"], action="close")
            self.assertTrue(K.wait_for(lambda: not windows("win"), 10), windows("win"))
        finally:
            C.call("workspace.destroy", workspace="win")

    @unittest.skipUnless(K.have("xeyes") and K.have("xmessage"), "xeyes/xmessage not installed")
    def test_window_list_is_consistent_for_announced_and_unannounced_x11_windows(self):
        C.call("workspace.create", name="xl", width=1000, height=600)
        try:
            C.call("workspace.launch", workspace="xl", argv=["xeyes"])
            C.call("workspace.launch", workspace="xl", argv=["xmessage", "hello"])
            self.assertTrue(K.wait_for(lambda: len(windows("xl")) >= 2, 15))
            time.sleep(1)
            ws_ = windows("xl")
            self.assertEqual(sorted(w["class"] for w in ws_), ["XEyes", "Xmessage"])
            self.assertEqual({w["kind"] for w in ws_}, {"x11"})
            self.assertEqual(len(ws_), 2)                          # xmessage is announced by labwc AND present in X: listed once
            self.assertEqual(sum(1 for w in ws_ if w["focused"]), 1)
            # the workspace window count in the summary agrees
            self.assertEqual(C.call("workspace.get", workspace="xl")["activity"]["window_count"], 2)
        finally:
            C.call("workspace.destroy", workspace="xl")

    def test_set_geometry_on_several_windows_never_stalls(self):
        """Regression: a Wayland connection dispatched from two threads made some window.set_geometry calls
        (typically on the second window of a floating workspace) wait out a 5 s roundtrip timeout, much longer
        with a busy viewer. Real windows, repeated placement, every call must be fast."""
        C.call("workspace.create", name="stall", width=1280, height=720, window_policy="floating")
        try:
            for i, col in enumerate(("#c05621", "#2f855a", "#2b6cb0")):
                lg = os.path.join(K.BASE, f"stall{i}.log")
                open(lg, "w").close()
                C.call("workspace.launch", workspace="stall", argv=[sys.executable, GTK4, f"S{i}", lg, col])
            wait_window("stall", lambda w: True)
            K.wait_for(lambda: len(windows("stall")) >= 3, 20)
            time.sleep(1.0)
            worst = 0.0
            for rnd in range(5):
                for k, w in enumerate(windows("stall")):
                    t0 = time.monotonic()
                    r = C.call("window.set_geometry", workspace="stall", window=w["id"],
                               x=40 + 90 * k + 10 * rnd, y=50 + 70 * k, width=400, height=300)["window"]
                    worst = max(worst, time.monotonic() - t0)
                    self.assertEqual(r["geometry"]["width"], 400)
            self.assertLess(worst, 3.0, f"slowest set_geometry took {worst:.1f} s")
        finally:
            C.call("workspace.destroy", workspace="stall")

    def test_host_x_server_is_never_consulted(self):
        C.call("workspace.create", name="nohost", width=800, height=500)
        try:
            info = C.call("screen.info", workspace="nohost")
            self.assertNotIn(info["x_display"], (":0", ":1"))
            self.assertEqual(C.call("window.list", workspace="nohost")["windows"], [])
            # a socket path of a display that is not served by this workspace's compositor is refused
            from aw import x11
            with self.assertRaises(x11.X11Error):
                x11.XConnection(":0", expect_ancestor_pid=C.call("workspace.get", workspace="nohost") and os.getpid())
        finally:
            C.call("workspace.destroy", workspace="nohost")


def host_snapshot():
    """READ-ONLY view of the host PipeWire graph: node ids, names, classes and states."""
    r = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=10,
                       env={"PATH": os.environ["PATH"], "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"})
    out = []
    for o in json.loads(r.stdout or "[]"):
        if o.get("type", "").endswith(":Node"):
            p = (o.get("info") or {}).get("props") or {}
            out.append((o["id"], p.get("node.name"), p.get("media.class")))
        if o.get("type", "").endswith(":Link"):
            out.append(("link", o["id"]))
    return sorted(out, key=str)


def make_tone(path, freq, seconds=4, amp=9000):
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(b"".join(struct.pack("<hh", *([int(amp * math.sin(2 * math.pi * freq * i / 48000))] * 2))
                               for i in range(48000 * seconds)))


@unittest.skipUnless(audiomod.have_pipewire(), "pipewire, pipewire-pulse, wireplumber and pw-cat are required")
class Audio(unittest.TestCase):
    def test_private_sink_captures_the_workspace_and_nothing_reaches_the_host(self):
        before = host_snapshot()
        C.call("workspace.create", name="aud", width=800, height=500)
        try:
            info = C.call("audio.info", workspace="aud")
            self.assertEqual((info["mode"], info["default_sink"]), ("private", "aw-sink"))
            self.assertTrue(all(info["daemons"].values()), info)
            wid = C.call("workspace.get", workspace="aud")["id"]
            tone440 = os.path.join(TMP, "t440.wav")
            make_tone(tone440, 440)
            # native PipeWire protocol client
            C.call("workspace.launch", workspace="aud", argv=["pw-cat", "-p", tone440])
            time.sleep(1.2)
            os.makedirs(os.path.join(K.BASE, wid, "shots"), exist_ok=True)
            r = C.call("audio.record", workspace="aud", seconds=2, path=os.path.join(K.BASE, wid, "shots", "a.wav"))
            self.assertAlmostEqual(r["tone_hz"], 440, delta=3)
            self.assertGreater(r["rms_dbfs"], -25)
            with wave.open(r["path"]) as w:
                self.assertEqual((w.getnchannels(), w.getframerate()), (2, 48000))
                self.assertGreaterEqual(w.getnframes(), 90000)
            streams = C.call("audio.info", workspace="aud")["streams"]
            self.assertTrue([s for s in streams if s["app"] == "pw-cat"], streams)
            # PulseAudio protocol client (what Chrome/Firefox/VLC use)
            time.sleep(3)
            if K.have("ffmpeg"):
                C.call("workspace.launch", workspace="aud", argv=[
                    "ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=880:duration=5",
                    "-f", "pulse", "-ac", "2", "aw-test"])
                time.sleep(1.5)
                r2 = C.call("audio.record", workspace="aud", seconds=2)
                self.assertAlmostEqual(r2["tone_hz"], 880, delta=4)
                self.assertTrue(r2["path"].startswith(os.path.join(K.BASE, wid)))
            # silence when nothing plays
            time.sleep(5)
            r3 = C.call("audio.record", workspace="aud", seconds=1)
            self.assertEqual(r3["peak"], 0.0)
            # the private daemons never opened a sound device
            for pid in info["daemons"] and [p for p in _workspace_pids(wid)]:
                fds = os.path.join("/proc", pid, "fd")
                try:
                    targets = [os.readlink(os.path.join(fds, f)) for f in os.listdir(fds)]
                except OSError:
                    continue
                self.assertFalse([t for t in targets if t.startswith("/dev/snd")], (pid, targets))
        finally:
            C.call("workspace.destroy", workspace="aud")
        self.assertEqual(host_snapshot(), before, "the host PipeWire graph changed")

    def test_agent_credential_may_record_only_inside_its_workspace(self):
        C.call("workspace.create", name="aperm", width=800, height=500)
        try:
            wid = C.call("workspace.get", workspace="aperm")["id"]
            with open(os.path.join(K.BASE, wid, "token")) as f:
                agent = Client(autostart=False, role="agent", token=f.read().strip())
            with self.assertRaises(ApiError) as cm:
                agent.call("audio.record", workspace="aperm", seconds=0.5, path=os.path.join(TMP, "escape.wav"))
            self.assertEqual(cm.exception.code, "E_BAD_ARGS")
            self.assertFalse(os.path.exists(os.path.join(TMP, "escape.wav")))
            self.assertEqual(agent.call("audio.info", workspace="aperm")["mode"], "private")
            with self.assertRaises(ApiError) as cm:
                agent.call("audio.route", workspace="aperm", to="host")
            self.assertEqual(cm.exception.code, "E_FORBIDDEN")
        finally:
            C.call("workspace.destroy", workspace="aperm")

    def test_audio_off_has_no_audio_socket_at_all(self):
        C.call("workspace.create", name="mute", width=800, height=500, audio="off")
        try:
            wid = C.call("workspace.get", workspace="mute")["id"]
            self.assertEqual(C.call("audio.info", workspace="mute")["mode"], "off")
            rt = os.path.join(K.BASE, wid, "rt")
            self.assertEqual([n for n in os.listdir(rt) if "pipewire" in n or n == "pulse"], [])
            if K.have("ffmpeg"):
                r = C.call("workspace.launch", workspace="mute", argv=[
                    "ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=500:duration=1",
                    "-f", "pulse", "aw-none"])
                self.assertTrue(K.wait_for(lambda: not any(a["running"] for a in C.call("app.list", workspace="mute")["apps"]), 10))
                with open(r["log"]) as f:
                    txt = f.read()
                self.assertIn("Cannot connect context", txt)       # the app found no audio server: not the host's
            with self.assertRaises(ApiError) as cm:
                C.call("audio.record", workspace="mute", seconds=1)
            self.assertEqual(cm.exception.code, "E_AUDIO_OFF")
        finally:
            C.call("workspace.destroy", workspace="mute")

    def test_routing_to_a_host_sink_is_human_only_and_works_against_a_stand_in(self):
        C.call("workspace.create", name="route", width=800, height=500)
        try:
            tone = os.path.join(TMP, "t660.wav")
            make_tone(tone, 660, 12)
            # not from a plain admin connection (no terminal), not from an agent
            with self.assertRaises(ApiError) as cm:
                C.call("audio.route", workspace="route", to="host")
            self.assertEqual(cm.exception.code, "E_NOT_HUMAN_CHANNEL")
            self.assertEqual(C.call("audio.info", workspace="route")["route"]["to"], "none")
            # the person, at a terminal
            env = {"PATH": os.environ["PATH"], "HOME": os.path.join(TMP, "home"), "AW_BASE_DIR": K.BASE,
                   "PYTHONPATH": K.ROOT, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}", "LANG": "C.UTF-8",
                   "AW_NO_AUTOSTART": "1", "TERM": "dumb"}

            def cli(*args):
                pid, fd = pty.fork()
                if pid == 0:
                    os.chdir(K.ROOT)
                    os.execve(sys.executable, [sys.executable, "-m", "aw"] + list(args), env)
                out = b""
                end = time.monotonic() + 20
                while time.monotonic() < end:
                    r, _, _ = select.select([fd], [], [], 0.2)
                    if r:
                        try:
                            c = os.read(fd, 4096)
                        except OSError:
                            break
                        if not c:
                            break
                        out += c
                _, st = os.waitpid(pid, 0)
                return os.waitstatus_to_exitcode(st), out.decode(errors="replace")
            rc, out = cli("audio-route", "route", "host", "--yes")
            self.assertEqual(rc, 0, out)
            self.assertEqual(C.call("audio.info", workspace="route")["route"], {"to": "host", "alive": True})
            C.call("workspace.launch", workspace="route", argv=["pw-cat", "-p", tone])
            time.sleep(1.5)
            rec = STANDIN.record(STANDIN_ENV, 2, None)             # what arrived at the stand-in "host" sink
            self.assertAlmostEqual(rec["tone_hz"], 660, delta=3)
            self.assertGreater(rec["rms_dbfs"], -25)
            rc, out = cli("audio-route", "route", "none")
            self.assertEqual(rc, 0, out)
            time.sleep(1.5)
            self.assertEqual(STANDIN.record(STANDIN_ENV, 1, None)["peak"], 0.0)
        finally:
            C.call("workspace.destroy", workspace="route")


def _workspace_pids(wid):
    out = subprocess.run(["systemctl", "--user", "show", f"aw-{wid}.slice", "-p", "ControlGroup"],
                         capture_output=True, text=True).stdout.strip().split("=", 1)[-1]
    pids = []
    for root, _, files in os.walk("/sys/fs/cgroup" + out):
        if "cgroup.procs" in files:
            with open(os.path.join(root, "cgroup.procs")) as f:
                pids += f.read().split()
    return pids


if __name__ == "__main__":
    unittest.main()
