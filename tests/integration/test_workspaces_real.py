"""REAL INTEGRATION TESTS. These start real labwc compositors and real GTK/X11/Chromium apps.

They are NOT simulations: each assertion is made against the output of a real app inside a real
workspace compositor. Requirements: labwc, python3-gi (GTK4), systemd --user, a render node (GPU tests
skip without NVIDIA). No window is ever shown on the user's desktop: the "personal desktop" in these
tests is a HostSim stand-in (a separate headless compositor).
"""
import os
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as K  # noqa: E402
from aw.client import ApiError  # noqa: E402

C = None


def setUpModule():
    global C
    if not K.have("labwc"):
        raise unittest.SkipTest("labwc not installed")
    C = K.start_daemon("int")


def tearDownModule():
    K.stop_daemon(C)


def shot(ws, **kw):
    import base64
    r = C.call("screen.screenshot", workspace=ws, inline=True, **kw)
    return base64.b64decode(r["base64"]), r


class Lifecycle(unittest.TestCase):
    def test_create_launch_destroy_leaves_nothing(self):
        w = C.call("workspace.create", name="life", agent_kind="claude-code", width=800, height=500)
        self.assertEqual(w["state"], "running")
        log, app = K.launch_probe(C, "life", "LIFE", "#2b6cb0", "life.log")
        pids = [w2 for w2 in [app["pid"]]]
        st = C.call("workspace.get", workspace="life")
        self.assertGreater(st["usage"].get("memory_bytes", 1), 0)
        self.assertEqual(st["windows"][0]["title"], "probe LIFE")
        d = os.path.join(K.BASE, w["id"])
        C.call("workspace.destroy", workspace="life")
        self.assertFalse(os.path.exists(d))
        self.assertTrue(K.wait_for(lambda: not os.path.exists(f"/proc/{pids[0]}"), 6), "app survived destroy")
        out = subprocess.run(["systemctl", "--user", "list-units", f"aw-{w['id']}*", "--no-legend"],
                             capture_output=True, text=True).stdout.strip()
        self.assertEqual(out, "", "systemd scopes left behind")

    def test_resolution_and_scale(self):
        C.call("workspace.create", name="disp", width=1024, height=640)
        try:
            png, r = shot("disp")
            self.assertEqual((r["width"], r["height"]), (1024, 640))
            C.call("workspace.configure", workspace="disp", width=1600, height=900)
            png, r = shot("disp")
            self.assertEqual((r["width"], r["height"]), (1600, 900))
            C.call("workspace.configure", workspace="disp", width=1600, height=900, scale=2.0)
            info = C.call("screen.info", workspace="disp")
            self.assertEqual(info["outputs"][0]["scale"], 2.0)
            png, r = shot("disp", region=[0, 0, 100, 100])
            self.assertEqual(r["width"], 200)  # 100 logical px at scale 2 = 200 physical px
        finally:
            C.call("workspace.destroy", workspace="disp")


