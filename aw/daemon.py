"""agent-workspacesd: the workspace manager daemon.

Local API: newline-delimited JSON over a user-only unix socket (docs/api.md).
The daemon never opens or reads any host display socket; it only talks to the compositors of
workspaces it manages, via their own socket paths.
"""
from __future__ import annotations

import base64
import json
import os
import queue
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import traceback
from typing import Any, Dict, List, Optional

from . import API_VERSION, __version__, paths
from .manager import AGENT_KINDS, Manager, ManagerError
from .session import SessionError

INPUT_METHODS = {"input.move", "input.click", "input.drag", "input.type", "input.key", "input.scroll",
                 "window.action", "clipboard.set"}


class Approval:
    def __init__(self, aid, workspace, title, detail, kind):
        self.id, self.workspace, self.title, self.detail, self.kind = aid, workspace, title, detail, kind
        self.status, self.created, self.resolved = "pending", time.time(), None

    def to_json(self):
        return {"id": self.id, "workspace": self.workspace, "title": self.title, "detail": self.detail,
                "kind": self.kind, "status": self.status, "created_at": self.created,
                "resolved_at": self.resolved}


class EventBus:
    def __init__(self):
        self.subs: List[Dict[str, Any]] = []
        self.lock = threading.Lock()

    def subscribe(self, q: "queue.Queue", workspaces: Optional[List[str]], types: Optional[List[str]]):
        s = {"q": q, "ws": set(workspaces or []), "types": set(types or [])}
        with self.lock:
            self.subs.append(s)
        return s

    def unsubscribe(self, s):
        with self.lock:
            if s in self.subs:
                self.subs.remove(s)

    def emit(self, typ: str, data: Dict[str, Any]):
        ev = {"v": API_VERSION, "event": typ, "data": data, "ts": time.time()}
        with self.lock:
            subs = list(self.subs)
        for s in subs:
            if s["types"] and not any(typ == t or typ.startswith(t.rstrip("*")) and t.endswith("*")
                                      for t in s["types"]):
                continue
            if s["ws"] and not ({data.get("id"), data.get("name"), data.get("workspace")} & s["ws"]):
                continue
            try:
                s["q"].put_nowait(ev)
            except queue.Full:
                pass


