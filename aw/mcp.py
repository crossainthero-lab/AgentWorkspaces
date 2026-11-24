"""MCP server (stdio, JSON-RPC 2.0) for graphical computer use inside an ASSIGNED AgentWorkspace.

Configuration (environment of the MCP server process, set by `agent-workspaces integrate`):
  AW_AGENT_KIND   claude-code | codex | antigravity | hermes | other   (who is calling)
  AW_WORKSPACE    optional default workspace name
Every tool targets an AgentWorkspace, never the personal desktop: the server has no code path to
the host display (it only talks to the daemon's unix socket).
"""
from __future__ import annotations

import json
import os
import shlex
import sys
from typing import Any, Dict, List, Optional

from . import __version__
from .client import ApiError, Client

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = (
    "These tools operate a graphical computer inside an AgentWorkspace: a separate headless desktop that was "
    "assigned to you. They never touch the user's personal desktop, mouse, keyboard focus or windows. "
    "Always use these tools (not xdotool, ydotool, wtype, grim against the host, or any other computer-use "
    "tool) for graphical interaction. First call list_workspaces; use the workspace assigned to you. "
    "Coordinates are pixels of that workspace's screen, origin top-left; take a screenshot before clicking. "
    "If a call fails with E_PAUSED the human has taken over the workspace: stop sending input, wait, and ask "
    "or poll workspace_status until input_mode is 'agent'."
)

WS = {"type": "string", "description": "Workspace name or id. Optional when exactly one workspace is "
      "assigned to you or AW_WORKSPACE is set."}


def _t(name, desc, props=None, required=None):
    return {"name": name, "description": desc,
            "inputSchema": {"type": "object", "properties": {"workspace": WS, **(props or {})},
                            "required": required or [], "additionalProperties": False}}


NUM = {"type": "number"}
TOOLS = [
    {"name": "list_workspaces", "description": "List AgentWorkspaces available to you (name, state, assigned "
     "agent, resolution, input mode, windows).", "inputSchema": {"type": "object", "properties": {},
                                                                 "additionalProperties": False}},
    _t("workspace_status", "State, input mode (agent/manual), windows, apps, resource usage of a workspace."),
    _t("screenshot", "Capture the workspace screen as an image. Coordinates for clicks are in workspace pixels.",
       {"region": {"type": "array", "items": NUM, "minItems": 4, "maxItems": 4,
                   "description": "[x, y, width, height] crop"},
        "max_width": {"type": "integer", "description": "Downscale the image to this width (coordinates "
                      "remain workspace pixels)."},
        "cursor": {"type": "boolean"}}),
    _t("display_info", "Output size, scale and the last pointer position of the workspace."),
    _t("list_windows", "List windows in the workspace: id, title, app id, focused/maximized/minimized."),
    _t("window_action", "Focus, close, maximize, unmaximize, minimize, unminimize, fullscreen or unfullscreen "
       "a window.", {"window": {"type": "integer"}, "action": {"type": "string", "enum": [
           "focus", "close", "maximize", "unmaximize", "minimize", "unminimize", "fullscreen",
           "unfullscreen"]}}, ["window", "action"]),
    _t("launch_app", "Start a program inside the workspace. 'command' is an argv array "
       "(e.g. [\"gnome-text-editor\"]) or a shell-style string.", {
           "command": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}]},
           "cwd": {"type": "string"}}, ["command"]),
    _t("mouse_move", "Move the workspace pointer.", {"x": NUM, "y": NUM}, ["x", "y"]),
    _t("mouse_click", "Click in the workspace (moves the pointer first when x,y are given).", {
        "x": NUM, "y": NUM, "button": {"type": "string", "enum": ["left", "right", "middle"]},
        "count": {"type": "integer", "description": "2 = double click"}}),
    _t("mouse_drag", "Press at (x1,y1), drag to (x2,y2), release.",
       {"x1": NUM, "y1": NUM, "x2": NUM, "y2": NUM, "button": {"type": "string"}},
       ["x1", "y1", "x2", "y2"]),
    _t("scroll", "Scroll with the wheel. dy>0 scrolls down. Optionally move the pointer to x,y first.",
       {"dy": NUM, "dx": NUM, "x": NUM, "y": NUM}, ["dy"]),
    _t("keyboard_type", "Type text into the focused window of the workspace.", {"text": {"type": "string"}},
       ["text"]),
    _t("keyboard_key", "Press a key or chord, e.g. 'Return', 'ctrl+a', 'alt+F4', 'ctrl+shift+t'.",
       {"combo": {"type": "string"}}, ["combo"]),
    _t("get_clipboard", "Read the workspace's own clipboard text (not the user's clipboard)."),
    _t("set_clipboard", "Set the workspace's own clipboard text.", {"text": {"type": "string"}}, ["text"]),
    _t("wait", "Wait for the UI to settle.", {"seconds": {"type": "number", "minimum": 0, "maximum": 30}},
       ["seconds"]),
    _t("request_human_input", "Ask the human (shown in their notch/panel) to approve or provide something for "
       "this workspace. Returns an id; poll with workspace_status.", {
           "title": {"type": "string"}, "detail": {"type": "string"}}, ["title"]),
]