class TwoWorkspacesAreIndependent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        C.call("workspace.create", name="A", agent_kind="claude-code", width=1000, height=640)
        C.call("workspace.create", name="B", agent_kind="codex", width=1000, height=640)
        cls.logA, _ = K.launch_probe(C, "A", "ALPHA", "#c05621", "A.log")
        cls.logB, _ = K.launch_probe(C, "B", "BRAVO", "#2f855a", "B.log")

    @classmethod
    def tearDownClass(cls):
        C.call("workspace.destroy", workspace="A")
        C.call("workspace.destroy", workspace="B")

    def test_screenshots_come_from_the_right_workspace(self):
        pa, _ = shot("A")
        pb, _ = shot("B")
        # banner background colour is sampled at the same spot in both workspaces
        ca, cb = K.pixel(pa, 60, 60), K.pixel(pb, 60, 60)
        self.assertEqual(ca, (0xc0, 0x56, 0x21))
        self.assertEqual(cb, (0x2f, 0x85, 0x5a))
        # repeated, interleaved captures never cross over
        for _ in range(5):
            self.assertEqual(K.pixel(shot("B")[0], 60, 60), cb)
            self.assertEqual(K.pixel(shot("A")[0], 60, 60), ca)

    def test_pointer_keyboard_focus_are_independent(self):
        a0 = len(K.log_events(self.logA))
        b0 = len(K.log_events(self.logB))
        C.call("input.move", workspace="A", agent_kind="claude-code", x=300, y=300)
        C.call("input.move", workspace="B", agent_kind="codex", x=700, y=200)
        ia = C.call("screen.info", workspace="A")["pointer"]
        ib = C.call("screen.info", workspace="B")["pointer"]
        self.assertEqual((ia["x"], ia["y"]), (300, 300))
        self.assertEqual((ib["x"], ib["y"]), (700, 200))
        # click into the entry of A only, then type: B's entry must stay empty
        C.call("input.click", workspace="A", agent_kind="claude-code", x=200, y=190)
        C.call("input.type", workspace="A", agent_kind="claude-code", text="only-in-A")
        time.sleep(0.4)
        ea = [e for e in K.log_events(self.logA)[a0:] if e["ev"] == "text"]
        eb = [e for e in K.log_events(self.logB)[b0:] if e["ev"] in ("text", "key", "button")]
        self.assertTrue(ea and ea[-1]["text"] == "only-in-A", ea[-3:])
        self.assertEqual(eb, [], "input leaked into the other workspace")
        motion_b = [e for e in K.log_events(self.logB)[b0:] if e["ev"] == "motion"]
        for e in motion_b:  # B only ever saw its own pointer position
            self.assertLess(abs(e["x"] - 700), 80)
        wa = C.call("window.list", workspace="A", agent_kind="claude-code")["windows"]
        wb = C.call("window.list", workspace="B", agent_kind="codex")["windows"]
        self.assertTrue(wa[0]["focused"] and wb[0]["focused"])  # each has its own focused window

    def test_agent_assignment_blocks_cross_agent_control(self):
        with self.assertRaises(ApiError) as cm:
            C.call("input.click", workspace="A", agent_kind="codex", x=5, y=5)
        self.assertEqual(cm.exception.code, "E_ASSIGNMENT")

    def test_clipboards_are_separate(self):
        C.call("clipboard.set", workspace="A", agent_kind="claude-code", text="secret-for-A")
        self.assertEqual(C.call("clipboard.get", workspace="A")["text"], "secret-for-A")
        self.assertNotEqual(C.call("clipboard.get", workspace="B")["text"], "secret-for-A")
        C.call("clipboard.set", workspace="B", agent_kind="codex", text="secret-for-B é")
        self.assertEqual(C.call("clipboard.get", workspace="A")["text"], "secret-for-A")
        self.assertEqual(C.call("clipboard.get", workspace="B")["text"], "secret-for-B é")


class ManualTakeover(unittest.TestCase):
    def test_takeover_blocks_agent_and_allows_human(self):
        C.call("workspace.create", name="TK", agent_kind="claude-code", width=900, height=600)
        try:
            log, _ = K.launch_probe(C, "TK", "TAKE", "#6b46c1", "TK.log")
            n0 = len(K.log_events(log))
            C.call("workspace.pause", workspace="TK", reason="human is typing")
            for m, p in (("input.click", dict(x=450, y=232)), ("input.type", dict(text="zzz")),
                         ("input.key", dict(combo="a")), ("input.move", dict(x=10, y=10)),
                         ("input.scroll", dict(dy=2))):
                with self.assertRaises(ApiError) as cm:
                    C.call(m, workspace="TK", agent_kind="claude-code", **p)
                self.assertEqual(cm.exception.code, "E_PAUSED", m)
            time.sleep(0.5)
            self.assertEqual(K.log_events(log)[n0:], [], "agent input reached the app during takeover")
            # the human (viewer / CLI --human) is still allowed
            C.call("input.click", workspace="TK", agent_kind="human", x=450, y=232)  # on the button
            self.assertTrue(K.wait_for(lambda: any(e["ev"] == "button" for e in K.log_events(log)[n0:]), 3))
            C.call("workspace.resume", workspace="TK")
            n1 = len(K.log_events(log))
            C.call("input.click", workspace="TK", agent_kind="claude-code", x=450, y=232)
            self.assertTrue(K.wait_for(lambda: len(K.log_events(log)) > n1, 3), "agent input after resume")
        finally:
            C.call("workspace.destroy", workspace="TK")


