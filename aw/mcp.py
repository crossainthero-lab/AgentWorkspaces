"""MCP server (stdio, JSON-RPC 2.0) for graphical computer use inside the agent's OWN AgentWorkspace.

Configuration (environment of the MCP server process, set by `agent-workspaces integrate`):
  AW_AGENT_KIND    claude-code | codex | antigravity | hermes | other   (who is calling)
  AW_WORKSPACE     optional: always use this workspace
  AW_PROJECT       optional project name for the automatic workspace name (default: basename of the cwd)
  AW_AUTO_CREATE   "0" disables create-on-first-use (then an assigned workspace must already exist)
  AW_APP_DENY      comma list of substrings: apps whose id/name/command matches are not offered
  AW_VISION_URL / AW_VISION_MODEL / AW_VISION_KEY_ENV   backend for describe_screen (see aw/vision.py)
Every tool targets an AgentWorkspace, never the personal desktop: the server has no code path to the
host display (it only talks to the daemon through aw.client, which also carries any credentials).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
import time
from typing import Any, Dict, List, Optional

from . import __version__, apps as appsmod, paths, vision
from .client import ApiError, Client

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = (
    "AgentWorkspaces gives you your OWN graphical computer: a separate headless desktop assigned to you. It "
    "is never the user's personal desktop, mouse, keyboard focus or windows. Whenever the user asks for "
    "anything that needs a graphical app or a screen (open a browser or a website, open/edit/save in a text "
    "editor, use the file manager, take or inspect a screenshot of an app, click or type in a window, switch "
    "between apps), do it with these tools. The user does not have to create a workspace, give a command or "
    "mention AgentWorkspaces: your workspace is created or re-attached automatically on your first graphical "
    "tool call. Typical loop: launch_app (e.g. app='browser', 'text editor', 'file manager') -> screenshot -> "
    "act with mouse_click / keyboard_type / keyboard_key / scroll / mouse_drag -> screenshot to verify. "
    "Coordinates are pixels of that screen, origin top-left; take a screenshot before clicking. Never use "
    "xdotool, ydotool, wtype, grim, gnome-screenshot, xdg-open or any other computer-use tool or GUI launch "
    "against the host session, and never change DISPLAY / WAYLAND_DISPLAY. If a call fails with E_PAUSED the "
    "human has taken over the workspace: stop sending input, wait, then poll workspace_status until "
    "input_mode is 'agent'. If your model cannot see images, use describe_screen instead of screenshot."
)

WS = {"type": "string", "description": "Workspace name or id. Leave out: your own workspace is used."}


def _t(name, desc, props=None, required=None, ws=True):
    p = ({"workspace": WS} if ws else {})
    p.update(props or {})
    return {"name": name, "description": desc,
            "inputSchema": {"type": "object", "properties": p, "required": required or [],
                            "additionalProperties": False}}


NUM = {"type": "number"}
BTN = {"type": "string", "enum": ["left", "right", "middle"]}
TOOLS = [
    _t("list_workspaces", "List the AgentWorkspaces you may use (name, state, resolution, input mode, windows) "
       "and the name your own workspace gets.", ws=False),
    _t("ensure_workspace", "Create your own graphical workspace (a separate headless desktop) or re-attach to it "
       "and make sure it is running. Called automatically by every other graphical tool, so you normally never "
       "need it; use it to choose a size.", {"width": {"type": "integer"}, "height": {"type": "integer"},
                                              "name": {"type": "string"}}, ws=False),
    _t("workspace_status", "State, input mode (agent/manual), windows, apps, CPU/memory use of your workspace."),
    _t("workspace_control", "Start, stop or restart your own workspace ('stop' and 'restart' close every app in "
       "it). Destroying a workspace is not possible for an agent: ask the user.",
       {"action": {"type": "string", "enum": ["start", "stop", "restart"]}}, ["action"]),
    _t("configure_display", "Change the workspace screen size or scale (applies live, apps keep running).",
       {"width": {"type": "integer"}, "height": {"type": "integer"}, "scale": NUM}),
    _t("display_info", "Output size, scale and the last pointer position of the workspace."),
    _t("screenshot", "SEE the screen: capture your workspace as an image. Call this before clicking and after "
       "every action to verify it. Coordinates for clicks are pixels of this image (unless you used max_width).",
       {"region": {"type": "array", "items": NUM, "minItems": 4, "maxItems": 4,
                   "description": "[x, y, width, height] crop"},
        "max_width": {"type": "integer", "description": "Downscale the image to this width."},
        "cursor": {"type": "boolean", "description": "Draw the pointer."}}),
    _t("describe_screen", "Describe the screen in TEXT with a separate vision model, for agents whose own model "
       "cannot see images (needs a configured vision backend; says so when it has none).",
       {"question": {"type": "string", "description": "What to look for."}}),
    _t("list_apps", "List graphical applications that can be launched in the workspace (browser, text editor, "
       "file manager, calculator, ...) with the id to pass to launch_app."),
    _t("launch_app", "Start a graphical program inside your workspace and return a screenshot once it appeared. "
       "Pass 'app' (a name or role such as 'browser', 'text editor', 'file manager', 'calculator', or an id "
       "from list_apps) or 'command' (argv array or shell-style string, e.g. [\"firefox\", \"https://agdc.dev\"]). "
       "Extra 'args' are appended (e.g. a URL for a browser, a path for the editor).",
       {"app": {"type": "string"}, "args": {"type": "array", "items": {"type": "string"}},
        "command": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}]},
        "cwd": {"type": "string"}, "screenshot": {"type": "boolean", "description": "default true"}}),
    _t("list_windows", "List windows in the workspace: id, title, app id, focused/maximized/minimized, and "
       "position/size when known."),
    _t("window_action", "Focus, close, maximize, unmaximize, minimize, unminimize, fullscreen or unfullscreen "
       "a window (id from list_windows). Use this to switch between apps: focus the other window.",
       {"window": {"type": "integer"}, "action": {"type": "string", "enum": [
           "focus", "close", "maximize", "unmaximize", "minimize", "unminimize", "fullscreen",
           "unfullscreen"]}}, ["window", "action"]),
    _t("window_move", "Move a floating window so its top-left corner is at x,y.",
       {"window": {"type": "integer"}, "x": NUM, "y": NUM}, ["window", "x", "y"]),
    _t("window_resize", "Resize a floating window to width x height pixels.",
       {"window": {"type": "integer"}, "width": NUM, "height": NUM}, ["window", "width", "height"]),
    _t("mouse_move", "Move the workspace pointer.", {"x": NUM, "y": NUM}, ["x", "y"]),
    _t("mouse_click", "Click in the workspace (moves the pointer first when x,y are given).", {
        "x": NUM, "y": NUM, "button": BTN, "count": {"type": "integer", "description": "2 = double click"}}),
    _t("double_click", "Double-click at x,y (opens files, selects a word).", {"x": NUM, "y": NUM, "button": BTN},
       ["x", "y"]),
    _t("mouse_drag", "Press at (x1,y1), drag to (x2,y2), release.",
       {"x1": NUM, "y1": NUM, "x2": NUM, "y2": NUM, "button": BTN}, ["x1", "y1", "x2", "y2"]),
    _t("scroll", "Scroll with the wheel. dy>0 scrolls down. Optionally move the pointer to x,y first.",
       {"dy": NUM, "dx": NUM, "x": NUM, "y": NUM}, ["dy"]),
    _t("keyboard_type", "Type text into the focused window of the workspace.", {"text": {"type": "string"}},
       ["text"]),
    _t("keyboard_key", "Press a key or chord, e.g. 'Return', 'ctrl+a', 'ctrl+s', 'alt+F4', 'ctrl+shift+t', 'Tab'.",
       {"combo": {"type": "string"}}, ["combo"]),
    _t("get_clipboard", "Read the workspace's OWN clipboard text (never the user's clipboard)."),
    _t("set_clipboard", "Set the workspace's OWN clipboard text (never the user's clipboard).",
       {"text": {"type": "string"}}, ["text"]),
    _t("wait", "Wait for the UI to settle.", {"seconds": {"type": "number", "minimum": 0, "maximum": 30}},
       ["seconds"], ws=False),
    _t("request_human_input", "Ask the human (shown in their notch/panel) to approve or provide something for "
       "this workspace. Returns an id; poll with workspace_status.", {
           "title": {"type": "string"}, "detail": {"type": "string"}}, ["title"]),
]
TOOL_NAMES = {t["name"] for t in TOOLS}
NO_WORKSPACE_TOOLS = {"list_workspaces", "ensure_workspace", "wait"}


def auto_name(kind: str, project: str) -> str:
    """Stable workspace name for an agent kind in a project: valid per the daemon's NAME_RE, <= 40 chars."""
    proj = re.sub(r"[^A-Za-z0-9_.\-]+", "-", project or "").strip("-._") or "project"
    name = f"{kind}-{proj}"
    if len(name) > 40:
        name = name[:33].rstrip("-._") + "-" + hashlib.sha1(project.encode()).hexdigest()[:6]
    return name


