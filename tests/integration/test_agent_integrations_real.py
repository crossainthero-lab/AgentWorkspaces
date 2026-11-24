"""REAL INTEGRATION TESTS of `agent-workspaces integrate ...` against the real agent CLIs
(claude, codex, agy, hermes) that are installed on this machine.

Safety: every call runs with `env -i`, a throw-away HOME, and NO display/browser variables, so the real
agent configuration is never touched and no CLI can open a window or a login page. The test also checks
that the real config files are byte-identical before and after.
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REAL = ["~/.codex/config.toml", "~/.gemini/config/mcp_config.json", "~/.hermes/config.yaml"]


def digest(p):
    p = os.path.expanduser(p)
    return hashlib.sha256(open(p, "rb").read()).hexdigest() if os.path.exists(p) else None


class Integrations(unittest.TestCase):
    def run_cli(self, home, *args):
        env = {"PATH": os.environ["PATH"], "HOME": home, "BROWSER": "/bin/true", "PYTHONPATH": ROOT, "LANG": "C.UTF-8"}
        assert "DISPLAY" not in env and "WAYLAND_DISPLAY" not in env
        return subprocess.run([sys.executable, os.path.join(ROOT, "bin", "agent-workspaces"), "integrate", *args],
                              env=env, capture_output=True, text=True, timeout=90, cwd=home)

    def test_roundtrip_each_installed_agent(self):
        before = {p: digest(p) for p in REAL}
        tried = []
        for agent, exe in (("claude-code", "claude"), ("codex", "codex"), ("antigravity", "agy"), ("hermes", "hermes")):
            if not shutil.which(exe):
                continue
            home = tempfile.mkdtemp(prefix="awint")
            try:
                r = self.run_cli(home, agent, "install", "--workspace", "demo")
                self.assertEqual(r.returncode, 0, f"{agent} install: {r.stdout} {r.stderr}")
                st = self.run_cli(home, agent, "status")
                self.assertIn("is registered", st.stdout, f"{agent}: {st.stdout} {st.stderr}")
                if agent in ("claude-code", "codex"):
                    sk = os.path.join(home, ".claude" if agent == "claude-code" else ".codex", "skills",
                                      "agent-workspaces", "SKILL.md")
                    self.assertTrue(os.path.exists(sk))
                    self.assertIn("never the user's personal desktop", open(sk).read())
                if agent == "antigravity":
                    rf = os.path.join(home, ".gemini", "GEMINI.md")
                    self.assertIn("agent-workspaces:begin", open(rf).read())
                u = self.run_cli(home, agent, "uninstall")
                self.assertEqual(u.returncode, 0, f"{agent} uninstall: {u.stdout} {u.stderr}")
                st = self.run_cli(home, agent, "status")
                self.assertIn("not registered", st.stdout, f"{agent}: {st.stdout} {st.stderr}")
                if agent in ("claude-code", "codex"):
                    self.assertFalse(os.path.exists(sk))
                if agent == "antigravity":
                    self.assertFalse(os.path.exists(rf), "the managed block (the only content) must be removed")
                tried.append(agent)
            finally:
                shutil.rmtree(home, ignore_errors=True)
        self.assertTrue(tried, "no agent CLI installed")
        print("round-tripped:", tried)
        self.assertEqual(before, {p: digest(p) for p in REAL}, "a real agent config file changed")
        # ~/.claude.json is rewritten constantly by running Claude sessions, so check for our entry instead
        cj = os.path.expanduser("~/.claude.json")
        if os.path.exists(cj):
            self.assertNotIn("agent-workspaces", open(cj).read(), "integration leaked into the real Claude config")

    def test_show_prints_the_assigned_workspace_rule(self):
        home = tempfile.mkdtemp(prefix="awint")
        try:
            for agent in ("claude-code", "codex", "antigravity", "hermes"):
                r = self.run_cli(home, agent, "show")
                self.assertIn("never the user's personal desktop", r.stdout)
        finally:
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
