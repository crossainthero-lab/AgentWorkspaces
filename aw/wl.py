"""Minimal pure-Python Wayland client (stdlib only).

Speaks the wire protocol directly over a unix socket and builds its interface tables from
protocol XML files (shipped in aw/protocols). It exists so that AgentWorkspaces needs no
compiled helper and no pip dependency to drive a workspace compositor.

SAFETY: a Connection is only ever created from an explicit socket path. It never reads
WAYLAND_DISPLAY / XDG_RUNTIME_DIR from the environment, so it cannot reach the user's
personal compositor by accident.
"""
from __future__ import annotations

import array
import mmap
import os
import select
import socket
import struct
import time
import xml.etree.ElementTree as ET
from typing import Any, Callable, Dict, List, Optional

PROTO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "protocols")


class WaylandError(Exception):
    pass


class PeerMismatch(WaylandError):
    """The process behind the socket is not the one we expected (checked before any byte is exchanged)."""


class Arg:
    __slots__ = ("name", "type", "interface", "allow_null")

    def __init__(self, name, type_, interface, allow_null):
        self.name, self.type, self.interface, self.allow_null = name, type_, interface, allow_null


class Message:
    __slots__ = ("name", "opcode", "args", "since")

    def __init__(self, name, opcode, args, since):
        self.name, self.opcode, self.args, self.since = name, opcode, args, since


class Interface:
    def __init__(self, name, version):
        self.name, self.version = name, version
        self.requests: Dict[str, Message] = {}
        self.events_by_opcode: Dict[int, Message] = {}
        self.enums: Dict[str, Dict[str, int]] = {}


_INTERFACES: Dict[str, Interface] = {}
_LOADED: set = set()


def load_protocol(path: str) -> None:
    path = os.path.abspath(path)
    if path in _LOADED:
        return
    _LOADED.add(path)
    root = ET.parse(path).getroot()
    for iface_el in root.findall("interface"):
        iface = Interface(iface_el.get("name"), int(iface_el.get("version", "1")))
        req_op = ev_op = 0
        for el in iface_el:
            if el.tag in ("request", "event"):
                args = [Arg(a.get("name"), a.get("type"), a.get("interface"),
                            a.get("allow-null") == "true") for a in el.findall("arg")]
                msg = Message(el.get("name"), req_op if el.tag == "request" else ev_op,
                              args, int(el.get("since", "1")))
                if el.tag == "request":
                    iface.requests[msg.name] = msg
                    req_op += 1
                else:
                    iface.events_by_opcode[msg.opcode] = msg
                    ev_op += 1
            elif el.tag == "enum":
                iface.enums[el.get("name")] = {
                    e.get("name"): int(e.get("value"), 0) for e in el.findall("entry")}
        _INTERFACES[iface.name] = iface


def load_all_protocols(directory: str = PROTO_DIR) -> None:
    for fn in sorted(os.listdir(directory)):
        if fn.endswith(".xml"):
            load_protocol(os.path.join(directory, fn))


class Proxy:
    """A bound protocol object. Call requests as methods; set handlers via .on(event, fn)."""

    def __init__(self, conn: "Connection", oid: int, iface: Interface, version: int):
        self._conn, self.id, self.iface, self.version = conn, oid, iface, version
        self._handlers: Dict[str, Callable] = {}
        self.dead = False

    def on(self, event: str, fn: Callable) -> "Proxy":
        self._handlers[event] = fn
        return self

    def __getattr__(self, name: str):
        iface = object.__getattribute__(self, "iface")
        msg = iface.requests.get(name)
        if msg is None:
            raise AttributeError(f"{iface.name} has no request {name!r}")

        def call(*args):
            return self._conn._send_request(self, msg, args)
        return call

    def __repr__(self):
        return f"<{self.iface.name}@{self.id}>"