class LauncherSafeguards(unittest.TestCase):
    def test_app_environment_has_no_host_sockets(self):
        w = C.call("workspace.create", name="envt", width=640, height=400)
        try:
            out = os.path.join(K.BASE, "env.out")
            C.call("workspace.launch", workspace="envt", argv=["/bin/sh", "-c", f"env > {out}; sleep 1"])
            self.assertTrue(K.wait_for(lambda: os.path.exists(out) and os.path.getsize(out) > 0, 5))
            env = dict(l.split("=", 1) for l in open(out).read().splitlines() if "=" in l)
            host_rt = f"/run/user/{os.getuid()}"
            self.assertEqual(env["XDG_RUNTIME_DIR"], os.path.join(K.BASE, w["id"], "rt"))
            self.assertNotEqual(env["XDG_RUNTIME_DIR"], host_rt)
            self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-0")
            self.assertNotEqual(env.get("DISPLAY"), ":0")
            self.assertTrue(env["DBUS_SESSION_BUS_ADDRESS"].startswith("unix:path=" + env["XDG_RUNTIME_DIR"]))
            for bad in ("SSH_AUTH_SOCK", "XAUTHORITY", "SESSION_MANAGER", "XDG_SESSION_ID", "PIPEWIRE_REMOTE"):
                self.assertNotIn(bad, env)
            self.assertEqual(env["HOME"], os.path.join(K.BASE, w["id"], "home"))  # private HOME by default
        finally:
            C.call("workspace.destroy", workspace="envt")

    def test_stopped_workspace_launch_fails_closed_and_spawns_nothing(self):
        C.call("workspace.create", name="stp", width=640, height=400)
        try:
            C.call("workspace.stop", workspace="stp")
            marker = os.path.join(K.BASE, "should-not-exist")
            with self.assertRaises(ApiError) as cm:
                C.call("workspace.launch", workspace="stp", argv=["/bin/sh", "-c", f"touch {marker}"])
            self.assertEqual(cm.exception.code, "E_NOT_RUNNING")
            time.sleep(0.5)
            self.assertFalse(os.path.exists(marker))
        finally:
            C.call("workspace.destroy", workspace="stp")

    def test_workspace_processes_hold_no_connection_to_host_sockets(self):
        """Structural audit: every unix socket of every process in the workspace slice is checked
        against the host's own socket inventory (wayland, session bus, X)."""
        w = C.call("workspace.create", name="aud", width=800, height=500)
        try:
            K.launch_probe(C, "aud", "AUD", "#2b6cb0", "aud.log")
            r = subprocess.run(["systemctl", "--user", "show", f"aw-{w['id']}.slice", "-p", "ControlGroup"],
                               capture_output=True, text=True)
            cg = "/sys/fs/cgroup" + r.stdout.strip().split("=", 1)[1]
            pids = set()
            for root, dirs, files in os.walk(cg):
                if "cgroup.procs" in files:
                    pids.update(int(x) for x in open(os.path.join(root, "cgroup.procs")).read().split())
            self.assertGreaterEqual(len(pids), 2)
            ss = subprocess.run(["ss", "-xpnH"], capture_output=True, text=True).stdout.splitlines()
            rows = []
            for line in ss:
                f = line.split()
                if len(f) < 8:
                    continue
                rows.append({"state": f[1], "local": f[4], "inode": f[5], "peer": f[6], "peer_inode": f[7],
                             "proc": line[line.find("users:"):] if "users:" in line else ""})
            by_inode = {r["inode"]: r for r in rows}
            host_rt = f"/run/user/{os.getuid()}"
            forbidden = {f"{host_rt}/wayland-0", f"{host_rt}/bus", "/tmp/.X11-unix/X0", "@/tmp/.X11-unix/X0"}
            checked = 0
            for r in rows:
                if not any(f"pid={p}," in r["proc"] for p in pids):
                    continue
                checked += 1
                peer = by_inode.get(r["peer_inode"])
                for name in (r["local"], r["peer"], peer["local"] if peer else ""):
                    self.assertNotIn(name, forbidden, f"workspace process holds host socket: {r}")
            self.assertGreater(checked, 0)
        finally:
            C.call("workspace.destroy", workspace="aud")


