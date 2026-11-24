"""UNIT test: every API method the daemon implements, and every method any client in this repository calls
(CLI, viewer, MCP server, GNOME extension), has an entry in the authorisation table. A method without an entry is
denied to every non-admin credential, so a missing entry is either a security gap (nobody reviewed it) or a dead
feature for agents."""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

PRE_AUTH = set()      # `hello` and `auth.login` are in the table with target "open" (no credential needed)
# string literals that look like methods but are event names or file names
NOT_METHODS = {
    "viewer.json", "viewer.log", "audit.log", "audio.log", "browser.clone_profile",
}
CALLER_FILES = ("aw/daemon.py", "aw/browser_api.py", "aw/cli.py", "aw/cli_browsers.py", "aw/viewer.py", "aw/mcp.py",
                "aw/mcp_ext.py", "aw/manager.py", "gnome-extension/extension.js")


def read(rel):
    with open(os.path.join(ROOT, rel)) as f:
        return f.read()


class PolicyCoverage(unittest.TestCase):
    def setUp(self):
        from aw import auth, daemon  # noqa: F401  (importing the daemon merges the extension tables)
        self.policy = auth.POLICY

    def test_every_dispatched_method_has_a_policy_entry(self):
        missing = set()
        for rel in ("aw/daemon.py", "aw/browser_api.py"):
            text = read(rel)
            # only look at dispatch conditions: `method == "x"` and `method in ("x", "y")`
            names = set(re.findall(r'method\s*==\s*"([a-z_.]+)"', text))
            for group in re.findall(r'method\s+in\s+\(([^)]*)\)', text):
                names |= set(re.findall(r'"([a-z_.]+)"', group))
            for n in names:
                if n not in self.policy and n not in PRE_AUTH:
                    missing.add((rel, n))
        self.assertEqual(missing, set(), "daemon methods without an auth POLICY entry")

    def test_every_method_called_by_a_client_has_a_policy_entry(self):
        missing = set()
        call = re.compile(r'(?:\.call|\.rpc|_prov_call|_daemonCall|Call)\(\s*["\']([a-z_]+\.[a-z_.]+)["\']')
        for rel in CALLER_FILES:
            for n in call.findall(read(rel)):
                if n not in self.policy and n not in PRE_AUTH and n not in NOT_METHODS:
                    missing.add((rel, n))
        self.assertEqual(missing, set(), "client calls to methods without an auth POLICY entry")

    def test_policy_entries_are_well_formed_and_implemented(self):
        targets = {"open", "any", "list", "ws", "wsadmin", "global"}
        implemented = set()
        for rel in ("aw/daemon.py", "aw/browser_api.py"):
            text = read(rel)
            implemented |= set(re.findall(r'method\s*==\s*"([a-z_.]+)"', text))
            for group in re.findall(r'method\s+in\s+\(([^)]*)\)', text):
                implemented |= set(re.findall(r'"([a-z_.]+)"', group))
        for m, (target, scope) in self.policy.items():
            self.assertIn(target, targets, m)
            if target in ("ws", "list"):
                self.assertIsNotNone(scope, f"{m}: workspace-scoped method without a scope")
            else:
                self.assertIsNone(scope, f"{m}: method without a scope target must not name a scope")
            self.assertIn(m, implemented, f"{m} has a policy entry but the daemon does not implement it")


if __name__ == "__main__":
    unittest.main()
