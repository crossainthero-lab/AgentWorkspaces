"""REAL INTEGRATION TESTS of the optional per-workspace sandbox (`create --sandbox`, bubblewrap).

Each test starts a real daemon, real labwc compositors and real apps (a Python probe, a GTK4 window, Google Chrome
when installed). The probe runs the same checks in an UNSANDBOXED control workspace and in a sandboxed one, so a
"denied" result is only believed when the control shows the path really is reachable without the sandbox.
No window is shown on the user's desktop; no real browser profile is touched (apps run with the workspace's
private HOME). Requires: labwc, bwrap, python3-gi (GTK4).
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as K  # noqa: E402
from aw.client import ApiError  # noqa: E402

C = None
PROBE = os.path.join(K.ROOT, "tests", "apps", "fs_probe.py")
SHARED = None


def setUpModule():
    global C, SHARED, ABSTRACT, ABS_SOCK
    if not (K.have("labwc") and K.have("bwrap")):
        raise unittest.SkipTest("labwc and bwrap are required")
    C = K.start_daemon("sbx")
    SHARED = tempfile.mkdtemp(prefix="aw-sbx-", dir="/tmp")      # canary + rw mount, outside every workspace
    # a stand-in for "an abstract unix socket of the host" (such as the host X server's): we own it, so the
    # test never has to connect to the real one
    import socket
    ABSTRACT = f"aw-test-abstract-{os.getpid()}"
    ABS_SOCK = socket.socket(socket.AF_UNIX)
    ABS_SOCK.bind("\0" + ABSTRACT)
    ABS_SOCK.listen(8)


def tearDownModule():
    K.stop_daemon(C)
    ABS_SOCK.close()
    shutil.rmtree(SHARED, ignore_errors=True)


ABSTRACT = None


def run_probe(ws, tag, canary, other_ws_dir, rw_dir):
    out = os.path.join(rw_dir, f"probe-{tag}.json")
    ws_info = C.call("workspace.get", workspace=ws)
    api = os.path.join(K.BASE, "api.sock")
    token = os.path.join(K.BASE, ws_info["id"], "token")
    real_home = os.path.expanduser("~")
    host_rt = f"/run/user/{os.getuid()}"
    C.call("workspace.launch", workspace=ws, argv=["/usr/bin/python3", PROBE, out, canary, real_home, api, token,
                                                    other_ws_dir, os.path.join(K.ROOT, "tests"), rw_dir, host_rt, ABSTRACT])
    assert K.wait_for(lambda: os.path.exists(out), 15), "probe produced no result"
    return json.load(open(out))


class Containment(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.canary = os.path.join(SHARED, "canary", "secret.txt")
        os.makedirs(os.path.dirname(cls.canary))
        with open(cls.canary, "w") as f:
            f.write("CANARY-" + "x" * 20)
        cls.rw = os.path.join(SHARED, "rw")
        os.makedirs(cls.rw)
        C.call("workspace.create", name="plain", width=640, height=400)
        cls.boxed = C.call("workspace.create", name="boxed", width=640, height=400, sandbox=True,
                           sandbox_mounts=[os.path.join(K.ROOT, "tests") + ":ro", cls.rw + ":rw"])
        cls.other = C.call("workspace.create", name="neighbour", width=640, height=400, start=False)
        cls.plain = run_probe("plain", "plain", cls.canary, os.path.join(K.BASE, cls.other["id"]), cls.rw)
        cls.sb = run_probe("boxed", "boxed", cls.canary, os.path.join(K.BASE, cls.other["id"]), cls.rw)
        C.call("workspace.destroy", workspace="plain")           # at most two compositors at a time
        C.call("workspace.create", name="nonet", width=640, height=400, sandbox=True, sandbox_net=False,
               sandbox_mounts=[os.path.join(K.ROOT, "tests") + ":ro", cls.rw + ":rw"])
        cls.nonet = run_probe("nonet", "nonet", cls.canary, os.path.join(K.BASE, cls.other["id"]), cls.rw)
        C.call("workspace.destroy", workspace="nonet")

    @classmethod
    def tearDownClass(cls):
        for n in ("plain", "boxed", "neighbour", "nonet"):
            try:
                C.call("workspace.destroy", workspace=n)
            except ApiError:
                pass

    def test_control_shows_the_probe_checks_are_meaningful(self):
        p = self.plain
        for k in ("canary_readable", "api_socket_visible", "token_file_readable",
                  "api_socket_connectable", "other_workspace_dir_visible",
                  "write_tmp", "write_home", "write_rw_mount"):
            self.assertTrue(p[k], f"control: {k} should be reachable without the sandbox")
        self.assertGreater(p["host_pids_visible"], 30)

    def test_paths_outside_the_allowed_set_are_not_readable(self):
        s = self.sb
        for k in ("canary_readable", "ssh_dir_visible", "api_socket_visible",
                  "token_file_readable", "api_socket_connectable", "other_workspace_dir_visible",
                  "host_wayland_socket_visible", "host_session_bus_visible"):
            self.assertFalse(s[k], f"sandbox leaked: {k}")

    def test_real_home_shows_nothing_but_the_path_to_a_mounted_project(self):
        home = os.path.expanduser("~")
        mounted_top = {os.path.relpath(m, home).split(os.sep)[0] for m in
                       (os.path.join(K.ROOT, "tests"),) if m.startswith(home + os.sep)}
        self.assertLessEqual(set(self.sb["real_home_entries"]), mounted_top)
        self.assertGreater(len(self.plain["real_home_entries"]), 3)

    def test_host_runtime_dir_shows_only_the_workspaces_own_path(self):
        self.assertEqual(self.sb["host_runtime_entries"], [os.path.basename(K.BASE)])   # skeleton of the mounted rt dir
        self.assertGreater(len(self.plain["host_runtime_entries"]), 8)

    def test_abstract_unix_sockets_of_the_host_stay_reachable_unless_network_is_unshared(self):
        """Honest limit, measured: bwrap shares the network namespace by default, and abstract unix sockets live in
        it. (Stand-in socket owned by this test; the real host X server's abstract socket works the same way.)"""
        self.assertTrue(self.plain["abstract_socket_connectable"])
        self.assertTrue(self.sb["abstract_socket_connectable"], "expected: default sandbox shares the net namespace")
        self.assertFalse(self.nonet["abstract_socket_connectable"])
        self.assertNotEqual(self.nonet["network_namespace"], self.sb["network_namespace"])
        self.assertEqual(self.sb["network_namespace"], self.plain["network_namespace"])

    def test_process_namespace_hides_host_processes(self):
        self.assertLess(self.sb["host_pids_visible"], 12)
        self.assertFalse(self.sb["parent_environ_readable"] and self.sb["host_pids_visible"] > 30)

    def test_write_permissions(self):
        s = self.sb
        self.assertFalse(s["write_usr"])
        self.assertFalse(s["write_ro_mount"])
        self.assertTrue(s["write_rw_mount"])
        self.assertTrue(s["write_home"])
        self.assertTrue(s["write_tmp"])
        self.assertFalse(os.path.exists(s["tmp_marker"]), "private /tmp write appeared on the host")

    def test_environment_inside_is_the_workspaces_own(self):
        s = self.sb
        self.assertEqual(s["home"], os.path.join(K.BASE, self.boxed["id"], "home"))
        self.assertEqual(s["xdg_runtime_dir"], os.path.join(K.BASE, self.boxed["id"], "rt"))
        self.assertEqual(s["wayland_display"], "wayland-0")
        self.assertEqual(s["uid"], os.getuid())

    def test_refused_mounts_and_cwd(self):
        for bad, why in (("/", "root"), (os.path.expanduser("~"), "home contains ~/.ssh etc"), (K.BASE, "base dir"),
                         (os.path.join(K.BASE, self.boxed["id"]), "inside base dir"), ("/run/user/%d" % os.getuid(),
                                                                                       "runtime dir"),
                         ("relative/path", "relative"), ("/nonexistent-aw", "missing")):
            with self.assertRaises(ApiError, msg=why) as cm:
                C.call("workspace.create", name="bad", sandbox=True, sandbox_mounts=[bad], start=False)
            self.assertEqual(cm.exception.code, "E_BAD_SANDBOX", why)
        with self.assertRaises(ApiError) as cm:                           # cwd outside the sandbox
            C.call("workspace.launch", workspace="boxed", argv=["/usr/bin/true"], cwd=SHARED)
        self.assertEqual(cm.exception.code, "E_BAD_ARGS")
        with self.assertRaises(ApiError) as cm:
            C.call("workspace.create", name="shared-home", sandbox=True, home="shared", start=False)
        self.assertEqual(cm.exception.code, "E_BAD_HOME")

    def test_sandbox_is_off_by_default_and_reported(self):
        self.assertFalse(C.call("workspace.get", workspace="neighbour")["sandbox"]["enabled"])
        self.assertTrue(C.call("workspace.get", workspace="boxed")["sandbox"]["enabled"])


class GraphicalAppsInsideTheSandbox(unittest.TestCase):
    def test_gtk4_app_renders_and_receives_input(self):
        C.call("workspace.create", name="sgtk", width=900, height=600, sandbox=True,
               sandbox_mounts=[os.path.join(K.ROOT, "tests") + ":ro", SHARED + ":rw"])
        try:
            log = os.path.join(SHARED, "gtk.log")
            open(log, "w").close()
            C.call("workspace.launch", workspace="sgtk",
                   argv=["/usr/bin/python3", os.path.join(K.ROOT, "tests", "apps", "gtk_probe.py"), "SANDBOXED",
                         log, "#2b6cb0"])
            self.assertTrue(K.wait_for(lambda: any(e.get("ev") == "ready" for e in K.log_events(log)), 20))
            self.assertTrue(K.wait_for(lambda: [w for w in C.call("window.list", workspace="sgtk")["windows"]
                                                if w["maximized"]], 10))
            time.sleep(1.0)
            C.call("input.click", workspace="sgtk", x=450, y=232)
            self.assertTrue(K.wait_for(lambda: any(e["ev"] == "button" for e in K.log_events(log)), 5),
                            "click did not reach the sandboxed app")
            C.call("input.click", workspace="sgtk", x=200, y=190)
            C.call("input.type", workspace="sgtk", text="boxed")
            self.assertTrue(K.wait_for(lambda: any(e.get("ev") == "text" for e in K.log_events(log)), 5))
            import base64
            png = base64.b64decode(C.call("screen.screenshot", workspace="sgtk", inline=True)["base64"])
            cols = {K.pixel(png, x, y) for x in range(100, 800, 60) for y in range(40, 120, 20)}
            self.assertIn((0x2b, 0x6c, 0xb0), cols)
        finally:
            C.call("workspace.destroy", workspace="sgtk")

    @unittest.skipUnless(K.have("google-chrome"), "google-chrome not installed")
    def test_chrome_runs_inside_the_sandbox_only_with_its_own_sandbox_off(self):
        """Measured: Chrome's own sandbox cannot start inside bwrap here (nested user namespaces are restricted
        by AppArmor on Ubuntu 26.04), so it needs --no-sandbox, replacing Chrome's sandbox by this one."""
        C.call("workspace.create", name="schr", width=1200, height=700, sandbox=True)
        try:
            page = ("data:text/html,<body style='margin:0;background:%23d53f8c'><h1 style='color:white;"
                    "font-size:80px'>CHROME-IN-SANDBOX</h1>")
            flags = ["--ozone-platform=wayland", "--no-first-run", "--no-default-browser-check",
                     "--password-store=basic", "--app=" + page]
            C.call("workspace.launch", workspace="schr", argv=["google-chrome"] + flags)
            time.sleep(6)
            self.assertEqual(C.call("window.list", workspace="schr")["windows"], [],
                             "chrome with its own sandbox unexpectedly started inside bwrap")
            C.call("workspace.launch", workspace="schr", argv=["google-chrome", "--no-sandbox"] + flags)
            self.assertTrue(K.wait_for(lambda: C.call("window.list", workspace="schr")["windows"], 25))
            time.sleep(3)
            import base64
            png = base64.b64decode(C.call("screen.screenshot", workspace="schr", inline=True)["base64"])
            cols = {K.pixel(png, x, y) for x in range(300, 1100, 80) for y in range(300, 650, 60)}
            self.assertIn((0xd5, 0x3f, 0x8c), cols)
        finally:
            C.call("workspace.destroy", workspace="schr")


if __name__ == "__main__":
    unittest.main()
