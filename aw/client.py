"""Client for the agent-workspacesd local API (docs/api.md).

Credentials are picked up automatically, callers never hand-roll sockets or tokens. Lookup order
(`aw.auth.find_credentials`, documented in docs/api.md):

  role="agent"  explicit `token=`, AW_TOKEN, AW_TOKEN_FILE, <base>/<workspace>/token for the workspace being
                addressed (call parameter `workspace`, else AW_WORKSPACE), then the token files of the workspaces
                assigned to AW_AGENT_KIND.
  role="admin"  explicit `token=`, AW_ADMIN_TOKEN, AW_ADMIN_TOKEN_FILE, <base>/admin.token  (the human).
  role="auto"   (default) agent tiers 1-3 when any is set, so a configured agent never silently becomes admin;
                otherwise the admin tiers; otherwise the agent token files.

The token travels in the `token` field of each request (never in argv or in the environment of children),
and is re-read from its file when the daemon says it was rotated.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from typing import Any, Dict, Iterator, List, Optional

from . import API_VERSION, auth, paths

# Calls that are safe to repeat after a lost connection (no side effect). Anything else is never resent
# once it may have been delivered.
READ_ONLY = {"hello", "auth.whoami", "workspace.list", "workspace.get", "workspace.status", "workspace.usage",
             "app.list", "screen.screenshot", "screen.info", "window.list", "clipboard.get", "approval.list",
             "approval.get", "grant.list", "grant.check", "credential.list"}


class ApiError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


class Client:
    def __init__(self, path: Optional[str] = None, autostart: bool = True, timeout: float = 30.0,
                 token: Optional[str] = None, role: str = "auto", workspace: Optional[str] = None):
        self.path = path or paths.api_socket()
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self._buf = b""
        self._id = 0
        self.autostart = autostart and not os.environ.get("AW_NO_AUTOSTART")
        self.token, self.role, self.workspace = token, role, workspace
        self.base = os.path.dirname(self.path)
        self.last_credential: Optional[auth.Credential] = None

    # ---- credentials
    def credentials(self, workspace: Optional[str] = None) -> List[auth.Credential]:
        try:
            return auth.find_credentials(self.base, self.role, workspace or self.workspace, explicit=self.token)
        except auth.AuthError as e:
            raise ApiError(e.code, e.message)

    def _no_credentials(self, workspace: Optional[str]) -> ApiError:
        where = f" for workspace {workspace!r}" if workspace else ""
        return ApiError("E_NO_CREDENTIALS", f"no credential found{where}. Agents: set AW_TOKEN_FILE (or AW_TOKEN) "
                        "to the workspace's token file; the human CLI uses <base>/admin.token. "
                        "See docs/api.md, 'Credential lookup'.")

    # ---- connection
    def connect(self) -> "Client":
        if self.sock:
            return self
        try:
            self._open()
        except OSError:
            if not self.autostart:
                raise ApiError("E_NO_DAEMON", f"agent-workspacesd is not running (socket {self.path})")
            self._spawn_daemon()
            self._open()
        return self

    def _open(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.path)
        self.sock = s
        self._buf = b""

    def _spawn_daemon(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = {k: v for k, v in os.environ.items() if not k.startswith("AW_TOKEN") and not k.startswith("AW_ADMIN")}
        env["PYTHONPATH"] = root + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        log = os.path.join(paths.ensure_private_dir(paths.base_dir()), "daemon.log")
        subprocess.Popen([sys.executable, "-m", "aw.daemon"], env=env, stdin=subprocess.DEVNULL,
                         stdout=open(log, "ab"), stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if os.path.exists(self.path):
                try:
                    t = socket.socket(socket.AF_UNIX)
                    t.connect(self.path)
                    t.close()
                    return
                except OSError:
                    pass
            time.sleep(0.05)
        raise ApiError("E_NO_DAEMON", f"could not start agent-workspacesd (see {log})")

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    def _readline(self) -> bytes:
        while b"\n" not in self._buf:
            chunk = self.sock.recv(1 << 20)
            if not chunk:
                raise ApiError("E_DISCONNECTED", "daemon closed the connection")
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        return line

    # ---- calls
    def _send(self, method: str, params: Dict[str, Any], token: Optional[str]) -> int:
        self._id += 1
        req: Dict[str, Any] = {"v": API_VERSION, "id": self._id, "method": method, "params": params}
        if token:
            req["token"] = token
        self.sock.sendall((json.dumps(req) + "\n").encode())
        return self._id

    def _roundtrip(self, method: str, params: Dict[str, Any], token: Optional[str]) -> Any:
        rid = self._send(method, params, token)
        while True:
            msg = json.loads(self._readline())
            if "event" in msg:
                continue  # events are only consumed through events()
            if msg.get("id") != rid:
                continue
            if msg.get("ok"):
                return msg["result"]
            e = msg.get("error") or {}
            raise ApiError(e.get("code", "E_UNKNOWN"), e.get("message", ""))

    def call(self, method: str, **params) -> Any:
        self.connect()
        if method == "hello":
            return self._with_reconnect(method, params, None)
        ws = params.get("workspace") if isinstance(params.get("workspace"), str) else None
        if method == "workspace.list" and self.role != "admin" and not self.token:
            return self._list_merged(params)
        creds = self.credentials(ws)
        if not creds:
            raise self._no_credentials(ws)
        last: Optional[ApiError] = None
        for i, cred in enumerate(creds):
            try:
                self.last_credential = cred
                return self._with_reconnect(method, params, cred.token)
            except ApiError as e:
                last = e
                if e.code == "E_UNAUTHENTICATED" and i + 1 < len(creds):
                    continue          # e.g. rotated: try the next source
                raise
        raise last or self._no_credentials(ws)

    def _with_reconnect(self, method, params, token):
        try:
            return self._roundtrip(method, params, token)
        except (BrokenPipeError, ConnectionResetError, ApiError, socket.timeout) as e:
            lost = isinstance(e, (BrokenPipeError, ConnectionResetError)) or (
                isinstance(e, ApiError) and e.code == "E_DISCONNECTED")
            if not lost:
                raise
            self.close()
            if method not in READ_ONLY:
                raise ApiError("E_DISCONNECTED", "connection to the daemon was lost during a call that changes "
                               "state; its outcome is unknown. Check the workspace before repeating it.")
            self.connect()          # idempotent call: safe to repeat on a fresh connection (daemon restart)
            return self._roundtrip(method, params, token)

    def _list_merged(self, params) -> Any:
        """workspace.list with workspace credentials: one call per credential, merged (each only ever sees the
        workspace it is bound to)."""
        creds = self.credentials(None)
        if not creds:
            raise self._no_credentials(None)
        if creds[0].kind == "admin":
            return self._with_reconnect("workspace.list", params, creds[0].token)
        out: List[Dict[str, Any]] = []
        errors: List[ApiError] = []
        for c in creds:
            try:
                out.extend(self._with_reconnect("workspace.list", params, c.token)["workspaces"])
            except ApiError as e:
                errors.append(e)
        if not out and errors:
            raise errors[0]
        seen, uniq = set(), []
        for w in out:
            if w["id"] not in seen:
                seen.add(w["id"])
                uniq.append(w)
        return {"workspaces": uniq}

    def subscribe(self, workspaces=None, types=None):
        """Turn this connection into an event stream and wait for the daemon's acknowledgement (so nothing that
        happens afterwards can be missed); read with next_event()."""
        self.timeout = None
        self.connect()
        self.sock.settimeout(None)
        ws0 = workspaces[0] if workspaces else None
        creds = self.credentials(ws0)
        if not creds:
            raise self._no_credentials(ws0)
        rid = self._send("events.subscribe", {"workspaces": workspaces or [], "types": types or []}, creds[0].token)
        while True:
            msg = json.loads(self._readline())
            if msg.get("id") == rid:
                if not msg.get("ok", True):
                    e = msg.get("error") or {}
                    raise ApiError(e.get("code", "E_UNKNOWN"), e.get("message", ""))
                return

    def next_event(self) -> Dict[str, Any]:
        """Block until the next event arrives (ApiError E_DISCONNECTED when the daemon goes away)."""
        while True:
            msg = json.loads(self._readline())
            if "event" in msg:
                return msg

    def events(self, workspaces=None, types=None) -> Iterator[Dict[str, Any]]:
        self.timeout = None
        self.connect()
        self.sock.settimeout(None)
        creds = self.credentials(workspaces[0] if workspaces else None)
        if not creds:
            raise self._no_credentials(workspaces[0] if workspaces else None)
        rid = self._send("events.subscribe", {"workspaces": workspaces or [], "types": types or []},
                         creds[0].token)
        while True:
            msg = json.loads(self._readline())
            if msg.get("id") == rid and not msg.get("ok", True):
                e = msg.get("error") or {}
                raise ApiError(e.get("code", "E_UNKNOWN"), e.get("message", ""))
            if "event" in msg:
                yield msg
