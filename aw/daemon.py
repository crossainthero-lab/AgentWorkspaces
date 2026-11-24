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

from . import API_VERSION, __version__, auth, paths
from .auth import AuthError, Principal
from .grants import GrantError, GrantStore
from .manager import AGENT_KINDS, Manager, ManagerError
from .session import SessionError
from . import wl
from .windows import WindowError
from .browser_api import BrowserAPI
from .policy_ext import POLICY_EXT

auth.POLICY.update(POLICY_EXT)

INPUT_METHODS = {"input.move", "input.click", "input.drag", "input.type", "input.key", "input.scroll",
                 "window.action", "window.move", "window.resize", "window.set_geometry", "clipboard.set"}


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
            if s["ws"] and data.get("id") not in s["ws"]:     # match on the immutable id only, never on names
                continue
            try:
                s["q"].put_nowait(ev)
            except queue.Full:
                pass


# Methods whose successful use is written to the audit log (never with parameters).
AUDITED = {"workspace.create", "workspace.destroy", "workspace.start", "workspace.stop", "workspace.pause",
           "workspace.takeover", "workspace.resume", "workspace.configure", "workspace.assign", "workspace.rename",
           "workspace.enter", "workspace.launch", "app.kill", "approval.request", "approval.resolve",
           "credential.issue", "credential.rotate", "credential.revoke", "auth.exchange", "viewer.attach"}
MAX_LINE = 16 * 1024 * 1024