def default_project() -> str:
    p = os.environ.get("AW_PROJECT")
    if p:
        return p
    cwd = os.getcwd()
    return "home" if os.path.realpath(cwd) == os.path.realpath(os.path.expanduser("~")) else os.path.basename(cwd)


class Server:
    def __init__(self, default_workspace: Optional[str] = None):
        self.kind = os.environ.get("AW_AGENT_KIND") or None
        self.default_ws = default_workspace or os.environ.get("AW_WORKSPACE") or None
        # All agent calls use a WORKSPACE credential (found by the client: AW_TOKEN / AW_TOKEN_FILE or the token
        # file of the workspace being addressed). It cannot create, destroy or take over anything.
        self.client = Client(role="agent", workspace=self.default_ws)
        self._prov: Optional[Client] = None
        self.project = default_project()
        self.auto = os.environ.get("AW_AUTO_CREATE", "1") != "0" and self.kind is not None
        self.shots_dir: Optional[str] = None
        self._ws_cache: Optional[str] = None
        self._shot_n = 0

    @property
    def my_name(self) -> str:
        return auto_name(self.kind or "other", self.project)

    # ---- workspace discovery / creation
    def _provisioner(self) -> Client:
        """The credential used ONLY to create / start / resize this agent's own workspace (the human delegated
        that by installing the integration). Never used for input, screenshots or any other workspace.
        AW_PROVISION=0 disables it: then the workspace must already exist (e.g. created by the human)."""
        if os.environ.get("AW_PROVISION", "1") == "0":
            raise ApiError("E_NEEDS_HUMAN", f"provisioning is disabled (AW_PROVISION=0): ask the user to run "
                           f"'agent-workspaces create --name {self.my_name} --agent {self.kind or 'other'}'")
        if self._prov is None:
            self._prov = Client(role="admin")
        return self._prov

    def _prov_call(self, method: str, **p) -> Any:
        try:
            return self._provisioner().call(method, **p)
        except ApiError as e:
            if e.code in ("E_NO_CREDENTIALS", "E_INSECURE_CREDENTIAL_FILE", "E_UNAUTHENTICATED", "E_FORBIDDEN"):
                raise ApiError("E_NEEDS_HUMAN", f"this needs the human: ask the user to run "
                               f"'agent-workspaces create --name {self.my_name} --agent {self.kind or 'other'}' "
                               f"({e.code}: {e.message})")
            raise

    def ensure(self, name: Optional[str] = None, **create_kw) -> Dict[str, Any]:
        """Attach to the workspace called `name` (default: this agent's automatic one), creating or starting it
        when needed."""
        name = name or self.my_name
        # 1. attach with the agent's own workspace credential (no privileges needed when it already runs)
        try:
            w = self.client.call("workspace.get", workspace=name)
            if w["agent"]["kind"] not in (self.kind or "other", "none"):
                raise ApiError("E_ASSIGNMENT", f"a workspace called {name!r} exists but is assigned to a "
                               "different agent; pass 'workspace' explicitly")
            if w["state"] == "running":
                self._ws_cache = w["name"]
                return dict(w, created=False)
            self._prov_call("workspace.start", workspace=name)
            self._ws_cache = w["name"]
            return dict(self.client.call("workspace.get", workspace=name), created=False)
        except ApiError as e:
            if e.code not in ("E_NO_CREDENTIALS", "E_FORBIDDEN", "E_NOT_FOUND", "E_UNAUTHENTICATED", "E_AMBIGUOUS"):
                raise
        # 2. provision: create it (or start it) with the delegated credential
        for _ in range(2):
            found = [w for w in self._prov_call("workspace.list")["workspaces"] if w["name"].lower() == name.lower()]
            if found:
                w = found[0]
                if w["agent"]["kind"] not in (self.kind or "other", "none"):
                    raise ApiError("E_ASSIGNMENT", f"a workspace called {name!r} exists but is assigned to a "
                                   "different agent; pass 'workspace' explicitly")
                if w["state"] != "running":
                    w = self._prov_call("workspace.start", workspace=w["name"])
                self._ws_cache = w["name"]
                return dict(w, created=False)
            try:
                w = self._prov_call("workspace.create", name=name, agent_kind=self.kind or "other", **create_kw)
                self._ws_cache = w["name"]
                return dict(w, created=True)
            except ApiError as e:
                if e.code != "E_EXISTS":
                    raise          # raced another process of this agent: look again
        raise ApiError("E_INTERNAL", "could not attach to or create the workspace")

    def _resolve(self, args: Dict[str, Any]) -> str:
        ref = args.get("workspace") or self.default_ws
        if ref:
            return ref
        if self.auto:
            if self._ws_cache:
                return self._ws_cache
            return self.ensure()["name"]
        res = self.client.call("workspace.list", **self._ck())
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

    # ---- helpers
    @staticmethod
    def _text(s: str) -> List[Dict[str, Any]]:
        return [{"type": "text", "text": s}]

    def _shot(self, ws: str, a: Dict[str, Any], want_file: bool = True) -> Dict[str, Any]:
        kw: Dict[str, Any] = dict(self._ck())
        if a.get("region"):
            kw["region"] = a["region"]
        if a.get("max_width"):
            kw["max_width"] = int(a["max_width"])
        if want_file and os.environ.get("AW_SAVE_SHOTS", "1") != "0":
            self._shot_n = (self._shot_n + 1) % 40
            kw["path"] = f"shot-{self._shot_n:02d}.png"      # a workspace credential may only write in <ws>/shots
        return self.client.call("screen.screenshot", workspace=ws, cursor=bool(a.get("cursor")), inline=True, **kw)

    def _shot_parts(self, r: Dict[str, Any], prefix: str = "") -> List[Dict[str, Any]]:
        txt = f"{prefix}workspace '{r['workspace']}' screen {r['width']}x{r['height']} px (captured in {r['capture_ms']} ms)"
        if r.get("path"):
            txt += f"; also saved as {r['path']}"
        return [{"type": "image", "data": r["base64"], "mimeType": "image/png"}, {"type": "text", "text": txt}]

    def _wait_first_window(self, ws: str, before: int, timeout: float = 6.0) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                if len(self.client.call("window.list", workspace=ws, **self._ck())["windows"]) > before:
                    break
            except ApiError:
                break
            time.sleep(0.3)
        time.sleep(0.8)

    def _call_ws_tool(self, name: str, a: Dict[str, Any]) -> List[Dict[str, Any]]:
        c = self.client
        ws = self._resolve(a)
        ck = self._ck()
        if name == "workspace_status":
            return self._text(json.dumps(c.call("workspace.get", workspace=ws), indent=2))
        if name == "workspace_control":
            act = a["action"]
            own = self.default_ws or self._ws_cache or self.my_name
            if ws != own and ws.lower() != own.lower():
                raise ApiError("E_FORBIDDEN", "you may only control your own workspace")
            if act in ("stop", "restart"):
                self._prov_call("workspace.stop", workspace=ws)
            if act in ("start", "restart"):
                self._prov_call("workspace.start", workspace=ws)
            return self._text(f"workspace {ws}: {act} done")
        if name == "configure_display":
            kw = {k: a[k] for k in ("width", "height", "scale") if k in a}
            if not kw:
                raise ApiError("E_BAD_ARGS", "give width, height and/or scale")
            own = self.default_ws or self._ws_cache or self.my_name
            if ws != own and ws.lower() != own.lower():
                raise ApiError("E_FORBIDDEN", "you may only configure your own workspace")
            w = self._prov_call("workspace.configure", workspace=ws, **kw)
            return self._text(f"display is now {w['display']['width']}x{w['display']['height']} scale {w['display']['scale']}")
        if name == "screenshot":
            return self._shot_parts(self._shot(ws, a))
        if name == "describe_screen":
            r = self._shot(ws, {"max_width": a.get("max_width")}, want_file=False)
            try:
                return self._text(vision.describe(r["base64"], r["width"], r["height"], a.get("question")))
            except vision.VisionUnavailable as e:
                raise ApiError("E_NO_VISION", str(e))
        if name == "display_info":
            return self._text(json.dumps(c.call("screen.info", workspace=ws, **ck), indent=2))
        if name == "list_apps":
            lst = appsmod.list_apps()
            if not lst:
                return self._text("no launchable applications found")
            roles = []
            for role in ("browser", "text editor", "file manager", "calculator", "image viewer"):
                f = appsmod.find_app(role, lst)
                if f:
                    roles.append(f"{role}: {f['id']} ({f['name']})")
            rest = [f"{x['id']}: {x['name']}" for x in lst if "Game" not in x["categories"]
                    and not any(c.startswith("X-") for c in x["categories"])]
            return self._text("Defaults (launch_app app=<role or id>):\n" + "\n".join(roles) +
                              f"\n\nAll ({len(rest)}):\n" + "\n".join(rest[:80]))
        if name == "list_windows":
            return self._text(json.dumps(c.call("window.list", workspace=ws, **ck)["windows"], indent=2))
        if name == "window_action":
            c.call("window.action", workspace=ws, window=int(a["window"]), action=a["action"], **ck)
            return self._text("ok")
        if name in ("window_move", "window_resize"):
            method = "window.move" if name == "window_move" else "window.resize"
            keys = ("x", "y") if name == "window_move" else ("width", "height")
            try:
                c.call(method, workspace=ws, window=int(a["window"]), **{k: int(a[k]) for k in keys}, **ck)
            except ApiError as e:
                if e.code == "E_UNKNOWN_METHOD":
                    raise ApiError("E_UNSUPPORTED", f"{name} is not available in this AgentWorkspaces daemon yet. "
                                   "Workaround: drag the window's title bar / edge with mouse_drag (only in a "
                                   "workspace created with window_policy=floating).")
                raise
            return self._text("ok")
        if name == "launch_app":
            return self._launch(ws, a)
        if name == "double_click":
            a = dict(a, count=2)
            name = "mouse_click"
        method = {"mouse_move": "input.move", "mouse_click": "input.click", "mouse_drag": "input.drag",
                  "scroll": "input.scroll", "keyboard_type": "input.type", "keyboard_key": "input.key"}.get(name)
        if method:
            p = {k: v for k, v in a.items() if k != "workspace"}
            r = c.call(method, workspace=ws, **p, **ck)
            ptr = r.get("pointer", {})
            return self._text(f"ok (pointer at {ptr.get('x')},{ptr.get('y')})")
        if name == "get_clipboard":
            return self._text(c.call("clipboard.get", workspace=ws, **ck)["text"] or "")
        if name == "set_clipboard":
            c.call("clipboard.set", workspace=ws, text=a["text"], **ck)
            return self._text("ok")
        if name == "request_human_input":
            r = c.call("approval.request", workspace=ws, title=a["title"], detail=a.get("detail", ""), **ck)
            return self._text(f"request {r['id']} sent to the user (status: {r['status']})")
        raise ApiError("E_UNKNOWN_TOOL", f"unknown tool {name}")

    def _launch(self, ws: str, a: Dict[str, Any]) -> List[Dict[str, Any]]:
        if a.get("app"):
            found = appsmod.find_app(a["app"])
            if not found:
                raise ApiError("E_NOT_FOUND", f"no installed application matches {a['app']!r}; call list_apps")
            argv = appsmod.launch_argv(found)
            label = found["name"]
        elif a.get("command"):
            cmd = a["command"]
            argv = shlex.split(cmd) if isinstance(cmd, str) else list(cmd)
            label = argv[0] if argv else ""
        else:
            raise ApiError("E_BAD_ARGS", "give 'app' or 'command'")
        argv += [str(x) for x in (a.get("args") or [])]
        if not argv:
            raise ApiError("E_BAD_ARGS", "empty command")
        try:
            before = len(self.client.call("window.list", workspace=ws, **self._ck())["windows"])
        except ApiError:
            before = 0
        r = self.client.call("workspace.launch", workspace=ws, argv=argv, cwd=a.get("cwd"), **self._ck())
        head = f"launched {label} (app {r['app']}, pid {r['pid']}) in workspace {ws}"
        if a.get("screenshot") is False:
            return self._text(head)
        self._wait_first_window(ws, before)
        return self._shot_parts(self._shot(ws, {}), prefix=head + "; ")

    def call_tool(self, name: str, a: Dict[str, Any]) -> List[Dict[str, Any]]:
        if name not in TOOL_NAMES:
            raise ApiError("E_UNKNOWN_TOOL", f"unknown tool {name}")
        spec = next(t for t in TOOLS if t["name"] == name)
        missing = [k for k in spec["inputSchema"].get("required", []) if k not in a]
        if missing:
            raise ApiError("E_BAD_ARGS", f"missing required argument(s): {', '.join(missing)}")
        for k, v in a.items():                 # enum validation (the schema is advisory for some clients)
            en = spec["inputSchema"]["properties"].get(k, {}).get("enum")
            if en and v not in en:
                raise ApiError("E_BAD_ARGS", f"{k} must be one of {en}")
        c = self.client
        if name == "list_workspaces":
            res = c.call("workspace.list", **self._ck())
            lines = []
            for w in res["workspaces"]:
                d = w["display"]
                mark = "  <- yours" if self.auto and w["name"].lower() == self.my_name.lower() else ""
                lines.append(f"- {w['name']}: {w['state']}, {d['width']}x{d['height']}, agent={w['agent']['label']}, "
                             f"input={w['input_mode']}, windows={w['activity']['window_count']}{mark}")
            if self.auto and not any(w["name"].lower() == self.my_name.lower() for w in res["workspaces"]):
                lines.append(f"(your workspace '{self.my_name}' does not exist yet: it is created automatically "
                             "on your first graphical tool call)")
            return self._text("\n".join(lines) or "No workspaces yet. Ask the user to create one "
                              "(agent-workspaces create --name NAME --agent KIND) or call ensure_workspace.")
        if name == "ensure_workspace":
            kw = {k: a[k] for k in ("width", "height") if a.get(k)}
            w = self.ensure(a.get("name"), **kw)
            d = w["display"]
            return self._text(f"workspace '{w['name']}' {'created' if w['created'] else 'attached'} "
                              f"({w['state']}, {d['width']}x{d['height']})")
        if name == "wait":
            time.sleep(min(max(float(a["seconds"]), 0), 30))
            return self._text("waited")
        try:
            return self._call_ws_tool(name, a)
        except ApiError as e:
            if e.code == "E_NOT_FOUND" and self._ws_cache and not a.get("workspace") and self.auto \
                    and "workspace" in e.message.lower():
                self._ws_cache = None          # destroyed meanwhile: create/attach again, once
                return self._call_ws_tool(name, a)
            raise

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
                hint = (" Stop sending input: the user took over this workspace. Wait and poll workspace_status."
                        if e.code == "E_PAUSED" else "")
                return {"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": f"{e.code}: {e.message}.{hint}" if hint else
                                 f"{e.code}: {e.message}"}], "isError": True}}
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