class Server:
    def __init__(self, default_workspace: Optional[str] = None):
        self.kind = os.environ.get("AW_AGENT_KIND") or None
        self.default_ws = default_workspace or os.environ.get("AW_WORKSPACE") or None
        self.client = Client(role="agent", workspace=self.default_ws)

    # ---- workspace resolution
    def _resolve(self, args: Dict[str, Any]) -> str:
        ref = args.get("workspace") or self.default_ws
        if ref:
            return ref
        res = self.client.call("workspace.list", **({"agent_kind": self.kind} if self.kind else {}))
        mine = [w for w in res["workspaces"] if self.kind and w["agent"]["kind"] == self.kind]
        pool = mine or ([w for w in res["workspaces"] if w["agent"]["kind"] == "none"]
                        if len(res["workspaces"]) == 1 else [])
        if len(pool) == 1:
            return pool[0]["name"]
        names = [w["name"] for w in res["workspaces"]]
        raise ApiError("E_AMBIGUOUS", "no single workspace is assigned to you; pass 'workspace'. "
                       f"Available: {names or 'none (ask the user to create one)'}")

    def _ck(self) -> Dict[str, Any]:
        return {"agent_kind": self.kind} if self.kind else {}

    def call_tool(self, name: str, a: Dict[str, Any]) -> List[Dict[str, Any]]:
        spec = next((t for t in TOOLS if t["name"] == name), None)
        if spec is None:
            raise ApiError("E_UNKNOWN_TOOL", f"unknown tool {name}")
        missing = [k for k in spec["inputSchema"].get("required", []) if k not in a]
        if missing:
            raise ApiError("E_BAD_ARGS", f"missing required argument(s): {', '.join(missing)}")
        c = self.client
        if name == "list_workspaces":
            res = c.call("workspace.list", **self._ck())
            lines = []
            for w in res["workspaces"]:
                d = w["display"]
                lines.append(f"- {w['name']}: {w['state']}, {d['width']}x{d['height']}, agent={w['agent']['label']}, "
                             f"input={w['input_mode']}, windows={w['activity']['window_count']}")
            return [{"type": "text", "text": "\n".join(lines) or "No workspaces. Ask the user to create one "
                     "(agent-workspaces create --name NAME --agent KIND)."}]
        if name == "wait":
            import time
            time.sleep(min(max(float(a["seconds"]), 0), 30))
            return [{"type": "text", "text": "waited"}]
        ws = self._resolve(a)
        ck = self._ck()
        if name == "workspace_status":
            return [{"type": "text", "text": json.dumps(c.call("workspace.get", workspace=ws), indent=2)}]
        if name == "screenshot":
            kw: Dict[str, Any] = dict(ck)
            if a.get("region"):
                kw["region"] = a["region"]
            if a.get("max_width"):
                kw["max_width"] = int(a["max_width"])
            r = c.call("screen.screenshot", workspace=ws, cursor=bool(a.get("cursor")), inline=True, **kw)
            return [{"type": "image", "data": r["base64"], "mimeType": "image/png"},
                    {"type": "text", "text": f"workspace '{r['workspace']}' {r['width']}x{r['height']} px "
                     f"(captured in {r['capture_ms']} ms)"}]
        if name == "display_info":
            return [{"type": "text", "text": json.dumps(c.call("screen.info", workspace=ws, **ck), indent=2)}]
        if name == "list_windows":
            return [{"type": "text", "text": json.dumps(c.call("window.list", workspace=ws, **ck)["windows"],
                                                        indent=2)}]
        if name == "window_action":
            c.call("window.action", workspace=ws, window=int(a["window"]), action=a["action"], **ck)
            return [{"type": "text", "text": "ok"}]
        if name == "launch_app":
            cmd = a["command"]
            argv = shlex.split(cmd) if isinstance(cmd, str) else list(cmd)
            r = c.call("workspace.launch", workspace=ws, argv=argv, cwd=a.get("cwd"), **ck)
            return [{"type": "text", "text": f"launched app {r['app']} (pid {r['pid']}) in workspace {ws}"}]
        method = {"mouse_move": "input.move", "mouse_click": "input.click", "mouse_drag": "input.drag",
                  "scroll": "input.scroll", "keyboard_type": "input.type", "keyboard_key": "input.key"}.get(name)
        if method:
            p = {k: v for k, v in a.items() if k != "workspace"}
            r = c.call(method, workspace=ws, **p, **ck)
            ptr = r.get("pointer", {})
            return [{"type": "text", "text": f"ok (pointer at {ptr.get('x')},{ptr.get('y')})"}]
        if name == "get_clipboard":
            return [{"type": "text", "text": c.call("clipboard.get", workspace=ws, **ck)["text"] or ""}]
        if name == "set_clipboard":
            c.call("clipboard.set", workspace=ws, text=a["text"], **ck)
            return [{"type": "text", "text": "ok"}]
        if name == "request_human_input":
            r = c.call("approval.request", workspace=ws, title=a["title"], detail=a.get("detail", ""), **ck)
            return [{"type": "text", "text": f"request {r['id']} sent to the user (status: {r['status']})"}]
        raise ApiError("E_UNKNOWN_TOOL", f"unknown tool {name}")

    # ---- JSON-RPC
    def handle(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if method == "initialize":
            want = params.get("protocolVersion")
            ver = want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": ver, "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "agent-workspaces", "version": __version__},
                "instructions": INSTRUCTIONS}}
        if method in ("notifications/initialized", "notifications/cancelled") or (
                method and method.startswith("notifications/")):
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": mid, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            try:
                content = self.call_tool(name, args)
                return {"jsonrpc": "2.0", "id": mid, "result": {"content": content, "isError": False}}
            except ApiError as e:
                return {"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": f"{e.code}: {e.message}"}], "isError": True}}
            except (KeyError, TypeError, ValueError) as e:
                return {"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": f"bad arguments: {e!r}"}], "isError": True}}
        if mid is not None:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
        return None


def run(default_workspace: Optional[str] = None) -> int:
    srv = Server(default_workspace)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        resp = srv.handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(run())