class Connection:
    def __init__(self, socket_path: Optional[str] = None, sock: Optional[socket.socket] = None,
                 expect_pid: Optional[int] = None):
        load_all_protocols()
        if sock is None:
            if not socket_path or not os.path.isabs(socket_path):
                raise WaylandError("socket path must be absolute (never resolved from the environment)")
            if not os.path.exists(socket_path):
                raise WaylandError(f"workspace compositor socket missing: {socket_path}")
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(socket_path)
            if expect_pid is not None:
                import struct
                peer = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                           struct.calcsize("3i")))[0]
                if peer != expect_pid:
                    sock.close()
                    raise PeerMismatch(f"socket {socket_path} is served by pid {peer}, expected {expect_pid}")
        self.path = socket_path
        self.sock = sock
        self.objects: Dict[int, Proxy] = {}
        self._next_id = 2
        self._rbuf = b""
        self._fds: List[int] = []
        self.display = self._register(Proxy(self, 1, _INTERFACES["wl_display"], 1), 1)
        self.display.on("error", self._on_error)
        self.display.on("delete_id", self._on_delete_id)
        self.globals: Dict[str, tuple] = {}
        self._registry = self.get_registry()

    # ---- object management
    def _register(self, proxy: Proxy, oid: int) -> Proxy:
        self.objects[oid] = proxy
        return proxy

    def _new_proxy(self, iface_name: str, version: int) -> Proxy:
        oid = self._next_id
        self._next_id += 1
        return self._register(Proxy(self, oid, _INTERFACES[iface_name], version), oid)

    def _on_error(self, obj, code, message):
        raise WaylandError(f"protocol error on {obj}: code={code} {message}")

    def _on_delete_id(self, oid):
        self.objects.pop(oid, None)

    # ---- encoding
    def _send_request(self, proxy: Proxy, msg: Message, args: tuple):
        payload = b""
        fds: List[int] = []
        result = None
        ai = 0
        for a in msg.args:
            if a.type == "new_id":
                if a.interface:
                    iname = a.interface
                    ver = min(_INTERFACES[iname].version, max(proxy.version, 1))
                    result = self._new_proxy(iname, ver)
                    payload += struct.pack("<I", result.id)
                else:  # untyped: (interface, version, id)
                    iname, ver = args[ai], args[ai + 1]
                    ai += 2
                    result = self._new_proxy(iname, ver)
                    s = iname.encode() + b"\0"
                    payload += struct.pack("<I", len(s)) + s + b"\0" * (-len(s) % 4)
                    payload += struct.pack("<II", ver, result.id)
                continue
            v = args[ai]
            ai += 1
            if a.type == "int":
                payload += struct.pack("<i", v)
            elif a.type == "uint":
                payload += struct.pack("<I", v)
            elif a.type == "fixed":
                payload += struct.pack("<i", int(round(v * 256)))
            elif a.type == "string":
                if v is None:
                    payload += struct.pack("<I", 0)
                else:
                    s = v.encode() + b"\0"
                    payload += struct.pack("<I", len(s)) + s + b"\0" * (-len(s) % 4)
            elif a.type == "object":
                payload += struct.pack("<I", 0 if v is None else v.id)
            elif a.type == "array":
                payload += struct.pack("<I", len(v)) + bytes(v) + b"\0" * (-len(v) % 4)
            elif a.type == "fd":
                fds.append(v)
            else:
                raise WaylandError(f"unsupported arg type {a.type}")
        hdr = struct.pack("<II", proxy.id, ((8 + len(payload)) << 16) | msg.opcode)
        data = hdr + payload
        if fds:
            self.sock.sendmsg([data], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                        array.array("i", fds))])
        else:
            self.sock.sendall(data)
        if msg.name == "destroy" or msg.name == "release":
            proxy.dead = True
        return result

    # ---- decoding / dispatch
    def _read_some(self, timeout: Optional[float]) -> bool:
        r, _, _ = select.select([self.sock], [], [], timeout)
        if not r:
            return False
        data, anc, _, _ = self.sock.recvmsg(65536, socket.CMSG_SPACE(16 * 4))
        for level, typ, cdata in anc:
            if level == socket.SOL_SOCKET and typ == socket.SCM_RIGHTS:
                a = array.array("i")
                a.frombytes(cdata[:len(cdata) - (len(cdata) % a.itemsize)])
                self._fds.extend(a)
        if not data:
            raise WaylandError("compositor closed the connection")
        self._rbuf += data
        return True

    def _dispatch_buffered(self) -> int:
        n = 0
        while len(self._rbuf) >= 8:
            oid, w2 = struct.unpack_from("<II", self._rbuf)
            size, opcode = w2 >> 16, w2 & 0xFFFF
            if len(self._rbuf) < size:
                break
            body = self._rbuf[8:size]
            self._rbuf = self._rbuf[size:]
            self._handle_event(oid, opcode, body)
            n += 1
        return n

    def _handle_event(self, oid: int, opcode: int, body: bytes):
        proxy = self.objects.get(oid)
        if proxy is None:
            # Event for an object we already destroyed: skip any fds it carried.
            return
        msg = proxy.iface.events_by_opcode.get(opcode)
        if msg is None:
            return
        pos = 0
        vals: List[Any] = []
        for a in msg.args:
            t = a.type
            if t in ("int", "fixed"):
                (v,) = struct.unpack_from("<i", body, pos)
                pos += 4
                if t == "fixed":
                    v = v / 256.0
            elif t == "uint":
                (v,) = struct.unpack_from("<I", body, pos)
                pos += 4
            elif t == "string":
                (ln,) = struct.unpack_from("<I", body, pos)
                pos += 4
                v = None if ln == 0 else body[pos:pos + ln - 1].decode("utf-8", "replace")
                pos += (ln + 3) & ~3
            elif t == "array":
                (ln,) = struct.unpack_from("<I", body, pos)
                pos += 4
                v = bytes(body[pos:pos + ln])
                pos += (ln + 3) & ~3
            elif t == "object":
                (i,) = struct.unpack_from("<I", body, pos)
                pos += 4
                v = self.objects.get(i) if i else None
            elif t == "new_id":
                (i,) = struct.unpack_from("<I", body, pos)
                pos += 4
                v = self._register(Proxy(self, i, _INTERFACES[a.interface], proxy.version), i)
            elif t == "fd":
                v = self._fds.pop(0)
            else:
                raise WaylandError(f"unsupported event arg {t}")
            vals.append(v)
        h = proxy._handlers.get(msg.name)
        if h:
            h(*vals)

    # ---- public API
    def dispatch(self, timeout: Optional[float] = 0) -> int:
        """Read what is available (waiting up to timeout) and dispatch events."""
        n = self._dispatch_buffered()
        if n:
            return n
        if self._read_some(timeout):
            return self._dispatch_buffered()
        return 0

    def roundtrip(self, timeout: float = 5.0) -> None:
        done = []
        cb = self.display.sync()
        cb.on("done", lambda *_: done.append(1))
        deadline = time.monotonic() + timeout
        while not done:
            left = deadline - time.monotonic()
            if left <= 0:
                raise WaylandError("roundtrip timed out")
            # short slices: another thread that dispatches on this connection may consume the "done" event, and
            # waiting out the whole timeout for more data would then stall (and not even fail) for `timeout` s
            self.dispatch(min(left, 0.2))

    def get_registry(self) -> Proxy:
        reg = self.display.get_registry()
        reg.on("global", lambda name, iface, ver: self.globals.__setitem__(iface, (name, ver)))
        self.roundtrip()
        return reg

    def bind(self, iface_name: str, max_version: int = 1) -> Optional[Proxy]:
        g = self.globals.get(iface_name)
        if g is None:
            return None
        ver = min(g[1], max_version, _INTERFACES[iface_name].version)
        return self._registry.bind(g[0], iface_name, ver)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def make_shm_file(size: int):
    fd = os.memfd_create("aw-shm", os.MFD_CLOEXEC)
    os.ftruncate(fd, size)
    return fd, mmap.mmap(fd, size)
