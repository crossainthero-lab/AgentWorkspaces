"""UNIT tests of the MCP server with a fake daemon client."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from aw import mcp  # noqa: E402
from aw.client import ApiError  # noqa: E402


class FakeClient:
    def __init__(self, workspaces):
        self.workspaces = workspaces
        self.calls = []

    def call(self, method, **p):
        self.calls.append((method, p))
        if method == "workspace.list":
            ws = self.workspaces
            if p.get("agent_kind"):
                ws = [w for w in ws if w["agent"]["kind"] in (p["agent_kind"], "none")]
            return {"workspaces": ws}
        if method == "screen.screenshot":
            return {"base64": "AAAA", "width": 10, "height": 5, "capture_ms": 1.0, "workspace": p["workspace"]}
        if method.startswith("input."):
            if p.get("_paused"):
                raise ApiError("E_PAUSED", "manual takeover")
            return {"ok": True, "pointer": {"x": p.get("x"), "y": p.get("y")}}
        return {}


def ws(name, kind):
    return {"name": name, "id": "w" + name, "state": "running", "agent": {"kind": kind, "label": kind},
            "display": {"width": 100, "height": 50}, "input_mode": "agent", "activity": {"window_count": 0}}


def server(workspaces, kind="claude-code", default=None):
    os.environ["AW_AGENT_KIND"] = kind
    os.environ.pop("AW_WORKSPACE", None)
    s = mcp.Server.__new__(mcp.Server)
    s.kind, s.default_ws, s.client = kind, default, FakeClient(workspaces)
    return s


class McpTests(unittest.TestCase):
    def rpc(self, s, method, params=None, mid=1):
        return s.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})

    def test_initialize_states_assigned_workspace_rule(self):
        r = self.rpc(server([]), "initialize", {"protocolVersion": "2025-06-18"})["result"]
        self.assertEqual(r["protocolVersion"], "2025-06-18")
        self.assertIn("never touch the user's personal desktop", r["instructions"])
        self.assertIn("assigned", r["instructions"])

    def test_tool_list_is_complete(self):
        names = {t["name"] for t in self.rpc(server([]), "tools/list")["result"]["tools"]}
        for n in ("screenshot", "list_windows", "launch_app", "mouse_move", "mouse_click", "mouse_drag",
                  "keyboard_type", "keyboard_key", "scroll", "window_action", "display_info", "workspace_status",
                  "list_workspaces"):
            self.assertIn(n, names)

    def test_notifications_get_no_response(self):
        self.assertIsNone(server([]).handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_resolves_the_single_assigned_workspace(self):
        s = server([ws("mine", "claude-code"), ws("theirs", "codex")])
        r = self.rpc(s, "tools/call", {"name": "screenshot", "arguments": {}})["result"]
        self.assertFalse(r["isError"])
        self.assertEqual(r["content"][0]["type"], "image")
        self.assertEqual(s.client.calls[-1][1]["workspace"], "mine")
        self.assertEqual(s.client.calls[-1][1]["agent_kind"], "claude-code")

    def test_ambiguous_without_assignment_is_an_error_not_a_guess(self):
        s = server([ws("a", "claude-code"), ws("b", "claude-code")])
        r = self.rpc(s, "tools/call", {"name": "screenshot", "arguments": {}})["result"]
        self.assertTrue(r["isError"])
        self.assertIn("E_AMBIGUOUS", r["content"][0]["text"])
        self.assertFalse([c for c in s.client.calls if c[0] == "screen.screenshot"])

    def test_no_workspaces_tells_the_agent_what_to_ask(self):
        r = self.rpc(server([]), "tools/call", {"name": "list_workspaces", "arguments": {}})["result"]
        self.assertIn("Ask the user", r["content"][0]["text"])

    def test_api_error_is_reported_as_tool_error(self):
        s = server([ws("mine", "claude-code")], default="mine")
        s.client.call = lambda m, **p: (_ for _ in ()).throw(ApiError("E_PAUSED", "manual takeover"))
        r = self.rpc(s, "tools/call", {"name": "mouse_click", "arguments": {"x": 1, "y": 2}})["result"]
        self.assertTrue(r["isError"])
        self.assertIn("E_PAUSED", r["content"][0]["text"])

    def test_launch_app_string_is_split_without_shell(self):
        s = server([ws("mine", "claude-code")], default="mine")
        self.rpc(s, "tools/call", {"name": "launch_app", "arguments": {"command": "gnome-calculator --x; rm -rf /"}})
        argv = [c for c in s.client.calls if c[0] == "workspace.launch"][0][1]["argv"]
        self.assertEqual(argv, ["gnome-calculator", "--x;", "rm", "-rf", "/"])  # plain argv, no shell

    def test_bad_arguments(self):
        s = server([ws("mine", "claude-code")], default="mine")
        r = self.rpc(s, "tools/call", {"name": "mouse_move", "arguments": {}})["result"]
        self.assertTrue(r["isError"])

    def test_unknown_method(self):
        self.assertEqual(self.rpc(server([]), "bogus")["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
