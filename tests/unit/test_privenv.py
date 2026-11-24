"""UNIT tests of the private-environment check every real-agent run must pass (tests/acceptance/privenv.py)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.acceptance import privenv  # noqa: E402


class Private(unittest.TestCase):
    def setUp(self):
        self.p = privenv.PrivateEnv("unit")

    def tearDown(self):
        self.p.close()

    def test_a_private_environment_passes_and_hides_secrets_in_the_plan(self):
        env = self.p.env({"SPARK_LLM_API_KEY": "sekrit-value"})
        plan = "\n".join(privenv.assert_private(env))
        self.assertNotIn("sekrit-value", plan)
        self.assertIn("SPARK_LLM_API_KEY=<redacted>", plan)

    def test_each_kind_of_exposure_aborts(self):
        rh = privenv.real_home()
        rrt = privenv.real_runtime_dir()
        bad = [{"HOME": rh}, {"DISPLAY": ":0"}, {"WAYLAND_DISPLAY": "wayland-0"}, {"DBUS_SESSION_BUS_ADDRESS": f"unix:path={rrt}/bus"},
               {"XDG_RUNTIME_DIR": rrt}, {"XDG_CONFIG_HOME": os.path.join(rh, ".config")}, {"PATH": f"{rh}/.local/bin:/usr/bin"},
               {"XAUTHORITY": f"{rh}/.Xauthority"}, {"SSH_AUTH_SOCK": f"{rrt}/openssh_agent"}, {"FOO": f"{rrt}/wayland-0"}]
        for extra in bad:
            with self.assertRaises(AssertionError, msg=str(extra)):
                privenv.assert_private(self.p.env(extra))

    def test_a_standin_desktops_variables_are_allowed_only_under_the_given_private_root(self):
        env = self.p.env({"WAYLAND_DISPLAY": "wayland-0", "XDG_RUNTIME_DIR": os.path.join(self.p.root, "hostrt")})
        with self.assertRaises(AssertionError):
            privenv.assert_private(env)
        privenv.assert_private(self.p.env({"DISPLAY": ":777", "DBUS_SESSION_BUS_ADDRESS": f"unix:path={self.p.root}/x/bus"}),
                               allowed_roots=[self.p.root], allowed_values={"DISPLAY": ":777"})


if __name__ == "__main__":
    unittest.main()
