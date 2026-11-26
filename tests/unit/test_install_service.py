"""install.sh --enable-service / uninstall.sh against a throw-away HOME and a FAKE systemctl: no real systemd, no real
user unit is touched. The fake records its arguments."""
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class InstallService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="awinst-")
        self.home = os.path.join(self.tmp, "home")
        self.bin = os.path.join(self.tmp, "fakebin")
        os.makedirs(self.home)
        os.makedirs(self.bin)
        self.log = os.path.join(self.tmp, "calls.log")
        for name in ("systemctl", "labwc", "systemd-run"):
            p = os.path.join(self.bin, name)
            with open(p, "w") as f:
                f.write(f'#!/bin/sh\necho "{name} $*" >> "{self.log}"\nexit 0\n')
            os.chmod(p, 0o755)
        self.env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": self.home, "XDG_CONFIG_HOME": os.path.join(self.home, ".config"),
                    "XDG_DATA_HOME": os.path.join(self.home, ".local", "share"), "LANG": "C.UTF-8"}
        self.prefix = os.path.join(self.tmp, "prefix")
        self.unit = os.path.join(self.home, ".config", "systemd", "user", "agent-workspaces.service")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_sh(self, script, *args):
        return subprocess.run(["bash", os.path.join(ROOT, script), "--prefix", self.prefix, "--bin-dir", os.path.join(self.home, "bin"), *args],
                              env=self.env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)

    def calls(self):
        return open(self.log).read() if os.path.exists(self.log) else ""

    def test_default_installs_the_unit_but_does_not_enable_it(self):
        r = self.run_sh("install.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(self.unit))
        self.assertNotIn("enable", self.calls())
        self.assertIn("--enable-service", r.stdout)           # tells the person how

    def test_enable_service_enables_and_starts_it(self):
        r = self.run_sh("install.sh", "--enable-service")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("systemctl --user enable --now agent-workspaces.service", self.calls())

    def test_uninstall_disables_it_and_removes_the_unit(self):
        self.run_sh("install.sh", "--enable-service")
        open(self.log, "w").close()
        r = self.run_sh("uninstall.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("systemctl --user disable --now agent-workspaces.service", self.calls())
        self.assertFalse(os.path.exists(self.unit))
        self.assertFalse(os.path.exists(self.prefix))


if __name__ == "__main__":
    unittest.main()
