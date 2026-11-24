"""Unit tests: MCP tool definitions for windows, browsers and audio (aw/mcp_ext.py) with a fake daemon client."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from aw import mcp_ext  # noqa: E402


class FakeClient:
    def __init__(self):
        self.calls = []

    def call(self, method, **p):
        self.calls.append((method, p))
        if method.startswith("window."):
            return {"ok": True, "window": {"id": p["window"], "title": "t", "kind": "x11",
                                           "geometry": {"x": 1, "y": 2, "width": 3, "height": 4},
                                           "geometry_source": "x11", "maximized": False, "focused": True}}
        if method == "browser.launch":
            return {"app": 3, "pid": 99, "browser": p["browser"], "profile": {"mode": "throwaway", "name": ""}}
        if method == "browser.profile.request":
            return {"id": "g1", "status": "pending", "how": "the user must run: agent-workspaces profile authorize ..."}
        if method == "audio.record":
            return {"path": "/x.wav", "seconds": 1.0, "peak": 0.1, "rms_dbfs": -20, "tone_hz": 440}
        return {"mode": "private"}


class McpExt(unittest.TestCase):
    def test_tool_schemas(self):
        names = [t["name"] for t in mcp_ext.TOOLS]
        self.assertEqual(len(names), len(set(names)))
        for t in mcp_ext.TOOLS:
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertFalse(t["inputSchema"]["additionalProperties"])
            for req in t["inputSchema"]["required"]:
                self.assertIn(req, t["inputSchema"]["properties"])
        # there is no tool that grants anything, and none takes a filesystem path of a browser profile
        self.assertNotIn("authorize_browser_profile", names)
        for t in mcp_ext.TOOLS:
            self.assertNotIn("user_data_dir", t["inputSchema"]["properties"])

    def test_calls_reach_the_right_daemon_methods(self):
        c = FakeClient()
        out = mcp_ext.call(c, "window_move", {"window": 5, "x": 10, "y": 20}, "ws")
        self.assertEqual(c.calls[-1], ("window.move", {"workspace": "ws", "window": 5, "x": 10, "y": 20}))
        self.assertEqual(json.loads(out[0]["text"])["geometry"]["width"], 3)
        mcp_ext.call(c, "window_resize", {"window": 5, "width": 300, "height": 200}, "ws")
        self.assertEqual(c.calls[-1][0], "window.resize")
        mcp_ext.call(c, "window_set_geometry", {"window": 5, "x": 1, "y": 2, "width": 3, "height": 4}, "ws")
        self.assertEqual(c.calls[-1][0], "window.set_geometry")
        out = mcp_ext.call(c, "launch_browser", {"browser": "google-chrome", "url": "https://x", "args": []}, "ws")
        self.assertEqual(c.calls[-1][0], "browser.launch")
        self.assertIn("throwaway", out[0]["text"])
        out = mcp_ext.call(c, "request_browser_profile", {"browser": "firefox", "profile": "p"}, "ws")
        self.assertEqual(c.calls[-1][0], "browser.profile.request")
        self.assertIn("the user decides", out[0]["text"])
        mcp_ext.call(c, "audio_record", {"seconds": 1}, "ws")
        self.assertEqual(c.calls[-1][1], {"workspace": "ws", "seconds": 1})
        mcp_ext.call(c, "audio_info", {}, "ws")
        mcp_ext.call(c, "list_browsers", {}, "ws")
        with self.assertRaises(KeyError):
            mcp_ext.call(c, "nope", {}, "ws")


if __name__ == "__main__":
    unittest.main()