class Daemon:
    def __init__(self):
        self.bus = EventBus()
        self.mgr = Manager(emit=self.bus.emit)
        self.creds = self.mgr.creds
        self.audit = auth.AuditLog(paths.base_dir())
        self.grants = GrantStore(paths.base_dir(), audit=self.audit, emit=self.bus.emit)
        self.browser = BrowserAPI(self)
        try:
            self.browser.sweep()        # authorised profile copies exist only while a live grant backs them
        except OSError:
            pass
        self.approvals: Dict[str, Approval] = {}
        self._aseq = 0
        self.lock = threading.Lock()
        self.started = time.time()
        self._watchers: Dict[str, threading.Thread] = {}

    # ---- helpers
    def _resolve_id(self, ref: str) -> Optional[str]:
        try:
            return self.mgr.get(ref).id
        except ManagerError:
            return None

    def _ws(self, p) -> Any:
        ref = p.get("workspace")
        if not ref:
            raise ManagerError("E_BAD_ARGS", "missing 'workspace'")
        return self.mgr.get(ref)

    def _check_assignment(self, ws, p, pr: Optional[Principal] = None):
        """Cooperative scoping for the ADMIN credential acting on behalf of an agent kind (CLI --as-agent).
        A workspace credential is bound to its workspace by authorisation and needs no kind claim."""
        if pr is not None and not pr.is_admin:
            return
        kind = p.get("agent_kind")
        if kind and kind != "human" and ws.agent.get("kind") not in ("none", kind):
            raise ManagerError("E_ASSIGNMENT", f"workspace {ws.name!r} is assigned to "
                               f"{ws.agent.get('kind')}, not {kind}")

    def _actor(self, p, pr: Optional[Principal]) -> str:
        """'human' only for the admin credential that says so. A workspace credential is always an agent:
        it cannot claim to be the person to bypass manual takeover."""
        if pr is not None and pr.is_admin and (p.get("agent_kind") == "human" or p.get("actor") == "human"):
            return "human"
        return "agent"

    def _guard_input(self, ws, p, pr: Optional[Principal] = None):
        actor = self._actor(p, pr)
        self._check_assignment(ws, p, pr)
        if actor == "agent" and ws.input_mode == "manual":
            self.bus.emit("input.rejected", {"id": ws.id, "name": ws.name, "reason": "manual_takeover"})
            raise ManagerError("E_PAUSED", f"workspace {ws.name!r} is in manual takeover: agent input is "
                               f"rejected until 'resume' (reason: {ws.takeover_reason or 'none'})")
        return actor

    def _session(self, ws):
        return self.mgr.session(ws)

    def _human_channel(self, conn_state) -> Optional[str]:
        """Which human-only channel the caller is on, or None. Heuristics on the peer process (see
        docs/security-model.md): an interactive terminal, the shipped viewer, GNOME Shell. A same-user
        process can imitate them; an agent credential or MCP can not reach this code at all."""
        if conn_state.get("human_channel"):     # set only by in-process (trusted) callers, never from the wire
            return conn_state["human_channel"]
        pid = conn_state.get("peer_pid")
        if not pid:
            return None
        if auth.peer_has_tty(pid):
            return "tty"
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv = f.read().split(b"\0")
            if b"aw.viewer" in argv or any(a.endswith(b"aw/viewer.py") for a in argv):
                return "viewer"
            if os.path.basename(os.readlink(f"/proc/{pid}/exe")) == "gnome-shell":
                return "gnome-shell"
        except OSError:
            pass
        return None

    def _audit_ctx(self, pr: Optional[Principal], conn_state) -> Dict[str, Any]:
        return {"cred": pr.cred_id if pr else None, "who": pr.kind if pr else None,
                "peer_pid": conn_state.get("peer_pid")}

    # ---- authentication entry points
    def _login(self, p, conn_state):
        try:
            pr = self.creds.authenticate(p.get("token"), conn_state.get("peer_pid"))
        except AuthError as e:
            self.audit.write("auth.failed", code=e.code, peer_pid=conn_state.get("peer_pid"))
            raise ManagerError(e.code, e.message)
        conn_state["principal"] = pr
        return {"principal": pr.describe(), "api_version": API_VERSION}

    def admin_state(self) -> Dict[str, Any]:
        """Connection state of a trusted in-process caller acting as the admin credential (unit tests,
        embedding). Never used on the wire."""
        return {"principal": Principal("admin", None, auth.SCOPES, self.creds.admin_cred_id(), "admin"),
                "human_channel": "in-process"}

    def workspace_state(self, ws_ref: str, scopes=None) -> Dict[str, Any]:
        """Connection state of an in-process caller holding the default credential of one workspace."""
        wid = self._resolve_id(ws_ref)
        rec = next((r for r in self.creds.by_hash.values() if r["ws"] == wid and r["kind"] == "default"), None)
        return {"principal": Principal("workspace", wid, scopes or auth.AGENT_DEFAULT_SCOPES,
                                       rec["id"] if rec else "none")}

    # ---- dispatch
    def handle(self, method: str, p: Dict[str, Any], conn_state: Dict[str, Any]) -> Any:
        """Authorise, then dispatch. `conn_state["principal"]` is set by auth.login (or by the transport for
        a per-request token). With no principal only `hello` and `auth.login` work: fail closed."""
        if not isinstance(p, dict):
            raise ManagerError("E_BAD_ARGS", "params must be an object")
        if method == "auth.login":
            return self._login(p, conn_state)
        pr: Optional[Principal] = conn_state.get("principal")
        try:
            auth.authorize(pr, method, p, self._resolve_id, self.creds)
        except AuthError as e:
            self.audit.write("auth.denied", method=method, code=e.code, workspace=p.get("workspace")
                             if isinstance(p.get("workspace"), str) else None, **self._audit_ctx(pr, conn_state))
            raise ManagerError(e.code, e.message)
        try:
            result = self._dispatch(method, p, conn_state, pr)
        except (OSError, wl.WaylandError, SessionError):
            # The connection to a workspace compositor broke mid-call. If the compositor is gone this becomes a
            # clean E_NOT_RUNNING (and the workspace is recorded as stopped); if something else is serving the
            # socket, E_ROUTING; otherwise the cached connection is dropped so the next call starts fresh.
            ref = p.get("workspace")
            ws = self.mgr.workspaces.get(self._resolve_id(ref) or "") if isinstance(ref, str) else None
            if ws is not None and ws.state == "running":
                self.mgr.check_after_failure(ws)
                raise ManagerError("E_SESSION", f"the connection to workspace {ws.name!r}'s compositor failed; "
                                   "the operation may not have been applied. The connection was reset: try again")
            raise
        if method in AUDITED:
            self.audit.write("request", method=method, workspace=p.get("workspace") if isinstance(
                p.get("workspace"), str) else None, **self._audit_ctx(pr, conn_state))
        return result

    def _own(self, pr: Optional[Principal], ws_id: str) -> bool:
        return pr is None or pr.is_admin or pr.ws_id == ws_id

    def _dispatch(self, method: str, p: Dict[str, Any], conn_state: Dict[str, Any], pr: Optional[Principal]) -> Any:
        m = self.mgr
        if method == "hello":
            return {"api_version": API_VERSION, "daemon_version": __version__,
                    "agent_kinds": {k: v for k, v in AGENT_KINDS.items()}, "uptime": time.time() - self.started,
                    "systemd_scopes": m.use_systemd,
                    "auth": {"required": True, "login": "auth.login", "per_request_field": "token"}}
        if method == "auth.whoami":
            return pr.describe()
        if method == "auth.exchange":
            try:
                if pr.is_admin:
                    wid = self._ws(p).id
                    scopes, ttl = auth.downscope(Principal("workspace", wid, auth.AGENT_DEFAULT_SCOPES, "admin"),
                                                 p.get("scopes"), p.get("ttl_seconds"))
                else:
                    wid = pr.ws_id
                    scopes, ttl = auth.downscope(pr, p.get("scopes"), p.get("ttl_seconds"))
                cid, tok, exp = self.creds.issue(wid, scopes, ttl, label=f"exchange:{pr.cred_id}")
            except AuthError as e:
                raise ManagerError(e.code, e.message)
            return {"token": tok, "credential": cid, "workspace": wid, "scopes": scopes, "expires_at": exp}
        if method.startswith("credential."):
            return self._credential(method, p)
        if method.startswith("grant."):
            return self._grant(method, p, conn_state, pr)
        if method.startswith("browser."):
            return self.browser.handle(method, p, conn_state, pr)
        if method == "audit.tail":
            return {"events": self.audit.tail(int(p.get("n", 50)))}
        if method == "workspace.list":
            allw = [m.summary(w) for w in sorted(m.workspaces.values(), key=lambda w: w.created_at)]
            if pr is not None and not pr.is_admin:
                allw = [w for w in allw if w["id"] == pr.ws_id]      # a workspace credential sees only its own
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
                d["windows"] = self.mgr.winmgr(ws).list() if ws.state == "running" else []
            except ManagerError:
                d["windows"] = []
            return d
        if method == "workspace.create":
            allowed = ("agent_kind", "agent_label", "width", "height", "scale", "gpu", "clipboard",
                       "window_policy", "home", "memory_max", "render_node", "xwayland", "sandbox", "sandbox_mounts", "sandbox_net", "audio")
            kw = {k: p[k] for k in allowed if k in p}
            ws = m.create(p.get("name", ""), start=p.get("start", True), **kw)
            d = m.summary(ws)
            d["token_file"] = self.creds.token_path(ws.id)
            return d
        if method == "workspace.rename":
            return m.summary(m.rename(self._ws(p).id, p.get("name", "")))
        if method == "workspace.assign":
            return m.summary(m.assign(self._ws(p).id, p.get("agent_kind", "none"), p.get("agent_label", "")))
        if method == "workspace.configure":
            ws = self._ws(p)
            keys = ("width", "height", "scale", "gpu", "clipboard", "window_policy", "memory_max", "audio")
            return m.summary(m.configure(ws.id, **{k: p.get(k) for k in keys}))
        if method == "workspace.start":
            return m.summary(m.start(self._ws(p)))
        if method == "workspace.stop":
            m.stop(self._ws(p))
            return {"ok": True}
        if method == "workspace.destroy":
            ws = self._ws(p)
            m.destroy(ws.id)
            self.grants.drop_workspace(ws.id)
            self.browser.drop_workspace(ws.id)
            self.approvals = {k: a for k, a in self.approvals.items() if a.workspace != ws.id}
            return {"ok": True}
        if method in ("workspace.pause", "workspace.takeover"):
            return m.summary(m.set_input_mode(self._ws(p).id, "manual", p.get("reason", "")))
        if method == "workspace.resume":
            return m.summary(m.set_input_mode(self._ws(p).id, "agent"))
        if method == "workspace.usage":
            return m.usage(self._ws(p))
        if method == "workspace.enter":
            return self._enter(p)
        if method == "workspace.launch":
            ws = self._ws(p)
            self._check_assignment(ws, p, pr)
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
            return self._screenshot(p, pr)
        if method == "screen.info":
            ws = self._ws(p)
            self._check_assignment(ws, p, pr)
            info = self._session(ws).display_info()
            info["workspace"] = ws.name
            info["x_display"] = ws.x_display
            return info
        if method == "window.list":
            ws = self._ws(p)
            self._check_assignment(ws, p, pr)
            return {"windows": self.mgr.winmgr(ws).list()}
        if method in INPUT_METHODS:
            return self._input(method, p, pr)
        if method == "clipboard.get":
            ws = self._ws(p)
            self._check_assignment(ws, p, pr)
            return {"text": self._session(ws).get_clipboard()}
        if method == "audio.info":
            ws = self._ws(p)
            self._check_assignment(ws, p, pr)
            return m.audio_info(ws)
        if method == "audio.record":
            ws = self._ws(p)
            self._check_assignment(ws, p, pr)
            path = p.get("path")
            if path:
                path = self._shot_path(ws, str(path), pr)      # a workspace credential writes only inside <ws>/shots
            m.note_action(ws, "audio.record", self._actor(p, pr))
            return m.audio_record(ws, float(p.get("seconds", 3)), path)
        if method == "audio.route":
            ws = self._ws(p)
            channel = self._human_channel(conn_state)
            if channel is None:
                self.audit.write("audio.route_refused", workspace=ws.id, **self._audit_ctx(pr, conn_state))
                raise ManagerError("E_NOT_HUMAN_CHANNEL", "routing a workspace's audio to the host speakers is "
                                   "only accepted from a human channel: run `agent-workspaces audio-route` in an "
                                   "interactive terminal")
            res = m.audio_route(ws, p.get("to", ""), p.get("host_remote"), p.get("sink"))
            self.audit.write("audio.route", workspace=ws.id, to=res["to"], channel=channel)
            return res
        if method == "viewer.attach":
            ws = self._ws(p)
            conn_state.setdefault("viewer_of", []).append(ws.id)
            ws.viewers += 1
            self.bus.emit("viewer.attached", {"id": ws.id, "name": ws.name, "viewers": ws.viewers})
            return {"ok": True, "viewers": ws.viewers}
        if method == "approval.request":
            ws = self._ws(p)
            self._check_assignment(ws, p, pr)
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
                     and self._own(pr, a.workspace)
                     and (not p.get("pending") or a.status == "pending")]
            return {"approvals": items}
        if method == "approval.get":
            a = self.approvals.get(p.get("id"))
            if not a or not self._own(pr, a.workspace):
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

    # ---- credentials (admin only; policy enforced before we get here)
    def _credential(self, method: str, p):
        if method == "credential.rotate" and p.get("admin"):
            self.creds.rotate_admin()
            return {"token_file": self.creds.admin_token_path, "note": "existing admin connections end now"}
        ws = self._ws(p)
        try:
            if method == "credential.list":
                return {"credentials": self.creds.list(ws.id), "token_file": self.creds.token_path(ws.id)}
            if method == "credential.rotate":
                return {"token_file": self.creds.rotate(ws.id)}
            if method == "credential.revoke":
                n = self.creds.revoke(ws.id, p.get("id"), include_default=bool(p.get("all")))
                return {"revoked": n}
            if method == "credential.issue":
                scopes = p.get("scopes") or list(auth.AGENT_DEFAULT_SCOPES)
                cid, tok, exp = self.creds.issue(ws.id, scopes, p.get("ttl_seconds", auth.DEFAULT_TTL),
                                                 label=str(p.get("label", "")))
                return {"credential": cid, "token": tok, "workspace": ws.id, "scopes": scopes,
                        "expires_at": exp}
        except AuthError as e:
            raise ManagerError(e.code, e.message)
        raise ManagerError("E_UNKNOWN_METHOD", f"unknown method {method!r}")

    # ---- human-only grants
    def _grant(self, method: str, p, conn_state, pr: Optional[Principal]):
        try:
            if method == "grant.request":
                ws = self._ws(p)
                g = self.grants.request(ws.id, str(p.get("kind", "")), p.get("resource", ""), p.get("detail", ""))
                return g.to_json()
            if method == "grant.list":
                wid = self._ws(p).id if p.get("workspace") else None
                if pr is not None and not pr.is_admin:
                    wid = pr.ws_id
                return {"grants": self.grants.list(wid, bool(p.get("pending")))}
            if method == "grant.check":
                ws = self._ws(p)
                g = self.grants.is_granted(ws.id, str(p.get("kind", "")), str(p.get("resource", "") or ""))
                return {"granted": g is not None, "expires_at": g.expires if g else None}
            # resolve / revoke: human channel only
            channel = self._human_channel(conn_state)
            if channel is None:
                self.audit.write("grant.refused_channel", method=method, **self._audit_ctx(pr, conn_state))
                raise ManagerError("E_NOT_HUMAN_CHANNEL", f"{method} is only accepted from a human channel: run "
                                   "`agent-workspaces grants approve|deny|revoke` in an interactive terminal "
                                   "or use the viewer")
            if method == "grant.resolve":
                g = self.grants.resolve(str(p.get("id", "")), str(p.get("decision", "")),
                                        p.get("ttl_seconds"), channel)
                return g.to_json()
            if method == "grant.revoke":
                return self.grants.revoke(str(p.get("id", "")), channel).to_json()
        except GrantError as e:
            raise ManagerError(e.code, e.message)
        raise ManagerError("E_UNKNOWN_METHOD", f"unknown method {method!r}")

    def _shot_path(self, ws, path: str, pr: Optional[Principal]) -> str:
        """The admin credential may write a screenshot anywhere it can. A workspace credential may only write
        inside <workspace>/shots (a relative name or an absolute path inside it): an agent must not be able to
        overwrite arbitrary files of the user with a PNG."""
        if pr is None or pr.is_admin:
            return path
        d = paths.ensure_private_dir(os.path.join(ws.dir, "shots"))
        full = path if os.path.isabs(path) else os.path.join(d, path)
        parent = os.path.realpath(os.path.dirname(full))
        if parent != os.path.realpath(d) or os.path.basename(full) in ("", ".", ".."):
            raise ManagerError("E_BAD_ARGS", f"a workspace credential may only write screenshots into {d}")
        return os.path.join(parent, os.path.basename(full))

    def _screenshot(self, p, pr: Optional[Principal] = None):
        ws = self._ws(p)
        self._check_assignment(ws, p, pr)
        s = self._session(ws)
        t0 = time.perf_counter()
        region = p.get("region")
        w, h, rgb = s.capture_raw(region, bool(p.get("cursor")))
        max_w = int(p.get("max_width") or 0)
        if max_w and w > max_w:
            w, h, rgb = _downscale(w, h, rgb, max_w)
        from .session import encode_png
        png = encode_png(w, h, rgb)
        self.mgr.note_action(ws, "screenshot", self._actor(p, pr))
        out: Dict[str, Any] = {"width": w, "height": h, "bytes": len(png), "format": "png",
                               "capture_ms": round((time.perf_counter() - t0) * 1000, 1),
                               "workspace": ws.name, "workspace_id": ws.id}
        path = p.get("path")
        if path:
            path = self._shot_path(ws, str(path), pr)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(png)
            out["path"] = path
        if p.get("inline", not path):
            out["base64"] = base64.b64encode(png).decode()
        return out

    def _input(self, method: str, p, pr: Optional[Principal] = None):
        ws = self._ws(p)
        actor = self._guard_input(ws, p, pr)
        s = self._session(ws)
        kind = method.split(".", 1)[1]
        wres = None
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
            wres = self.mgr.winmgr(ws).action(int(p["window"]), p["action"])
            kind = "window." + p["action"]
        elif method in ("window.move", "window.resize", "window.set_geometry"):
            g = {k: p.get(k) for k in ("x", "y", "width", "height")}
            if method == "window.move":
                g["width"] = g["height"] = None
            elif method == "window.resize":
                g["x"] = g["y"] = None
            wres = self.mgr.winmgr(ws).set_geometry(int(p["window"]), **g)
            kind = "window.geometry"
        elif method == "clipboard.set":
            s.set_clipboard(str(p["text"]))
            kind = "clipboard"
        self.mgr.note_action(ws, kind, actor)
        res = {"ok": True, "pointer": {"x": s.pos[0], "y": s.pos[1]}}
        if method.startswith("window."):
            res["window"] = wres
        return res

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
        try:
            pid, uid, _gid = auth.peer_cred(self.request)
        except OSError:
            return
        state: Dict[str, Any] = {"peer_pid": pid, "viewer_of": []}   # shared with per-request copies
        wlock = threading.Lock()
        sub = None
        pump: Optional[threading.Thread] = None
        q: "queue.Queue" = queue.Queue(maxsize=1000)
        failures = 0

        def send(obj):
            data = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
            with wlock:
                self.wfile.write(data)
                self.wfile.flush()

        def err(rid, code, message):
            send({"v": API_VERSION, "id": rid, "ok": False, "error": {"code": code, "message": message}})

        try:
            while True:
                raw = self.rfile.readline(MAX_LINE + 1)
                if not raw:
                    break
                if len(raw) > MAX_LINE:
                    err(None, "E_BAD_REQUEST", "request too large")
                    break
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    req = json.loads(raw)
                    rid, method, params = req.get("id"), req["method"], req.get("params") or {}
                    if not isinstance(method, str) or not isinstance(params, dict):
                        raise TypeError
                except (ValueError, KeyError, TypeError, AttributeError):
                    err(None, "E_BAD_REQUEST", "invalid JSON request")
                    continue
                try:
                    st = state
                    if req.get("token") is not None:
                        # per-request credential (stateless clients); authenticates this request only
                        st = dict(state)
                        try:
                            st["principal"] = self.daemon.creds.authenticate(req["token"], pid)
                        except AuthError as e:
                            self.daemon.audit.write("auth.failed", code=e.code, peer_pid=pid, method=method)
                            raise ManagerError(e.code, e.message)
                    if method == "events.subscribe":
                        pr = st.get("principal")
                        try:
                            auth.authorize(pr, method, params, self.daemon._resolve_id, self.daemon.creds)
                        except AuthError as e:
                            self.daemon.audit.write("auth.denied", method=method, code=e.code,
                                                    **self.daemon._audit_ctx(pr, st))
                            raise ManagerError(e.code, e.message)
                        if sub is None:
                            ids = []
                            for ref in params.get("workspaces") or []:
                                wid = self.daemon._resolve_id(ref)
                                if pr is not None and not pr.is_admin and wid != pr.ws_id:
                                    raise ManagerError("E_FORBIDDEN", "a workspace credential can only "
                                                       "subscribe to its own workspace")
                                ids.append(wid or ref)
                            if pr is not None and not pr.is_admin:
                                ids = [pr.ws_id]          # always filtered to the bound workspace
                            sub = self.daemon.bus.subscribe(q, ids, params.get("types"))

                            def run(principal=pr):
                                while True:
                                    ev = q.get()
                                    if ev is None:
                                        return
                                    try:
                                        if principal is not None:
                                            self.daemon.creds.still_valid(principal)
                                        send(ev)
                                    except AuthError:
                                        try:
                                            send({"v": API_VERSION, "event": "auth.revoked",
                                                  "data": {"reason": "credential revoked or expired"}})
                                        except OSError:
                                            pass
                                        return
                                    except OSError:
                                        return
                            pump = threading.Thread(target=run, daemon=True)
                            pump.start()
                        send({"v": API_VERSION, "id": rid, "ok": True, "result": {"subscribed": True}})
                        continue
                    result = self.daemon.handle(method, params, st)
                    failures = 0
                    send({"v": API_VERSION, "id": rid, "ok": True, "result": result})
                except ManagerError as e:
                    if e.code in ("E_UNAUTHENTICATED", "E_TOKEN_EXPIRED"):
                        failures += 1
                        time.sleep(min(0.1 * failures, 1.0))      # slow down guessing
                    err(rid, e.code, e.message)
                    if failures >= 10:
                        break
                except WindowError as e:
                    err(rid, e.code, e.message)
                except SessionError as e:
                    err(rid, "E_SESSION", str(e))
                except (KeyError, ValueError, TypeError) as e:
                    err(rid, "E_BAD_ARGS", f"bad arguments: {e!r}")
                except OSError as e:
                    err(rid, "E_IO", str(e))
                except Exception as e:  # keep the daemon alive
                    traceback.print_exc()
                    err(rid, "E_INTERNAL", repr(e))
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
    audit: Optional[auth.AuditLog] = None

    def verify_request(self, request, client_address):
        """Kernel-verified peer check: only processes of the daemon's own user may talk to the API."""
        try:
            pid, uid, _ = auth.peer_cred(request)
        except OSError:
            return False
        if uid != os.getuid():
            if self.audit:
                self.audit.write("auth.refused_uid", peer_uid=uid, peer_pid=pid)
            return False
        return True


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
    srv.audit = d.audit
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