@unittest.skipUnless(K.have("xclock"), "xclock (XWayland client) not installed")
@unittest.skipUnless(K.have("xeyes") and K.have("xwininfo"), "xeyes/xwininfo (XWayland client tools) not installed")
class XWaylandApp(unittest.TestCase):
    def test_x11_app_runs_on_the_workspaces_own_xwayland(self):
        C.call("workspace.create", name="x11", width=800, height=500)
        try:
            out = os.path.join(K.BASE, "x11.env")
            C.call("workspace.launch", workspace="x11", argv=["/bin/sh", "-c", f"echo $DISPLAY > {out}; exec xeyes"])
            self.assertTrue(K.wait_for(lambda: os.path.exists(out) and os.path.getsize(out) > 0, 5))
            disp = open(out).read().strip()
            xd = C.call("screen.info", workspace="x11")["x_display"]
            self.assertEqual(disp, xd)
            self.assertNotEqual(disp, ":0")
            # the window exists in THIS workspace's X server (xwininfo is a plain X client aimed at that display)
            def x_has_window():
                r = subprocess.run(["xwininfo", "-display", xd, "-root", "-tree"], capture_output=True, text=True, timeout=5)
                return "xeyes" in r.stdout.lower()
            self.assertTrue(K.wait_for(x_has_window, 10), "xeyes window not found in the workspace X server")
            time.sleep(1.5)
            png, _ = shot("x11")
            w_, h_, rows = K.decode_png(png)
            colours = {rows[y][x * 3:x * 3 + 3] for y in range(60, 450, 9) for x in range(20, 780, 9)}
            self.assertGreater(len(colours), 2, "X11 client did not render into the workspace screenshot")
        finally:
            C.call("workspace.destroy", workspace="x11")


@unittest.skipUnless(K.have("google-chrome"), "google-chrome not installed")
class ChromiumApp(unittest.TestCase):
    def test_chrome_native_wayland_renders_distinct_content(self):
        C.call("workspace.create", name="chr", agent_kind="hermes", width=1200, height=700)
        try:
            page = "data:text/html,<body style='margin:0;background:%23d53f8c'><h1 style='color:white;font-size:80px'>CHROME-IN-WS</h1>"
            C.call("workspace.launch", workspace="chr", argv=[
                "google-chrome", "--ozone-platform=wayland", "--no-first-run", "--no-default-browser-check",
                "--disable-features=Translate", "--password-store=basic", "--app=" + page])
            self.assertTrue(K.wait_for(lambda: C.call("window.list", workspace="chr")["windows"], 20))
            time.sleep(3)
            win = C.call("window.list", workspace="chr")["windows"][0]
            self.assertIn("chrome", win["app_id"].lower())
            png, _ = shot("chr")
            colours = {K.pixel(png, x, y) for x in range(300, 1100, 80) for y in range(300, 650, 60)}
            self.assertIn((0xd5, 0x3f, 0x8c), colours, colours)
        finally:
            C.call("workspace.destroy", workspace="chr")


@unittest.skipUnless(os.environ.get("AW_TEST_ELECTRON_BIN") and os.environ.get("AW_TEST_ELECTRON_APP"),
                     "set AW_TEST_ELECTRON_BIN and AW_TEST_ELECTRON_APP to an Electron binary and a test app dir "
                     "(see docs/testing.md); Electron is not claimed as supported without this test")
class ElectronApp(unittest.TestCase):
    def test_electron_native_wayland(self):
        C.call("workspace.create", name="el", agent_kind="hermes", width=1100, height=700)
        try:
            C.call("workspace.launch", workspace="el", argv=[
                os.environ["AW_TEST_ELECTRON_BIN"], "--no-sandbox", "--ozone-platform=wayland",
                os.environ["AW_TEST_ELECTRON_APP"]])
            self.assertTrue(K.wait_for(lambda: C.call("window.list", workspace="el")["windows"], 30))
            time.sleep(3)
            png, _ = shot("el")
            colours = {K.pixel(png, x, y) for x in range(200, 1000, 80) for y in range(250, 650, 60)}
            self.assertIn((0xd6, 0x9e, 0x2e), colours, colours)
        finally:
            C.call("workspace.destroy", workspace="el")


