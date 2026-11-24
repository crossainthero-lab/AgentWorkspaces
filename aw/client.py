"""Client for the agent-workspacesd local API (docs/api.md)."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from typing import Any, Dict, Iterator, Optional

from . import API_VERSION, paths


class ApiError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


class Client:
    def __init__(self, path: Optional[str] = None, autostart: bool = True, timeout: float = 30.0):
        self.path = path or paths.api_socket()
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self._buf = b""
        self._id = 0
        self.autostart = autostart and not os.environ.get("AW_NO_AUTOSTART")

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

    def _spawn_daemon(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = {k: v for k, v in os.environ.items()}
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

    def call(self, method: str, **params) -> Any:
        self.connect()
        self._id += 1
        rid = self._id
        self.sock.sendall((json.dumps({"v": API_VERSION, "id": rid, "method": method,
                                       "params": params}) + "\n").encode())
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

    def events(self, workspaces=None, types=None) -> Iterator[Dict[str, Any]]:
        self.timeout = None
        self.connect()
        self.sock.settimeout(None)
        self.call("events.subscribe", workspaces=workspaces or [], types=types or [])
        while True:
            msg = json.loads(self._readline())
            if "event" in msg:
                yield msg
