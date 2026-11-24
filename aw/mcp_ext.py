"""MCP tool definitions for windows, browsers and audio (to be merged into aw/mcp.py's TOOLS).

Wiring (in `aw/mcp.py`):

    from . import mcp_ext
    TOOLS = TOOLS + mcp_ext.TOOLS
    ...
    # in Server.call_tool, after the workspace is resolved:
    if name in mcp_ext.NAMES:
        return mcp_ext.call(self.client, name, a, ws)

The tools only call daemon API methods (docs/api.md); they hold no display, profile or audio access of
their own. Browser profile access for a user's signed-in profile can only be REQUESTED here
(`request_browser_profile`); the human approves it in a terminal (docs/browsers.md).
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

WS = {"type": "string", "description": "Workspace name or id. Optional when only one is assigned to you."}
INT = {"type": "integer"}


def _t(name, desc, props=None, required=None):
    return {"name": name, "description": desc,
            "inputSchema": {"type": "object", "properties": {"workspace": WS, **(props or {})},
                            "required": required or [], "additionalProperties": False}}


TOOLS: List[Dict[str, Any]] = [
    _t("window_move", "Move a window (list_windows gives ids). x,y = top-left of the window content in workspace "
       "pixels.", {"window": INT, "x": INT, "y": INT}, ["window", "x", "y"]),
    _t("window_resize", "Resize a window's content area in workspace pixels.",
       {"window": INT, "width": INT, "height": INT}, ["window", "width", "height"]),
    _t("window_set_geometry", "Move and resize a window in one step (content rectangle, workspace pixels).",
       {"window": INT, "x": INT, "y": INT, "width": INT, "height": INT},
       ["window", "x", "y", "width", "height"]),
    _t("list_browsers", "Installed browsers (names/ids), whether each can be launched here, and browser profiles: "
       "your own persistent ones and the names of the user's profiles (which you can only request access to)."),
    _t("launch_browser", "Start a browser inside this workspace. Default profile: a throwaway one deleted with the "
       "workspace. profile 'agent:NAME' keeps a persistent profile of yours. profile 'authorized:ID' uses a COPY of "
       "one of the user's profiles, only after the user approved it. No debugging port, extensions or automation "
       "flags are possible.", {
           "browser": {"type": "string", "description": "id from list_browsers (e.g. google-chrome, firefox)"},
           "profile": {"type": "string", "description": "throwaway | agent:NAME | authorized:ID"},
           "url": {"type": "string"},
           "args": {"type": "array", "items": {"type": "string"}}}),
    _t("request_browser_profile", "Ask the user for access to one of their signed-in browser profiles (a private "
       "copy, cookies only). Returns a request id; only the user can approve it, in a terminal. Do not retry in a loop.",
       {"browser": {"type": "string"}, "profile": {"type": "string"}, "reason": {"type": "string"}},
       ["browser", "profile"]),
    _t("audio_info", "State of the workspace's private audio: sink, running streams, routing."),
    _t("audio_record", "Record what the workspace is playing (its private sink) to a WAV file; returns level and, "
       "for a pure tone, its frequency.", {"seconds": {"type": "number", "minimum": 0.1, "maximum": 120},
                                           "path": {"type": "string",
                                                    "description": "file name inside the workspace's shots folder"}},
       ["seconds"]),
]
NAMES = {t["name"] for t in TOOLS}


def call(client, name: str, a: Dict[str, Any], ws: str) -> List[Dict[str, Any]]:
    """Execute one of TOOLS against the daemon. `ws` is the resolved workspace."""
    if name in ("window_move", "window_resize", "window_set_geometry"):
        method = {"window_move": "window.move", "window_resize": "window.resize",
                  "window_set_geometry": "window.set_geometry"}[name]
        p = {k: a[k] for k in ("window", "x", "y", "width", "height") if k in a}
        r = client.call(method, workspace=ws, **p)
        w = r.get("window") or {}
        return [{"type": "text", "text": json.dumps({k: w.get(k) for k in (
            "id", "title", "kind", "geometry", "geometry_source", "maximized", "focused")})}]
    if name == "list_browsers":
        return [{"type": "text", "text": json.dumps(client.call("browser.list", workspace=ws), indent=2)}]
    if name == "launch_browser":
        p = {k: a[k] for k in ("browser", "profile", "url", "args") if k in a}
        r = client.call("browser.launch", workspace=ws, **p)
        return [{"type": "text", "text": f"launched {r['browser']} (app {r['app']}, pid {r['pid']}), profile "
                 f"{r['profile']['mode']}{':' + r['profile']['name'] if r['profile']['name'] else ''}. "
                 "Take a screenshot to see it."}]
    if name == "request_browser_profile":
        r = client.call("browser.profile.request", workspace=ws, browser=a["browser"], profile=a["profile"],
                        reason=a.get("reason", ""))
        return [{"type": "text", "text": f"request {r['id']} is {r['status']}. {r.get('how', '')} "
                 "Continue with another task; the user decides."}]
    if name == "audio_info":
        return [{"type": "text", "text": json.dumps(client.call("audio.info", workspace=ws), indent=2)}]
    if name == "audio_record":
        r = client.call("audio.record", workspace=ws, seconds=a["seconds"], **({"path": a["path"]} if a.get("path") else {}))
        return [{"type": "text", "text": json.dumps({k: r.get(k) for k in (
            "path", "seconds", "peak", "rms_dbfs", "tone_hz")})}]
    raise KeyError(name)