class McpEndToEnd(unittest.TestCase):
    def test_mcp_stdio_drives_assigned_workspace(self):
        import json
        C.call("workspace.create", name="mcpws", agent_kind="hermes", width=900, height=600)
        try:
            log, _ = K.launch_probe(C, "mcpws", "MCP", "#2b6cb0", "mcp.log")
            env = {"PATH": os.environ["PATH"], "HOME": os.path.expanduser("~"), "AW_BASE_DIR": K.BASE,
                   "AW_AGENT_KIND": "hermes", "PYTHONPATH": K.ROOT}
            p = subprocess.Popen([sys.executable, "-m", "aw", "mcp"], env=env, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, text=True, cwd=K.ROOT)

            def rpc(i, method, params=None):
                p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}) + "\n")
                p.stdin.flush()
                return json.loads(p.stdout.readline())["result"]
            rpc(1, "initialize", {"protocolVersion": "2025-06-18"})
            png = rpc(2, "tools/call", {"name": "screenshot", "arguments": {}})
            self.assertEqual(png["content"][0]["type"], "image")
            n0 = len(K.log_events(log))
            self.assertFalse(rpc(3, "tools/call", {"name": "mouse_click", "arguments": {"x": 450, "y": 232}})["isError"])
            self.assertTrue(K.wait_for(lambda: any(e["ev"] == "button" for e in K.log_events(log)[n0:]), 3))
            C.call("workspace.pause", workspace="mcpws")
            r = rpc(4, "tools/call", {"name": "mouse_click", "arguments": {"x": 450, "y": 232}})
            self.assertTrue(r["isError"])
            self.assertIn("E_PAUSED", r["content"][0]["text"])
            p.stdin.close()
            p.wait(5)
        finally:
            C.call("workspace.destroy", workspace="mcpws")


class ViewerAndHostStandIn(unittest.TestCase):
    """The viewer runs in HostSim, a stand-in for the personal desktop. HostSim also hosts a probe window
    that logs every pointer/key event it receives: after all the agent activity it must have seen none."""

    def test_enter_view_takeover_close_and_agent_continues(self):
        from harness import HostSim
        host = HostSim(K.BASE)
        try:
            hostlog = os.path.join(K.BASE, "host-probe.log")
            host.spawn([sys.executable, K.PROBE, "PERSONAL", hostlog, "#718096"])
            self.assertTrue(K.wait_for(lambda: any(e.get("ev") == "ready" for e in K.log_events(hostlog)), 15))
            time.sleep(0.5)
            host_pointer_before = dict(host.sess.display_info()["pointer"])
            host_focus_before = [w["title"] for w in host.sess.list_windows() if w["focused"]]

            C.call("workspace.create", name="vw", agent_kind="claude-code", width=900, height=600)
            wlog, _ = K.launch_probe(C, "vw", "VIEWED", "#c05621", "vw.log")
            host_n0 = len(K.log_events(hostlog))

            # agent activity while a viewer is open
            viewer = host.spawn([sys.executable, "-m", "aw.viewer", "vw"],
                                {"AW_BASE_DIR": K.BASE, "PYTHONPATH": K.ROOT}, log=os.path.join(K.BASE, "viewer.log"))
            self.assertTrue(K.wait_for(lambda: C.call("workspace.get", workspace="vw")["viewers"] == 1, 15),
                            "viewer did not attach")
            time.sleep(1.5)
            png = host.shot()
            self.assertEqual(K.pixel(png, 800, 450) != (0, 0, 0), True)

            for i in range(5):
                C.call("input.move", workspace="vw", agent_kind="claude-code", x=100 + i * 50, y=300)
            C.call("input.click", workspace="vw", agent_kind="claude-code", x=450, y=232)
            C.call("input.type", workspace="vw", agent_kind="claude-code", text="agent text")

            # close the viewer through its own window; the workspace and the agent must keep working
            wins = host.sess.list_windows()
            vwin = [w for w in wins if "AgentWorkspaces: vw" in w["title"]][0]
            host.sess.window_action(vwin["id"], "close")
            self.assertTrue(K.wait_for(lambda: C.call("workspace.get", workspace="vw")["viewers"] == 0, 10),
                            "viewer count did not drop")
            n = len(K.log_events(wlog))
            C.call("input.click", workspace="vw", agent_kind="claude-code", x=450, y=232)
            self.assertTrue(K.wait_for(lambda: len(K.log_events(wlog)) > n, 3), "agent stopped working after viewer closed")
            self.assertEqual(C.call("workspace.get", workspace="vw")["state"], "running")

            # the stand-in personal desktop saw nothing
            self.assertEqual(K.log_events(hostlog)[host_n0:], [], "stand-in desktop received events")
            self.assertEqual(dict(host.sess.display_info()["pointer"]), host_pointer_before)
            self.assertEqual([w["title"] for w in host.sess.list_windows() if w["focused"]], host_focus_before)
        finally:
            host.close()
            C.call("workspace.destroy", workspace="vw")

    def test_human_input_through_the_viewer_reaches_the_workspace(self):
        from harness import HostSim
        host = HostSim(K.BASE, size=(1400, 900))
        try:
            C.call("workspace.create", name="vh", agent_kind="claude-code", width=900, height=600)
            wlog, _ = K.launch_probe(C, "vh", "HUMAN", "#2b6cb0", "vh.log")
            host.spawn([sys.executable, "-m", "aw.viewer", "vh", "--takeover"],
                       {"AW_BASE_DIR": K.BASE, "PYTHONPATH": K.ROOT}, log=os.path.join(K.BASE, "viewer2.log"))
            self.assertTrue(K.wait_for(lambda: C.call("workspace.get", workspace="vh")["input_mode"] == "manual", 15),
                            "takeover from the viewer did not pause the agent")
            with self.assertRaises(ApiError) as cm:
                C.call("input.click", workspace="vh", agent_kind="claude-code", x=5, y=5)
            self.assertEqual(cm.exception.code, "E_PAUSED")
            time.sleep(1.5)
            win = [w for w in host.sess.list_windows() if "AgentWorkspaces: vh" in w["title"]][0]
            host.sess.window_action(win["id"], "maximize")
            time.sleep(1)
            n0 = len(K.log_events(wlog))
            # "the human" clicks the workspace's button *through the viewer* (HostSim input -> viewer -> workspace)
            # The 900x600 workspace is fitted into the viewer area; locate its button by scanning HostSim's pixels is
            # fragile, so drive the position from the known layout: fit scale = min(W/900, H/600).
            png = host.shot()
            w_, h_, rows = K.decode_png(png)
            host.sess.pointer_move(650, 430)    # a person hovers before clicking
            time.sleep(0.2)
            host.sess.pointer_move(690, 470)
            time.sleep(0.2)
            host.sess.click(700, 480)           # somewhere inside the picture; maps to a workspace pixel
            host.sess.type_text("hello")
            self.assertTrue(K.wait_for(lambda: any(e["ev"] in ("motion", "button") for e in K.log_events(wlog)[n0:]), 5),
                            "pointer through viewer did not reach the workspace")
            self.assertTrue(K.wait_for(lambda: [e for e in K.log_events(wlog)[n0:] if e["ev"] == "text"
                                                                  and e["text"] == "hello"], 5),
                            "keyboard through viewer did not reach the workspace")
        finally:
            host.close()
            C.call("workspace.destroy", workspace="vh")


