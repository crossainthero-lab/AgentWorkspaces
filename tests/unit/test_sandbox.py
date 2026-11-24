"""UNIT tests of the sandbox argument builder and mount validation (no bwrap is run here; the real behaviour is
tested in tests/integration/test_sandbox_real.py)."""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from aw import sandbox  # noqa: E402


class Mounts(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="awsb", dir="/tmp")
        self.home = os.path.join(self.d, "home")
        os.makedirs(os.path.join(self.home, ".ssh"))
        os.makedirs(os.path.join(self.home, "proj"))
        self.base = os.path.join(self.d, "run", "agent-workspaces")
        os.makedirs(self.base)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def v(self, items):
        return sandbox.validate_mounts(sandbox.parse_mounts(items), self.base, os.path.join(self.d, "run"), self.home)

    def test_parse(self):
        self.assertEqual(sandbox.parse_mounts(["/a", "/b:rw", "/c:ro", {"path": "/d", "ro": False}]),
                         [{"path": "/a", "ro": True}, {"path": "/b", "ro": False}, {"path": "/c", "ro": True},
                          {"path": "/d", "ro": False}])

    def test_project_dir_ok_default_readonly(self):
        r = self.v([os.path.join(self.home, "proj")])
        self.assertEqual(r[0]["ro"], True)

    def test_refused(self):
        for bad in ("/", "/run", "/etc", self.home, self.base, os.path.join(self.base, "w1"),
                    os.path.join(self.d, "run"), os.path.join(self.home, ".ssh"), "rel", "/nonexistent-zzz"):
            with self.assertRaises(sandbox.SandboxError, msg=bad):
                self.v([bad])


class Argv(unittest.TestCase):
    def test_shape(self):
        a = sandbox.build_argv("/w", "/w/rt", "/w/home", [{"path": "/p", "ro": True}, {"path": "/q", "ro": False}],
                               ":7", "/p", True)
        j = " ".join(a)
        for must in ("--unshare-pid", "--unshare-ipc", "--new-session", "--die-with-parent", "--cap-drop ALL",
                     "--tmpfs /tmp", "--ro-bind /usr /usr", "--bind /w/home /w/home", "--bind /w/rt /w/rt",
                     "--ro-bind /p /p", "--bind /q /q", "--bind-try /tmp/.X11-unix/X7 /tmp/.X11-unix/X7",
                     "--chdir /p", "--dev-bind-try /dev/dri /dev/dri"):
            self.assertIn(must, j)
        self.assertNotIn("--unshare-net", j)
        self.assertNotIn("/tmp/.X11-unix/X0", j)
        self.assertNotIn("--ro-bind / /", j)

    def test_no_net_and_no_gpu(self):
        j = " ".join(sandbox.build_argv("/w", "/w/rt", "/w/home", [], None, None, False, False))
        self.assertIn("--unshare-net", j)
        self.assertNotIn("/dev/dri", j)

    def test_host_runtime_dir_and_home_are_never_bound(self):
        j = sandbox.build_argv("/w", "/w/rt", "/w/home", [], None)
        self.assertNotIn(os.path.expanduser("~"), j)
        self.assertNotIn(f"/run/user/{os.getuid()}", " ".join(j))


if __name__ == "__main__":
    unittest.main()