class Daemon:
    def __init__(self):
        self.bus = EventBus()
        self.mgr = Manager(emit=self.bus.emit)
        self.approvals: Dict[str, Approval] = {}
        self._aseq = 0
        self.lock = threading.Lock()
        self.started = time.time()
        self._watchers: Dict[str, threading.Thread] = {}

    # ---- helpers
    def _ws(self, p) -> Any:
        ref = p.get("workspace")
        if not ref:
            raise ManagerError("E_BAD_ARGS", "missing 'workspace'")
        return self.mgr.get(ref)

    def _check_assignment(self, ws, p):
        """Cooperative scoping: an agent that identifies itself may only act on workspaces assigned
        to its kind (or unassigned ones). Not a security boundary (see docs/security-model.md)."""
        kind = p.get("agent_kind")
        if kind and kind != "human" and ws.agent.get("kind") not in ("none", kind):
            raise ManagerError("E_ASSIGNMENT", f"workspace {ws.name!r} is assigned to "
                               f"{ws.agent.get('kind')}, not {kind}")

    def _guard_input(self, ws, p):
        actor = "human" if p.get("agent_kind") == "human" or p.get("actor") == "human" else "agent"
        self._check_assignment(ws, p)
        if actor == "agent" and ws.input_mode == "manual":
            self.bus.emit("input.rejected", {"id": ws.id, "name": ws.name, "reason": "manual_takeover"})
            raise ManagerError("E_PAUSED", f"workspace {ws.name!r} is in manual takeover: agent input is "
                               f"rejected until 'resume' (reason: {ws.takeover_reason or 'none'})")
        return actor

    def _session(self, ws):
        return self.mgr.session(ws)

    # ---- dispatch
    def handle(self, method: str, p: Dict[str, Any], conn_state: Dict[str, Any]) -> Any:
        m = self.mgr
        if method == "hello":
            return {"api_version": API_VERSION, "daemon_version": __version__,
                    "agent_kinds": {k: v for k, v in AGENT_KINDS.items()}, "uptime": time.time() - self.started,
                    "systemd_scopes": m.use_systemd}
        if method == "workspace.list":
            allw = [m.summary(w) for w in sorted(m.workspaces.values(), key=lambda w: w.created_at)]
            if p.get("agent_kind"):
                allw = [w for w in allw if w["agent"]["kind"] in (p["agent_kind"], "none")]
            for w in allw:
                w["pending_approvals"] = sum(1 for a in self.approvals.values()
                                             if a.workspace == w["id"] and a.status == "pending")
            return {"workspaces": allw}
        if method in ("workspace.get", "workspace.status"):
            ws = self._ws(p)
            d = m.summary(ws)
            d["usage"] = m.usage(ws)
            d["pending_approvals"] = sum(1 for a in self.approvals.values()
                                         if a.workspace == ws.id and a.status == "pending")
            try:
                d["windows"] = self._session(ws).list_windows() if ws.state == "running" else []
            except ManagerError:
                d["windows"] = []
            return d
        if method == "workspace.create":
            allowed = ("agent_kind", "agent_label", "width", "height", "scale", "gpu", "clipboard",
                       "window_policy", "home", "memory_max", "render_node", "xwayland")
            kw = {k: p[k] for k in allowed if k in p}
            ws = m.create(p.get("name", ""), start=p.get("start", True), **kw)
            return m.summary(ws)
        if method == "workspace.rename":
            return m.summary(m.rename(p["workspace"], p.get("name", "")))
        if method == "workspace.assign":
            return m.summary(m.assign(p["workspace"], p.get("agent_kind", "none"), p.get("agent_label", "")))
        if method == "workspace.configure":
            ws = self._ws(p)
            keys = ("width", "height", "scale", "gpu", "clipboard", "window_policy", "memory_max")
            return m.summary(m.configure(ws.id, **{k: p.get(k) for k in keys}))
        if method == "workspace.start":
            return m.summary(m.start(self._ws(p)))
        if method == "workspace.stop":
            m.stop(self._ws(p))
            return {"ok": True}
        if method == "workspace.destroy":
            m.destroy(p["workspace"])
            return {"ok": True}
        if method in ("workspace.pause", "workspace.takeover"):
            return m.summary(m.set_input_mode(p["workspace"], "manual", p.get("reason", "")))
        if method == "workspace.resume":
            return m.summary(m.set_input_mode(p["workspace"], "agent"))
        if method == "workspace.usage":
            return m.usage(self._ws(p))
        if method == "workspace.enter":
            return self._enter(p)
        if method == "workspace.launch":
            ws = self._ws(p)
            self._check_assignment(ws, p)
            argv = p.get("argv")
            if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
                raise ManagerError("E_BAD_ARGS", "'argv' must be a list of strings")
            m.note_action(ws, "launch", "agent")
            return m.launch(ws, argv, cwd=p.get("cwd"), extra_env=p.get("env"))
        if method == "app.list":
            ws = self._ws(p)
            return {"apps": m.summary(ws)["apps"]}
        if method == "app.kill":
            m.kill_app(self._ws(p), int(p["app"]))
            return {"ok": True}
        if method == "screen.screenshot":
            return self._screenshot(p)
        if method == "screen.info":
            ws = self._ws(p)
            self._check_assignment(ws, p)
            info = self._session(ws).display_info()
            info["workspace"] = ws.name
            info["x_display"] = ws.x_display
            return info
        if method == "window.list":
            ws = self._ws(p)
            self._check_assignment(ws, p)
            return {"windows": self._session(ws).list_windows()}
        if method in INPUT_METHODS:
            return self._input(method, p)
        if method == "clipboard.get":
            ws = self._ws(p)
            self._check_assignment(ws, p)
            return {"text": self._session(ws).get_clipboard()}
        if method == "viewer.attach":
            ws = self._ws(p)
            conn_state.setdefault("viewer_of", []).append(ws.id)
            ws.viewers += 1
            self.bus.emit("viewer.attached", {"id": ws.id, "name": ws.name, "viewers": ws.viewers})
            return {"ok": True, "viewers": ws.viewers}
        if method == "approval.request":
            ws = self._ws(p)
            self._check_assignment(ws, p)
            with self.lock:
                self._aseq += 1
                a = Approval(f"a{self._aseq}", ws.id, p.get("title", "Agent needs input"),
                             p.get("detail", ""), p.get("kind", "input"))
                self.approvals[a.id] = a
            self.bus.emit("approval.requested", {"id": ws.id, "name": ws.name, "approval": a.to_json()})
            return a.to_json()
        if method == "approval.list":
            items = [a.to_json() for a in self.approvals.values()
                     if (not p.get("workspace") or a.workspace == self._ws(p).id)
                     and (not p.get("pending") or a.status == "pending")]
            return {"approvals": items}
        if method == "approval.get":
            a = self.approvals.get(p.get("id"))
            if not a:
                raise ManagerError("E_NOT_FOUND", "no such approval")
            return a.to_json()
        if method == "approval.resolve":
            a = self.approvals.get(p.get("id"))
            if not a:
                raise ManagerError("E_NOT_FOUND", "no such approval")
            if p.get("decision") not in ("approved", "denied"):
                raise ManagerError("E_BAD_ARGS", "decision must be approved|denied")
            a.status, a.resolved = p["decision"], time.time()
            ws = m.workspaces.get(a.workspace)
            self.bus.emit("approval.resolved", {"id": a.workspace, "name": ws.name if ws else "",
                                                "approval": a.to_json()})
            return a.to_json()
        raise ManagerError("E_UNKNOWN_METHOD", f"unknown method {method!r}")

    def _screenshot(self, p):
        ws = self._ws(p)
        self._check_assignment(ws, p)
        s = self._session(ws)
        t0 = time.perf_counter()
        region = p.get("region")
        w, h, rgb = s.capture_raw(region, bool(p.get("cursor")))
        max_w = int(p.get("max_width") or 0)
        if max_w and w > max_w:
            w, h, rgb = _downscale(w, h, rgb, max_w)
        from .session import encode_png
        png = encode_png(w, h, rgb)
        self.mgr.note_action(ws, "screenshot", "human" if p.get("agent_kind") == "human" else "agent")
        out: Dict[str, Any] = {"width": w, "height": h, "bytes": len(png), "format": "png",
                               "capture_ms": round((time.perf_counter() - t0) * 1000, 1),
                               "workspace": ws.name, "workspace_id": ws.id}
        path = p.get("path")
        if path:
            with open(path, "wb") as f:
                f.write(png)
            out["path"] = path
        if p.get("inline", not path):
            out["base64"] = base64.b64encode(png).decode()
        return out

    def _input(self, method: str, p):
        ws = self._ws(p)
        actor = self._guard_input(ws, p)
        s = self._session(ws)
        kind = method.split(".", 1)[1]
        if method == "input.move":
            s.pointer_move(float(p["x"]), float(p["y"]))
        elif method == "input.click":
            s.click(p.get("x"), p.get("y"), p.get("button", "left"), int(p.get("count", 1)))
        elif method == "input.drag":
            s.drag(float(p["x1"]), float(p["y1"]), float(p["x2"]), float(p["y2"]), p.get("button", "left"))
        elif method == "input.type":
            s.type_text(str(p["text"]))
        elif method == "input.key":
            s.key(str(p["combo"]))
        elif method == "input.scroll":
            s.scroll(float(p.get("dx", 0)), float(p.get("dy", 0)), p.get("x"), p.get("y"))
        elif method == "window.action":
            s.window_action(int(p["window"]), p["action"])
            kind = "window." + p["action"]
        elif method == "clipboard.set":
            s.set_clipboard(str(p["text"]))
            kind = "clipboard"
        self.mgr.note_action(ws, kind, actor)
        return {"ok": True, "pointer": {"x": s.pos[0], "y": s.pos[1]}}

    def _enter(self, p):
        ws = self._ws(p)
        if ws.state != "running":
            raise ManagerError("E_NOT_RUNNING", f"workspace {ws.name!r} is {ws.state}")
        if p.get("takeover"):
            self.mgr.set_input_mode(ws.id, "manual", "entered by user")
        self.bus.emit("workspace.enter_requested", {"id": ws.id, "name": ws.name})
        env = p.get("display_env") or {}
        if not env.get("WAYLAND_DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            return {"launched": False, "reason": "no host display known; run `agent-workspaces enter` "
                    "from a session terminal or pass display_env"}
        e = {k: v for k, v in os.environ.items()}
        e.update({k: v for k, v in env.items() if k in ("WAYLAND_DISPLAY", "DISPLAY", "XDG_RUNTIME_DIR",
                                                       "DBUS_SESSION_BUS_ADDRESS")})
        viewer = [sys.executable, "-m", "aw.viewer", ws.name]
        proc = subprocess.Popen(viewer, env=e, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=open(os.path.join(ws.dir, "viewer.log"), "ab"), start_new_session=True)
        return {"launched": True, "pid": proc.pid}


