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
            lst = self.workspaces
            if p.get("agent_kind"):
                lst = [w for w in lst if w["agent"]["kind"] in (p["agent_kind"], "none")]
            return {"workspaces": lst}
        if method == "workspace.create":
            if any(w["name"].lower() == p["name"].lower() for w in self.workspaces):
                raise ApiError("E_EXISTS", "exists")
            w = ws(p["name"], p["agent_kind"])
            self.workspaces.append(w)
            return w
        if method == "workspace.get":
            hit = [w for w in self.workspaces if w["name"].lower() == p["workspace"].lower()]
            if not hit:
                raise ApiError("E_NO_CREDENTIALS", "no credential found")   # no token file yet
            return hit[0]
        if method == "workspace.configure":
            return ws(p["workspace"], "claude-code")
        if method == "workspace.stop":
            return {"ok": True}
        if method == "workspace.start":
            return [w for w in self.workspaces if w["name"] == p["workspace"]][0] | {"state": "running"}
        if method == "window.list":
            return {"windows": []}
        if method == "workspace.launch":
            return {"app": 1, "pid": 42}
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


def server(workspaces, kind="claude-code", default=None, auto=False):
    os.environ["AW_AGENT_KIND"] = kind
    os.environ.pop("AW_WORKSPACE", None)
    s = mcp.Server.__new__(mcp.Server)
    s.kind, s.default_ws, s.client = kind, default, FakeClient(workspaces)
    s.project, s.auto, s.shots_dir, s._ws_cache, s._shot_n = "proj", auto, None, None, 0
    s._prov = s.client            # the fake daemon answers both the agent and the provisioning credential
    return s


