"""UNIT tests: the guard install/uninstall edits are minimal, idempotent and byte-restorable (throw-away config dirs)."""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from aw import guard_install as gi  # noqa: E402

SETTINGS = {"env": {"TMPDIR": "/x/tmp"}, "permissions": {"allow": ["Bash(git *)"]}, "model": "opus",
            "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "/usr/bin/other-hook"}]}],
                      "Stop": [{"hooks": [{"type": "command", "command": "x"}]}]}}
HERMES_YAML = "# my config\nmodel:\n  default: m  # comment kept\nmcp_servers:\n  a:\n    command: x\n"


class ClaudeGuard(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.environ["CLAUDE_CONFIG_DIR"] = self.d
        os.environ["AW_HOST_RUNTIME_DIR"] = self.d
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.d, "xdgconf")
        self.p = os.path.join(self.d, "settings.json")
        with open(self.p, "w") as f:
            f.write(json.dumps(SETTINGS, indent=2) + "\n")
        self.orig = open(self.p, "rb").read()

    def tearDown(self):
        os.environ.pop("CLAUDE_CONFIG_DIR")
        os.environ.pop("AW_HOST_RUNTIME_DIR")
        os.environ.pop("XDG_CONFIG_HOME")
        shutil.rmtree(self.d, ignore_errors=True)

    def test_install_is_minimal_and_uninstall_restores_the_exact_bytes(self):
        gi.claude_install()
        d = json.load(open(self.p))
        self.assertEqual(d["permissions"]["allow"], SETTINGS["permissions"]["allow"])
        self.assertEqual(d["permissions"]["deny"], ["mcp__claude-in-chrome"])      # the host-browser MCP tools are denied
        self.assertEqual(d["env"]["TMPDIR"], "/x/tmp")
        self.assertEqual(d["env"]["DISPLAY"], "")
        self.assertEqual(d["env"]["WAYLAND_DISPLAY"], "aw-guard-no-wayland-display")
        pre = d["hooks"]["PreToolUse"]
        self.assertEqual(pre[0], SETTINGS["hooks"]["PreToolUse"][0])           # the user's own hook is untouched, first
        self.assertEqual(len(pre), 2)
        self.assertTrue(gi.claude_status()["on"])
        gi.claude_uninstall()
        self.assertEqual(open(self.p, "rb").read(), self.orig)
        self.assertFalse(gi.claude_status()["on"])

    def test_idempotent(self):
        gi.claude_install()
        once = open(self.p, "rb").read()
        gi.claude_install()
        self.assertEqual(open(self.p, "rb").read(), once)

    def test_missing_settings_file_is_created_and_removed_again(self):
        os.remove(self.p)
        gi.claude_install()
        self.assertTrue(gi.claude_status()["on"])
        gi.claude_uninstall()
        self.assertEqual(json.load(open(self.p)), {})

    def test_a_conflicting_user_value_is_not_overwritten(self):
        d = dict(SETTINGS)
        d["env"] = {"DISPLAY": ":7"}
        open(self.p, "w").write(json.dumps(d, indent=2) + "\n")
        msgs = gi.claude_install()
        self.assertEqual(json.load(open(self.p))["env"]["DISPLAY"], ":7")
        self.assertTrue(any("DISPLAY" in m for m in msgs))
        gi.claude_uninstall()
        self.assertEqual(json.load(open(self.p))["env"], {"DISPLAY": ":7"})


class HermesGuard(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.environ["HERMES_HOME"] = self.d
        os.environ["AW_HOST_RUNTIME_DIR"] = self.d
        self.p = os.path.join(self.d, "config.yaml")
        open(self.p, "w").write(HERMES_YAML)

    def tearDown(self):
        os.environ.pop("HERMES_HOME")
        os.environ.pop("AW_HOST_RUNTIME_DIR")
        shutil.rmtree(self.d, ignore_errors=True)

    def test_roundtrip_bytes_and_consent(self):
        gi.hermes_install()
        txt = open(self.p).read()
        self.assertTrue(txt.startswith(HERMES_YAML))
        self.assertIn("fail_closed: true", txt)
        self.assertIn("pre_tool_call", txt)
        self.assertTrue(gi.hermes_status()["on"])
        a = json.load(open(os.path.join(self.d, "shell-hooks-allowlist.json")))["approvals"]
        self.assertEqual(a[0]["event"], "pre_tool_call")
        gi.hermes_uninstall()
        self.assertEqual(open(self.p).read(), HERMES_YAML)
        self.assertFalse(os.path.exists(os.path.join(self.d, "shell-hooks-allowlist.json")))

    def test_existing_hooks_section_is_not_touched(self):
        open(self.p, "w").write(HERMES_YAML + "hooks:\n  pre_llm_call: []\n")
        before = open(self.p).read()
        msgs = gi.hermes_install()
        self.assertEqual(open(self.p).read(), before)
        self.assertIn("NOT added", msgs[0])

    def test_hermes_accepts_the_block(self):
        import yaml
        gi.hermes_install()
        cfg = yaml.safe_load(open(self.p))
        h = cfg["hooks"]["pre_tool_call"][0]
        self.assertEqual(h["matcher"], "terminal|execute_code|write_file|patch|browser_.*|computer_use")
        self.assertTrue(h["fail_closed"])
        self.assertEqual(cfg["model"]["default"], "m")


if __name__ == "__main__":
    unittest.main()
