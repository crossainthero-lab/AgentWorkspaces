"""UNIT tests (no compositor needed): the launcher environment is host-free and fails closed."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from aw import appenv  # noqa: E402

HOST = {
    "WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0", "XDG_RUNTIME_DIR": "/run/user/1000",
    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus", "XAUTHORITY": "/run/user/1000/.mutter-Xwaylandauth.X",
    "SSH_AUTH_SOCK": "/run/user/1000/keyring/ssh", "SESSION_MANAGER": "local/x:@/tmp/.ICE-unix/1",
    "PATH": "/usr/bin:/bin", "HOME": "/home/u", "LANG": "en_US.UTF-8", "LC_TIME": "C",
    "ANTHROPIC_API_KEY": "secret-should-not-leak", "SECRET_TOKEN": "x", "GDK_BACKEND": "x11",
    "XDG_SESSION_ID": "3", "PIPEWIRE_REMOTE": "/run/user/1000/pipewire-0",
}


class AppEnvTests(unittest.TestCase):
    def setUp(self):
        self.rt = tempfile.mkdtemp(prefix="awrt")
        open(os.path.join(self.rt, "wayland-0"), "w").close()  # stands in for the workspace socket

    def test_fails_closed_when_socket_missing(self):
        with self.assertRaises(appenv.WorkspaceNotRunning):
            appenv.build_env(tempfile.mkdtemp(), host_env=HOST)

    def test_fails_closed_on_relative_dir(self):
        with self.assertRaises(appenv.WorkspaceNotRunning):
            appenv.build_env("rt", host_env=HOST)

    def test_no_host_display_vars(self):
        env = appenv.build_env(self.rt, host_env=HOST)
        self.assertEqual(env["XDG_RUNTIME_DIR"], self.rt)
        self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-0")
        self.assertNotIn("DISPLAY", env)
        for k in ("XAUTHORITY", "SSH_AUTH_SOCK", "SESSION_MANAGER", "DBUS_SESSION_BUS_ADDRESS",
                  "PIPEWIRE_REMOTE", "XDG_SESSION_ID"):
            self.assertNotIn(k, env, k)

    def test_allowlist_not_blocklist(self):
        env = appenv.build_env(self.rt, host_env=HOST)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("SECRET_TOKEN", env)
        self.assertEqual(env["LANG"], "en_US.UTF-8")
        self.assertEqual(env["LC_TIME"], "C")

    def test_display_and_bus_are_the_workspace_ones(self):
        env = appenv.build_env(self.rt, x_display=":7", bus_address=f"unix:path={self.rt}/bus", host_env=HOST)
        self.assertEqual(env["DISPLAY"], ":7")
        self.assertTrue(env["DBUS_SESSION_BUS_ADDRESS"].startswith(f"unix:path={self.rt}"))

    def test_forbidden_extra_rejected(self):
        for k in ("WAYLAND_DISPLAY", "DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
            with self.assertRaises(ValueError):
                appenv.build_env(self.rt, extra={k: "x"}, host_env=HOST)

    def test_extra_allowed(self):
        env = appenv.build_env(self.rt, extra={"MY_FLAG": "1"}, host_env=HOST)
        self.assertEqual(env["MY_FLAG"], "1")

    def test_assert_no_host_sockets(self):
        env = appenv.build_env(self.rt, host_env=HOST)
        appenv.assert_no_host_sockets(env, "/run/user/1000")
        bad = dict(env, XDG_RUNTIME_DIR="/run/user/1000")
        with self.assertRaises(AssertionError):
            appenv.assert_no_host_sockets(bad, "/run/user/1000")
        with self.assertRaises(AssertionError):
            appenv.assert_no_host_sockets(dict(env, WAYLAND_DISPLAY="/run/user/1000/wayland-0"), "/run/user/1000")


if __name__ == "__main__":
    unittest.main()