class McpTests(unittest.TestCase):
    def rpc(self, s, method, params=None, mid=1):
        return s.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})

    def test_initialize_states_assigned_workspace_rule(self):
        r = self.rpc(server([]), "initialize", {"protocolVersion": "2025-06-18"})["result"]
        self.assertEqual(r["protocolVersion"], "2025-06-18")
        self.assertIn("never the user's personal desktop", r["instructions"])
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
        s._wait_first_window = lambda *a, **k: None
        self.rpc(s, "tools/call", {"name": "launch_app", "arguments": {"command": "gnome-calculator --x; rm -rf /"}})
        argv = [c for c in s.client.calls if c[0] == "workspace.launch"][0][1]["argv"]
        self.assertEqual(argv, ["gnome-calculator", "--x;", "rm", "-rf", "/"])  # plain argv, no shell

    def test_bad_arguments(self):
        s = server([ws("mine", "claude-code")], default="mine")
        r = self.rpc(s, "tools/call", {"name": "mouse_move", "arguments": {}})["result"]
        self.assertTrue(r["isError"])

    # ---- automatic workspace
    def test_auto_name_is_valid_stable_and_bounded(self):
        import re
        rx = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,39}$")
        self.assertEqual(mcp.auto_name("codex", "my app"), "codex-my-app")
        for proj in ("x" * 100, "../../etc", "Ünïcode ✓", "", "a b"):
            n = mcp.auto_name("claude-code", proj)
            self.assertTrue(rx.match(n), n)
            self.assertEqual(n, mcp.auto_name("claude-code", proj))
        self.assertNotEqual(mcp.auto_name("claude-code", "x" * 60), mcp.auto_name("claude-code", "x" * 61))

    def test_first_graphical_call_creates_the_agents_workspace(self):
        s = server([], auto=True)
        r = self.rpc(s, "tools/call", {"name": "screenshot", "arguments": {}})["result"]
        self.assertFalse(r["isError"], r)
        created = [c for c in s.client.calls if c[0] == "workspace.create"]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0][1]["name"], "claude-code-proj")
        self.assertEqual(created[0][1]["agent_kind"], "claude-code")
        self.rpc(s, "tools/call", {"name": "screenshot", "arguments": {}})
        self.assertEqual(len([c for c in s.client.calls if c[0] == "workspace.create"]), 1)  # reused

    def test_a_workspace_the_user_assigned_to_this_agent_is_used_instead_of_creating_one(self):
        s = server([ws("research", "claude-code")], auto=True)
        self.rpc(s, "tools/call", {"name": "screenshot", "arguments": {}})
        self.assertFalse([c for c in s.client.calls if c[0] == "workspace.create"])
        self.assertEqual(s.client.calls[-1][1]["workspace"], "research")

    def test_two_user_workspaces_do_not_hijack_the_automatic_one(self):
        s = server([ws("a", "claude-code"), ws("b", "claude-code")], auto=True)
        self.rpc(s, "tools/call", {"name": "screenshot", "arguments": {}})
        self.assertEqual([c for c in s.client.calls if c[0] == "workspace.create"][0][1]["name"], "claude-code-proj")

    def test_existing_workspace_is_attached_not_duplicated(self):
        s = server([ws("claude-code-proj", "claude-code"), ws("other", "claude-code")], auto=True)
        self.rpc(s, "tools/call", {"name": "screenshot", "arguments": {}})
        self.assertFalse([c for c in s.client.calls if c[0] == "workspace.create"])
        self.assertEqual(s.client.calls[-1][1]["workspace"], "claude-code-proj")

    def test_stopped_workspace_is_started(self):
        w = ws("codex-proj", "codex")
        w["state"] = "stopped"
        s = server([w], kind="codex", auto=True)
        self.rpc(s, "tools/call", {"name": "screenshot", "arguments": {}})
        self.assertIn("workspace.start", [c[0] for c in s.client.calls])

    def test_name_owned_by_another_agent_is_reported(self):
        s = server([ws("claude-code-proj", "codex")], auto=True)
        r = self.rpc(s, "tools/call", {"name": "screenshot", "arguments": {}})["result"]
        self.assertTrue(r["isError"])
        self.assertIn("E_ASSIGNMENT", r["content"][0]["text"])

    def test_auto_create_can_be_disabled(self):
        s = server([], auto=False)
        r = self.rpc(s, "tools/call", {"name": "screenshot", "arguments": {}})["result"]
        self.assertTrue(r["isError"])
        self.assertFalse([c for c in s.client.calls if c[0] == "workspace.create"])

    def test_launch_by_role_uses_installed_app_and_returns_screenshot(self):
        s = server([ws("mine", "claude-code")], default="mine")
        import aw.apps as apps
        apps.list_apps = lambda *a, **k: [{"id": "org.gnome.TextEditor", "name": "Text Editor",
                                           "argv": ["gnome-text-editor"], "categories": ["TextEditor"],
                                           "comment": "", "mime": []}]
        s._wait_first_window = lambda *a, **k: None
        r = self.rpc(s, "tools/call", {"name": "launch_app", "arguments": {"app": "text editor"}})["result"]
        self.assertFalse(r["isError"], r)
        self.assertEqual([c for c in s.client.calls if c[0] == "workspace.launch"][0][1]["argv"], ["gnome-text-editor"])
        self.assertEqual(r["content"][0]["type"], "image")

    def test_window_and_browser_tools_call_the_daemon_methods(self):
        s = server([ws("mine", "claude-code")], default="mine")
        r = self.rpc(s, "tools/call", {"name": "window_move", "arguments": {"window": 1, "x": 1, "y": 2}})["result"]
        self.assertFalse(r["isError"])
        self.assertIn("window.move", [c[0] for c in s.client.calls])
        names = {t["name"] for t in self.rpc(s, "tools/list")["result"]["tools"]}
        for n in ("window_resize", "window_set_geometry", "list_browsers", "launch_browser", "request_browser_profile"):
            self.assertIn(n, names)

    def test_launch_browser_without_a_name_picks_a_launchable_browser(self):
        s = server([ws("mine", "claude-code")], default="mine")
        real = s.client.call
        def call(m, **p):
            if m == "browser.list":
                return {"browsers": [{"id": "chromium", "launchable": False, "reason": "not-installed"},
                                     {"id": "google-chrome", "launchable": True}]}
            if m == "browser.launch":
                s.client.calls.append((m, p))
                return {"browser": p["browser"], "app": 1, "pid": 5, "profile": {"mode": "throwaway", "name": ""}}
            return real(m, **p)
        s.client.call = call
        r = self.rpc(s, "tools/call", {"name": "launch_browser", "arguments": {"url": "https://example.org"}})["result"]
        self.assertFalse(r["isError"], r)
        self.assertEqual([c for c in s.client.calls if c[0] == "browser.launch"][0][1]["browser"], "google-chrome")

    def test_browser_role_goes_through_browser_launch_with_a_throwaway_profile(self):
        s = server([ws("mine", "claude-code")], default="mine")
        import aw.apps as apps
        apps.list_apps = lambda *a, **k: [{"id": "google-chrome", "name": "Google Chrome",
                                           "argv": ["/usr/bin/google-chrome-stable"], "categories": ["WebBrowser"],
                                           "comment": "", "mime": []}]
        s._wait_first_window = lambda *a, **k: None
        real = s.client.call
        def call(m, **p):
            if m == "browser.launch":
                s.client.calls.append((m, p))
                return {"browser": "google-chrome", "app": 1, "pid": 5, "profile": {"mode": "throwaway", "name": ""}}
            return real(m, **p)
        s.client.call = call
        r = self.rpc(s, "tools/call", {"name": "launch_app", "arguments": {"app": "browser", "args": ["https://example.org"]}})["result"]
        self.assertFalse(r["isError"], r)
        bl = [c for c in s.client.calls if c[0] == "browser.launch"][0][1]
        self.assertEqual((bl["profile"], bl["url"], bl["browser"]), ("throwaway", "https://example.org", "google-chrome"))
        self.assertFalse([c for c in s.client.calls if c[0] == "workspace.launch"])

    def test_describe_screen_without_backend_is_honest(self):
        for k in ("AW_VISION_URL", "AW_VISION_MODEL"):
            os.environ.pop(k, None)
        s = server([ws("mine", "claude-code")], default="mine")
        r = self.rpc(s, "tools/call", {"name": "describe_screen", "arguments": {}})["result"]
        self.assertTrue(r["isError"])
        self.assertIn("E_NO_VISION", r["content"][0]["text"])

    def test_agent_cannot_destroy_or_control_another_workspace(self):
        s = server([ws("mine", "claude-code")], default="mine")
        r = self.rpc(s, "tools/call", {"name": "workspace_control", "arguments": {"action": "destroy"}})
        self.assertTrue(r["result"]["isError"] or "error" in r)
        self.assertFalse([c for c in s.client.calls if c[0] == "workspace.destroy"])
        r = self.rpc(s, "tools/call", {"name": "workspace_control",
                                       "arguments": {"action": "stop", "workspace": "someone-else"}})["result"]
        self.assertIn("E_FORBIDDEN", r["content"][0]["text"])

    def test_provisioning_can_be_switched_off(self):
        os.environ["AW_PROVISION"] = "0"
        try:
            s = server([], auto=True)
            r = self.rpc(s, "tools/call", {"name": "screenshot", "arguments": {}})["result"]
            self.assertTrue(r["isError"])
            self.assertIn("E_NEEDS_HUMAN", r["content"][0]["text"])
            self.assertFalse([c for c in s.client.calls if c[0] == "workspace.create"])
        finally:
            del os.environ["AW_PROVISION"]

    def test_missing_admin_credential_asks_the_human(self):
        s = server([], auto=True)
        real = s.client.call
        def call(m, **p):
            if m in ("workspace.list", "workspace.create"):
                raise ApiError("E_NO_CREDENTIALS", "no admin token")
            return real(m, **p)
        s._prov = type("P", (), {"call": staticmethod(call)})()
        r = self.rpc(s, "tools/call", {"name": "screenshot", "arguments": {}})["result"]
        self.assertIn("E_NEEDS_HUMAN", r["content"][0]["text"])
        self.assertIn("agent-workspaces create --name claude-code-proj", r["content"][0]["text"])

    def test_screenshot_file_is_requested_relative_to_the_workspace_shots_dir(self):
        s = server([ws("mine", "claude-code")], default="mine")
        self.rpc(s, "tools/call", {"name": "screenshot", "arguments": {}})
        p = [c for c in s.client.calls if c[0] == "screen.screenshot"][0][1]["path"]
        self.assertFalse(os.path.isabs(p))

    def test_unknown_method(self):
        self.assertEqual(self.rpc(server([]), "bogus")["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
