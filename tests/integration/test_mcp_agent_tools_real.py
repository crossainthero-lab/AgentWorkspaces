"""REAL INTEGRATION TEST of the agent-facing MCP tool surface (no LLM involved): a real daemon, a real headless
compositor, a real GTK probe app that logs every event it receives, and the MCP server over stdio exactly as an
agent would run it. No display variable reaches any process.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as K  # noqa: E402

C = None


def setUpModule():
    global C
    if not K.have("labwc"):
        raise unittest.SkipTest("labwc not installed")
    C = K.start_daemon("mcptools")


def tearDownModule():
    K.stop_daemon(C)


class Mcp:
    def __init__(self, kind, cwd):
        env = {"PATH": os.environ["PATH"], "HOME": os.path.expanduser("~"), "AW_BASE_DIR": K.BASE,
               "AW_AGENT_KIND": kind, "PYTHONPATH": K.ROOT, "LANG": "C.UTF-8"}
        assert "DISPLAY" not in env and "WAYLAND_DISPLAY" not in env
        self.p = subprocess.Popen([sys.executable, os.path.join(K.ROOT, "bin", "agent-workspaces"), "mcp"], env=env,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, cwd=cwd)
        self.n = 0
        self.rpc("initialize", {"protocolVersion": "2025-06-18"})

    def rpc(self, method, params=None):
        self.n += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}}) + "\n")
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())["result"]

    def tool(self, name, **args):
        return self.rpc("tools/call", {"name": name, "arguments": args})

    def text(self, name, **args):
        r = self.tool(name, **args)
        return r["isError"], " ".join(c.get("text", "") for c in r["content"] if c["type"] == "text")

    def close(self):
        self.p.stdin.close()
        self.p.wait(5)


class AgentToolSurface(unittest.TestCase):
    def test_everything_an_agent_needs_in_its_own_workspace(self):
        proj = os.path.join(tempfile.mkdtemp(prefix="awproj"), "demo-proj")
        os.makedirs(proj)
        m = Mcp("codex", proj)
        try:
            names = {t["name"] for t in m.rpc("tools/list")["tools"]}
            for n in ("screenshot", "describe_screen", "mouse_move", "mouse_click", "double_click", "mouse_drag", "scroll",
                      "keyboard_type", "keyboard_key", "launch_app", "list_apps", "list_windows", "window_action",
                      "window_move", "window_resize", "get_clipboard", "set_clipboard", "configure_display",
                      "ensure_workspace", "workspace_status", "request_permission"):
                self.assertIn(n, names)
            # first graphical call creates the agent's own workspace, named after agent + project
            err, txt = m.text("ensure_workspace", width=900, height=600)
            self.assertFalse(err, txt)
            self.assertIn("codex-demo-proj", txt)
            self.assertIn("created", txt)
            wid = [w for w in C.call("workspace.list")["workspaces"] if w["name"] == "codex-demo-proj"][0]
            self.assertEqual(wid["agent"]["kind"], "codex")
            log = os.path.join(K.BASE, "tools.log")
            open(log, "w").close()
            r = m.tool("launch_app", command=["/usr/bin/python3", K.PROBE, "TOOLS", log, "#2b6cb0"])
            self.assertFalse(r["isError"], r)
            self.assertEqual(r["content"][0]["type"], "image")            # screenshot comes with the launch
            self.assertTrue(K.wait_for(lambda: any(e.get("ev") == "ready" for e in K.log_events(log)), 15))
            K.wait_for(lambda: [w for w in C.call("window.list", workspace="codex-demo-proj")["windows"] if w["maximized"]], 10)
            import time
            time.sleep(1.0)
            n0 = len(K.log_events(log))
            self.assertFalse(m.tool("double_click", x=450, y=232)["isError"])
            self.assertTrue(K.wait_for(lambda: len([e for e in K.log_events(log)[n0:] if e["ev"] == "button"]) == 2, 4),
                            "double_click must produce two clicks")
            self.assertFalse(m.tool("mouse_drag", x1=100, y1=300, x2=300, y2=310)["isError"])
            self.assertFalse(m.tool("scroll", dy=3, x=200, y=300)["isError"])
            self.assertTrue(K.wait_for(lambda: any(e["ev"] == "scroll" and e["dy"] > 0 for e in K.log_events(log)), 3))
            self.assertFalse(m.tool("keyboard_key", combo="ctrl+a")["isError"])
            self.assertTrue(K.wait_for(lambda: any(e["ev"] == "key" and e["name"] == "a" and e["state"] & 4
                                                   for e in K.log_events(log)), 5))
            # the screenshot is a real image of that workspace
            r = m.tool("screenshot", max_width=300)
            self.assertEqual(r["content"][0]["type"], "image")
            self.assertIn("300x", r["content"][1]["text"])
            self.assertIn("also saved as", r["content"][1]["text"])
            # the workspace's own clipboard round trip
            self.assertFalse(m.tool("set_clipboard", text="alpha beta")["isError"])
            self.assertEqual(m.text("get_clipboard")[1], "alpha beta")
            # windows
            wins = json.loads(m.text("list_windows")[1])
            self.assertEqual(wins[0]["title"], "probe TOOLS")
            self.assertFalse(m.tool("window_action", window=wins[0]["id"], action="focus")["isError"])
            # window move/resize: works when the daemon has the API, otherwise says so explicitly
            err, txt = m.text("window_move", window=wins[0]["id"], x=10, y=10)
            self.assertTrue(not err or "E_UNSUPPORTED" in txt, txt)
            # display configuration
            err, txt = m.text("configure_display", width=1024, height=640)
            self.assertFalse(err, txt)
            self.assertEqual(json.loads(m.text("display_info")[1])["outputs"][0]["width"], 1024)
            # sensitive permission: the agent can only ASK
            err, txt = m.text("request_permission", kind="host_clipboard", detail="paste a link")
            self.assertFalse(err, txt)
            self.assertIn("pending", txt)
            # an agent cannot destroy or leave its workspace
            err, txt = m.text("workspace_control", action="destroy")
            self.assertTrue(err)
            err, txt = m.text("workspace_control", action="stop", workspace="someone-else")
            self.assertTrue(err)
            self.assertIn("E_FORBIDDEN", txt)
            # a second session of the same agent in the same project re-attaches to the SAME workspace
            m2 = Mcp("codex", proj)
            try:
                err, txt = m2.text("ensure_workspace")
                self.assertFalse(err, txt)
                self.assertIn("attached", txt)
                ids = [w["id"] for w in C.call("workspace.list")["workspaces"] if w["name"] == "codex-demo-proj"]
                self.assertEqual(len(ids), 1)
            finally:
                m2.close()
            # manual takeover reaches the MCP tools
            C.call("workspace.pause", workspace="codex-demo-proj")
            err, txt = m.text("mouse_click", x=10, y=10)
            self.assertTrue(err)
            self.assertIn("E_PAUSED", txt)
        finally:
            m.close()

    def test_a_different_agent_kind_gets_its_own_workspace_in_the_same_project(self):
        proj = os.path.join(tempfile.mkdtemp(prefix="awproj"), "shared-proj")
        os.makedirs(proj)
        a, b = Mcp("claude-code", proj), Mcp("hermes", proj)
        try:
            self.assertFalse(a.text("ensure_workspace")[0])
            self.assertFalse(b.text("ensure_workspace")[0])
            ws = {w["name"]: w["agent"]["kind"] for w in C.call("workspace.list")["workspaces"] if "shared-proj" in w["name"]}
            self.assertEqual(ws, {"claude-code-shared-proj": "claude-code", "hermes-shared-proj": "hermes"})
        finally:
            a.close()
            b.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)


class AgentWindowTools(unittest.TestCase):
    """window_move / window_resize / window_set_geometry through the MCP server, checked on real pixels and on the
    geometry the compositor / X server reports (floating workspace; SA2's window API)."""

    def test_move_resize_and_geometry_are_real(self):
        import base64
        import time
        proj = os.path.join(tempfile.mkdtemp(prefix="awproj"), "winproj")
        os.makedirs(proj)
        C.call("workspace.create", name="codex-winproj", agent_kind="codex", width=1000, height=700,
               window_policy="floating")
        m = Mcp("codex", proj)
        try:
            log = os.path.join(K.BASE, "win.log")
            open(log, "w").close()
            r = m.tool("launch_app", command=["/usr/bin/python3", K.PROBE, "WINBOX", log, "#2b6cb0"], screenshot=False)
            self.assertFalse(r["isError"], r)
            self.assertTrue(K.wait_for(lambda: any(e.get("ev") == "ready" for e in K.log_events(log)), 15))
            time.sleep(1.0)
            wins = json.loads(m.text("list_windows")[1])
            wid = [w for w in wins if w["title"] == "probe WINBOX"][0]["id"]
            err, txt = m.text("window_set_geometry", window=wid, x=300, y=200, width=500, height=300)
            self.assertFalse(err, txt)
            time.sleep(1.0)
            g = json.loads(txt)["geometry"]
            self.assertEqual((g["x"], g["y"], g["width"], g["height"]), (300, 200, 500, 300))

            def pix(x, y):
                r = m.tool("screenshot", region=[x, y, 1, 1])
                import base64 as b64
                png = b64.b64decode([c for c in r["content"] if c["type"] == "image"][0]["data"])
                return K.pixel(png, 0, 0)
            inside = pix(330, 240)          # the blue banner of the probe, inside the new rectangle
            outside = pix(100, 100)
            self.assertTrue(inside[2] > 120 and inside[0] < 90, f"banner not at the requested place: {inside}")
            self.assertFalse(outside[2] > 120 and outside[0] < 90, f"window also covers the old place: {outside}")
            err, txt = m.text("window_move", window=wid, x=500, y=350)
            self.assertFalse(err, txt)
            time.sleep(1.0)
            self.assertTrue(pix(530, 390)[2] > 120)
            self.assertFalse(pix(330, 240)[2] > 120 and pix(330, 240)[0] < 90, "window left a ghost at the old place")
            err, txt = m.text("window_resize", window=wid, width=420, height=260)
            self.assertFalse(err, txt)
            g = json.loads(txt)["geometry"]
            self.assertEqual((g["width"], g["height"]), (420, 260))
        finally:
            m.close()
            C.call("workspace.destroy", workspace="codex-winproj")