@unittest.skipUnless(K.have("nvidia-smi") and subprocess.run(["nvidia-smi", "-L"], capture_output=True).returncode == 0,
                     "no NVIDIA GPU with a working driver on this machine")
class GpuAcceleration(unittest.TestCase):
    def test_compositor_and_app_use_the_gpu(self):
        w = C.call("workspace.create", name="gpu", width=1280, height=720, gpu="auto")
        try:
            log = os.path.join(K.BASE, "vk.log")
            C.call("workspace.launch", workspace="gpu", argv=["vkcube", "--wsi", "wayland"])
            time.sleep(4)
            st = C.call("workspace.get", workspace="gpu")
            comp_pid = None
            for p in os.listdir("/proc"):
                if p.isdigit():
                    try:
                        env = open(f"/proc/{p}/environ", "rb").read()
                    except OSError:
                        continue
                    if os.path.join(K.BASE, w["id"], "rt").encode() in env and open(f"/proc/{p}/comm").read().strip() == "labwc":
                        comp_pid = int(p)
            self.assertIsNotNone(comp_pid)
            out = subprocess.run(["nvidia-smi", "pmon", "-c", "2", "-s", "u"], capture_output=True, text=True).stdout
            pids = {int(l.split()[1]) for l in out.splitlines() if l.strip() and not l.startswith("#")}
            self.assertIn(comp_pid, pids, "compositor is not a GPU process")
            app_pid = st["apps"][0]["pid"]
            self.assertIn(app_pid, pids, "vkcube is not a GPU process")
            png, _ = shot("gpu")
            self.assertGreater(len({K.pixel(png, x, y) for x in range(100, 1200, 60) for y in range(100, 650, 40)}), 3)
        finally:
            C.call("workspace.destroy", workspace="gpu")


if __name__ == "__main__":
    unittest.main(verbosity=2)
