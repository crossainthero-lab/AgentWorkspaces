"""UNIT tests of the routing guarantees: every screenshot/input call must be routed to the live compositor of the
authorised workspace and never anywhere else. The real-compositor versions are in
tests/integration/test_reliability_real.py (RoutingFailures, CompositorCrash)."""
import os
import re
import shutil
import socket
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)


class VerifyRouting(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="awr", dir="/tmp")
        os.environ["AW_BASE_DIR"] = self.base
        from aw import manager
        self.manager = manager
        self.m = manager.Manager()
        self.ws = self.m.create("r", start=False)
        os.makedirs(self.ws.rt, mode=0o700, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)
        os.environ.pop("AW_BASE_DIR", None)

    def code(self):
        with self.assertRaises(self.manager.ManagerError) as cm:
            self.m.verify_routing(self.ws)
        return cm.exception.code

    def fake_running(self, pid, start):
        ws = self.ws
        os.makedirs(ws.rt, mode=0o700, exist_ok=True)
        ws.state, ws.compositor_pid, ws.compositor_start = "running", pid, start

    def test_stopped_workspace_is_not_routable(self):
        self.assertEqual(self.code(), "E_NOT_RUNNING")
        with self.assertRaises(self.manager.ManagerError) as cm:
            self.m.session(self.ws)
        self.assertEqual(cm.exception.code, "E_NOT_RUNNING")

    def test_dead_pid_with_a_stale_socket_file(self):
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(self.ws.socket)          # a leftover socket file
        srv.close()
        p = os.fork()
        if p == 0:
            os._exit(0)
        os.waitpid(p, 0)
        self.fake_running(p, "1")
        self.assertEqual(self.code(), "E_NOT_RUNNING")
        self.assertEqual(self.ws.state, "stopped")      # recorded, so every later call fails fast

    def test_reused_pid_is_not_our_compositor(self):
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(self.ws.socket)
        self.fake_running(os.getpid(), "12345")            # alive pid, different start time
        self.assertEqual(self.code(), "E_NOT_RUNNING")
        srv.close()

    def test_socket_path_that_is_not_a_socket_or_is_a_symlink(self):
        self.fake_running(os.getpid(), self.manager._proc_start(os.getpid()))
        open(self.ws.socket, "w").close()
        self.assertEqual(self.code(), "E_ROUTING")
        os.unlink(self.ws.socket)
        os.symlink("/nonexistent", self.ws.socket)
        self.assertEqual(self.code(), "E_ROUTING")

    def test_missing_socket(self):
        self.fake_running(os.getpid(), self.manager._proc_start(os.getpid()))
        self.assertEqual(self.code(), "E_NOT_RUNNING")

    def test_foreign_process_behind_the_socket_is_refused_before_any_wayland_byte(self):
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(self.ws.socket)
        srv.listen(2)
        self.fake_running(1, None)                         # claims a compositor pid that is not the listener
        self.ws.compositor_pid = os.getppid()               # alive, but not the process that owns the socket
        self.ws.compositor_start = self.manager._proc_start(os.getppid())
        got = []
        import threading

        def accept():
            c, _ = srv.accept()
            c.settimeout(0.3)
            try:
                got.append(c.recv(10))
            except OSError:
                got.append(b"")
            c.close()
        th = threading.Thread(target=accept, daemon=True)
        th.start()
        with self.assertRaises(self.manager.ManagerError) as cm:
            self.m.session(self.ws)
        self.assertEqual(cm.exception.code, "E_ROUTING")
        th.join(3)
        self.assertEqual(got, [b""])
        srv.close()

    def test_session_constructor_never_reads_the_environment(self):
        from aw import wl
        os.environ["WAYLAND_DISPLAY"] = "wayland-0"
        try:
            for bad in (None, "", "wayland-0", "relative/socket"):
                with self.assertRaises(wl.WaylandError):
                    wl.Connection(bad)
        finally:
            os.environ.pop("WAYLAND_DISPLAY")


def string_constants(path):
    import ast
    tree = ast.parse(open(path).read())
    return [(n.value, n.lineno) for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


class NoHostDisplayInTheCode(unittest.TestCase):
    """Static audit (AST, so comments and docstrings do not count): the only modules that name the host display
    variables as strings are listed here, with the reason."""
    NAMES = {"WAYLAND_DISPLAY", "WAYLAND_SOCKET", "DISPLAY", "XDG_RUNTIME_DIR"}
    ALLOWED = {
        "aw/appenv.py": "builds the app environment from scratch; names them to DROP them or set the workspace's",
        "aw/daemon.py": "workspace.enter: the human's viewer window is opened on the human's display",
        "aw/viewer.py": "the viewer IS a window on the human's desktop",
        "aw/manager.py": "sets the compositor's / apps' own private XDG_RUNTIME_DIR; reads the runtime dir for systemd",
        "aw/paths.py": "XDG_RUNTIME_DIR locates the base directory of the API socket",
        "aw/sandbox.py": "--setenv XDG_RUNTIME_DIR <the workspace's own runtime dir>",
    }

    def test_only_listed_modules_name_display_variables(self):
        hits = {}
        for fn in sorted(os.listdir(os.path.join(ROOT, "aw"))):
            if fn.endswith(".py"):
                found = [v for v, _ in string_constants(os.path.join(ROOT, "aw", fn)) if v in self.NAMES]
                if found:
                    hits["aw/" + fn] = found
        self.assertEqual(set(hits) - set(self.ALLOWED), set(), f"new module touches display variables: {hits}")

    def test_session_wl_xkb_never_look_at_the_environment(self):
        for fn in ("session.py", "wl.py", "xkb.py"):
            text = open(os.path.join(ROOT, "aw", fn)).read()
            self.assertNotIn("os.environ", text, fn)
            self.assertNotIn("getenv", text, fn)

    def test_no_input_injection_tools_anywhere(self):
        bad = {"xdotool", "ydotool", "ydotoold", "wtype", "grim", "dotool", "/dev/uinput", "/dev/input"}
        for dirpath, _, files in os.walk(os.path.join(ROOT, "aw")):
            for f in files:
                if f.endswith(".py"):
                    for v, line in string_constants(os.path.join(dirpath, f)):
                        self.assertFalse(v in bad or v.startswith("/dev/input/"), f"{f}:{line}: {v}")


if __name__ == "__main__":
    unittest.main()