def _downscale(w: int, h: int, rgb: bytes, max_w: int):
    """Box-free nearest-neighbour downscale with stdlib only (good enough for model input)."""
    try:
        from PIL import Image  # optional, better quality
        im = Image.frombytes("RGB", (w, h), rgb)
        nh = max(1, round(h * max_w / w))
        im = im.resize((max_w, nh), Image.LANCZOS)
        return max_w, nh, im.tobytes()
    except ImportError:
        pass
    nh = max(1, round(h * max_w / w))
    xs = [int(i * w / max_w) for i in range(max_w)]
    out = bytearray(max_w * nh * 3)
    for oy in range(nh):
        sy = int(oy * h / nh)
        row = rgb[sy * w * 3:(sy + 1) * w * 3]
        o = oy * max_w * 3
        for ox, sx in enumerate(xs):
            out[o + ox * 3:o + ox * 3 + 3] = row[sx * 3:sx * 3 + 3]
    return max_w, nh, bytes(out)


class Handler(socketserver.StreamRequestHandler):
    daemon: Daemon

    def handle(self):
        state: Dict[str, Any] = {}
        wlock = threading.Lock()
        sub = None
        pump: Optional[threading.Thread] = None
        q: "queue.Queue" = queue.Queue(maxsize=1000)

        def send(obj):
            data = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
            with wlock:
                self.wfile.write(data)
                self.wfile.flush()

        try:
            for raw in self.rfile:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    req = json.loads(raw)
                    rid, method, params = req.get("id"), req["method"], req.get("params") or {}
                except (ValueError, KeyError, TypeError):
                    send({"v": API_VERSION, "id": None, "ok": False,
                          "error": {"code": "E_BAD_REQUEST", "message": "invalid JSON request"}})
                    continue
                try:
                    if method == "events.subscribe":
                        if sub is None:
                            ids = []
                            for ref in params.get("workspaces") or []:
                                try:
                                    ids.append(self.daemon.mgr.get(ref).id)   # names may change; ids do not
                                except ManagerError:
                                    ids.append(ref)
                            sub = self.daemon.bus.subscribe(q, ids, params.get("types"))

                            def run():
                                while True:
                                    ev = q.get()
                                    if ev is None:
                                        return
                                    try:
                                        send(ev)
                                    except OSError:
                                        return
                            pump = threading.Thread(target=run, daemon=True)
                            pump.start()
                        send({"v": API_VERSION, "id": rid, "ok": True, "result": {"subscribed": True}})
                        continue
                    result = self.daemon.handle(method, params, state)
                    send({"v": API_VERSION, "id": rid, "ok": True, "result": result})
                except ManagerError as e:
                    send({"v": API_VERSION, "id": rid, "ok": False, "error": {"code": e.code, "message": e.message}})
                except SessionError as e:
                    send({"v": API_VERSION, "id": rid, "ok": False,
                          "error": {"code": "E_SESSION", "message": str(e)}})
                except (KeyError, ValueError, TypeError) as e:
                    send({"v": API_VERSION, "id": rid, "ok": False,
                          "error": {"code": "E_BAD_ARGS", "message": f"bad arguments: {e!r}"}})
                except OSError as e:
                    send({"v": API_VERSION, "id": rid, "ok": False, "error": {"code": "E_IO", "message": str(e)}})
                except Exception as e:  # keep the daemon alive
                    traceback.print_exc()
                    send({"v": API_VERSION, "id": rid, "ok": False,
                          "error": {"code": "E_INTERNAL", "message": repr(e)}})
        except (ConnectionError, OSError):
            pass
        finally:
            if sub is not None:
                self.daemon.bus.unsubscribe(sub)
                q.put(None)
            for wid in state.get("viewer_of", []):
                ws = self.daemon.mgr.workspaces.get(wid)
                if ws:
                    ws.viewers = max(0, ws.viewers - 1)
                    self.daemon.bus.emit("viewer.detached", {"id": ws.id, "name": ws.name, "viewers": ws.viewers})


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(foreground: bool = True) -> int:
    paths.ensure_private_dir(paths.base_dir())
    sock = paths.api_socket()
    # refuse to start if another daemon answers
    if os.path.exists(sock):
        probe = socket.socket(socket.AF_UNIX)
        try:
            probe.connect(sock)
            print("agent-workspacesd already running", file=sys.stderr)
            return 1
        except OSError:
            os.unlink(sock)
        finally:
            probe.close()
    d = Daemon()
    Handler.daemon = d
    old = os.umask(0o177)
    srv = Server(sock, Handler)
    os.umask(old)
    os.chmod(sock, 0o600)
    stop = threading.Event()

    def on_sig(*_):
        stop.set()
        threading.Thread(target=srv.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, on_sig)
    signal.signal(signal.SIGINT, on_sig)
    print(f"agent-workspacesd {__version__} listening on {sock}", flush=True)
    try:
        srv.serve_forever()
    finally:
        d.mgr.shutdown_sessions()
        srv.server_close()
        try:
            os.unlink(sock)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(serve())
