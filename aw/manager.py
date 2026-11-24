"""Workspace lifecycle: one headless labwc compositor per workspace, supervised in its own
systemd user scope/slice (when available) so that destroy always removes every process, and
resource use is measurable per workspace.

Isolation boundaries implemented here (see docs/isolation-model.md):
  * own Wayland compositor + seat  -> own pointer, keyboard focus, clipboard, outputs
  * own XDG_RUNTIME_DIR           -> host Wayland/X/D-Bus/PipeWire sockets are not discoverable
  * own session D-Bus (no service dirs) and own HOME by default
  * own systemd slice (cgroup)    -> lifecycle + accounting (+ optional memory cap)
NOT implemented: filesystem, network or syscall confinement. Apps are ordinary user processes.
"""
from __future__ import annotations

import glob
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from . import appenv, paths
from .session import SessionError, WorkspaceSession

AGENT_KINDS = {
    "claude-code": {"label": "Claude Code", "icon_hint": "claude"},
    "codex": {"label": "OpenAI Codex", "icon_hint": "openai"},
    "antigravity": {"label": "Google Antigravity", "icon_hint": "google-antigravity"},
    "hermes": {"label": "Hermes Agent", "icon_hint": "hermes"},
    "other": {"label": "Agent", "icon_hint": "robot"},
    "none": {"label": "Unassigned", "icon_hint": "none"},
}

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,39}$")


class ManagerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _proc_start(pid: int) -> Optional[str]:
    try:
        with open(f"/proc/{pid}/stat") as f:
            s = f.read()
        return s.rsplit(")", 1)[1].split()[19]  # starttime (clock ticks)
    except (OSError, IndexError):
        return None


def _alive(pid: Optional[int], start: Optional[str]) -> bool:
    return bool(pid) and _proc_start(pid) is not None and (start is None or _proc_start(pid) == start)


def find_render_node() -> Optional[str]:
    nodes = sorted(glob.glob("/dev/dri/renderD*"))
    return nodes[0] if nodes else None


def _systemd_available() -> bool:
    return shutil.which("systemd-run") is not None and os.path.exists(
        os.path.join(paths.host_runtime_dir(), "systemd"))


class Workspace:
    """Plain data holder; persisted as ws.json next to the workspace's runtime dir."""

    FIELDS = ("id", "name", "agent", "width", "height", "scale", "gpu", "render_node", "clipboard",
              "window_policy", "home", "memory_max", "state", "input_mode", "compositor_pid",
              "compositor_start", "x_display", "created_at", "apps", "next_app", "xkb_layout",
              "takeover_reason", "last_action", "last_action_at", "last_input_at", "viewers")

    def __init__(self, **kw):
        self.id: str = kw["id"]
        self.name: str = kw["name"]
        self.agent: Dict[str, Any] = kw.get("agent") or {"kind": "none", "label": ""}
        self.width: int = kw.get("width", 1280)
        self.height: int = kw.get("height", 720)
        self.scale: float = kw.get("scale", 1.0)
        self.gpu: str = kw.get("gpu", "auto")          # auto | off
        self.render_node: Optional[str] = kw.get("render_node")
        self.clipboard: str = kw.get("clipboard", "isolated")
        self.window_policy: str = kw.get("window_policy", "maximized")
        self.home: str = kw.get("home", "private")
        self.memory_max: Optional[str] = kw.get("memory_max")
        self.state: str = kw.get("state", "stopped")
        self.input_mode: str = kw.get("input_mode", "agent")   # agent | manual (human takeover)
        self.compositor_pid: Optional[int] = kw.get("compositor_pid")
        self.compositor_start: Optional[str] = kw.get("compositor_start")
        self.x_display: Optional[str] = kw.get("x_display")
        self.created_at: float = kw.get("created_at", time.time())
        self.apps: Dict[str, Dict[str, Any]] = kw.get("apps") or {}
        self.next_app: int = kw.get("next_app", 1)
        self.xkb_layout: str = kw.get("xkb_layout", "us")
        self.takeover_reason: str = kw.get("takeover_reason", "")
        self.last_action: str = kw.get("last_action", "")
        self.last_action_at: float = kw.get("last_action_at", 0.0)
        self.last_input_at: float = kw.get("last_input_at", 0.0)
        self.viewers: int = 0

    def to_meta(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self.FIELDS if k != "viewers"}

    @property
    def dir(self) -> str:
        return paths.ws_dir(self.id)

    @property
    def rt(self) -> str:
        return paths.ws_rt(self.id)

    @property
    def socket(self) -> str:
        return os.path.join(self.rt, "wayland-0")

    @property
    def slice(self) -> str:
        return f"aw-{self.id}.slice"


class Manager:
    def __init__(self, emit: Optional[Callable[[str, Dict[str, Any]], None]] = None):
        self.emit = emit or (lambda t, d: None)
        self.lock = threading.RLock()
        self.workspaces: Dict[str, Workspace] = {}
        self.sessions: Dict[str, WorkspaceSession] = {}
        self.use_systemd = _systemd_available()
        paths.ensure_private_dir(paths.base_dir())
        self._adopt()

    # ---- persistence / adoption
    def _save(self, ws: Workspace):
        os.makedirs(ws.dir, mode=0o700, exist_ok=True)
        tmp = os.path.join(ws.dir, "ws.json.tmp")
        with open(tmp, "w") as f:
            json.dump(ws.to_meta(), f)
        os.replace(tmp, os.path.join(ws.dir, "ws.json"))

    def _adopt(self):
        for meta in glob.glob(os.path.join(paths.base_dir(), "*", "ws.json")):
            try:
                with open(meta) as f:
                    d = json.load(f)
                ws = Workspace(**{k: v for k, v in d.items() if k in Workspace.FIELDS})
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if _alive(ws.compositor_pid, ws.compositor_start) and os.path.exists(ws.socket):
                ws.state = "running"
            else:
                ws.state = "stopped"
                ws.compositor_pid = None
            for n, app in list(ws.apps.items()):
                if not _alive(app.get("pid"), app.get("start")):
                    app["running"] = False
            self.workspaces[ws.id] = ws
            self._save(ws)

    # ---- lookup
    def get(self, ref: str) -> Workspace:
        with self.lock:
            if ref in self.workspaces:
                return self.workspaces[ref]
            for ws in self.workspaces.values():
                if ws.name == ref:
                    return ws
            low = [w for w in self.workspaces.values() if w.name.lower() == str(ref).lower()]
            if len(low) == 1:
                return low[0]
        raise ManagerError("E_NOT_FOUND", f"no workspace named {ref!r}")

    # ---- create / start
    def create(self, name: str, agent_kind: str = "none", agent_label: str = "", width: int = 1280,
               height: int = 720, scale: float = 1.0, gpu: str = "auto", clipboard: str = "isolated",
               window_policy: str = "maximized", home: str = "private", memory_max: Optional[str] = None,
               start: bool = True, render_node: Optional[str] = None) -> Workspace:
        if not NAME_RE.match(name or ""):
            raise ManagerError("E_BAD_NAME", "name must be 1-40 chars: letters, digits, space, _ . -")
        if agent_kind not in AGENT_KINDS:
            raise ManagerError("E_BAD_AGENT", f"agent kind must be one of {sorted(AGENT_KINDS)}")
        if gpu not in ("auto", "off"):
            raise ManagerError("E_BAD_GPU", "gpu must be 'auto' or 'off'")
        if clipboard not in ("isolated", "to-workspace", "from-workspace", "both"):
            raise ManagerError("E_BAD_CLIPBOARD", "clipboard must be isolated|to-workspace|from-workspace|both")
        if window_policy not in ("maximized", "floating"):
            raise ManagerError("E_BAD_POLICY", "window_policy must be maximized|floating")
        if home not in ("private", "shared"):
            raise ManagerError("E_BAD_HOME", "home must be private|shared")
        if not (320 <= width <= 7680 and 240 <= height <= 4320 and 0.5 <= scale <= 4):
            raise ManagerError("E_BAD_SIZE", "width 320-7680, height 240-4320, scale 0.5-4")
        with self.lock:
            if any(w.name.lower() == name.lower() for w in self.workspaces.values()):
                raise ManagerError("E_EXISTS", f"a workspace named {name!r} already exists")
            wid = "w" + secrets.token_hex(3)
            ws = Workspace(id=wid, name=name, width=width, height=height, scale=scale, gpu=gpu,
                           clipboard=clipboard, window_policy=window_policy, home=home,
                           memory_max=memory_max, render_node=render_node,
                           agent={"kind": agent_kind, "label": agent_label or AGENT_KINDS[agent_kind]["label"]})
            self.workspaces[wid] = ws
            paths.ensure_private_dir(ws.dir)
            self._save(ws)
        self.emit("workspace.created", self.summary(ws))
        if start:
            self.start(ws)
        return ws

    def _host_bus_env(self) -> Dict[str, str]:
        rt = paths.host_runtime_dir()
        return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "XDG_RUNTIME_DIR": rt,
                "DBUS_SESSION_BUS_ADDRESS": os.environ.get("DBUS_SESSION_BUS_ADDRESS",
                                                           f"unix:path={rt}/bus"),
                "HOME": os.path.expanduser("~")}

    def _wrap(self, ws: Workspace, unit: str, inner_env: Dict[str, str], argv: List[str],
              cwd: Optional[str] = None, stdout=None) -> subprocess.Popen:
        """Start argv with EXACTLY inner_env (via env -i), inside the workspace's systemd slice."""
        envargs = [f"{k}={v}" for k, v in inner_env.items()]
        cmd = ["/usr/bin/env", "-i", *envargs, *argv]
        if self.use_systemd:
            cmd = ["systemd-run", "--user", "--scope", "--quiet", "--collect", f"--slice={ws.slice}",
                   f"--unit=aw-{ws.id}-{unit}", "-p", "TimeoutStopSec=5"] + \
                  (["-p", f"MemoryMax={ws.memory_max}"] if ws.memory_max and unit == "compositor" else []) + \
                  ["--"] + cmd
            launch_env = self._host_bus_env()      # only to reach the user manager; replaced by env -i
        else:
            launch_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        return subprocess.Popen(cmd, env=launch_env, cwd=cwd, stdin=subprocess.DEVNULL,
                                stdout=stdout or subprocess.DEVNULL, stderr=subprocess.STDOUT,
                                start_new_session=True)

    def _write_config(self, ws: Workspace):
        cfg = os.path.join(ws.dir, "labwc")
        os.makedirs(cfg, mode=0o700, exist_ok=True)
        rule = ('<windowRules><windowRule identifier="*"><action name="Maximize"/></windowRule></windowRules>'
                if ws.window_policy == "maximized" else "")
        with open(os.path.join(cfg, "rc.xml"), "w") as f:
            f.write(f"""<?xml version="1.0"?>
<labwc_config>
  <core><gap>0</gap></core>
  <theme><name>Clearlooks</name><cornerRadius>0</cornerRadius></theme>
  <focus><followMouse>no</followMouse><raiseOnFocus>yes</raiseOnFocus></focus>
  <keyboard><default /><keybind key="A-Tab"><action name="NextWindow"/></keybind>
    <keybind key="A-F4"><action name="Close"/></keybind>
    <keybind key="W-Up"><action name="ToggleMaximize"/></keybind></keyboard>
  <mouse><default /></mouse>
  {rule}
</labwc_config>
""")
        # labwc's default menu/autostart would run host-installed desktop entries; keep both empty.
        open(os.path.join(cfg, "autostart"), "w").close()
        startup = os.path.join(ws.dir, "startup.sh")
        with open(startup, "w") as f:
            f.write(f"""#!/bin/sh
# Runs inside the workspace compositor session. Records the workspace's own XWayland display.
echo "$DISPLAY" > {shlex.quote(ws.dir)}/display.tmp && mv {shlex.quote(ws.dir)}/display.tmp {shlex.quote(ws.dir)}/display
""")
        os.chmod(startup, 0o700)
        with open(os.path.join(ws.dir, "bus.conf"), "w") as f:
            f.write(f"""<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig><type>session</type><keep_umask/>
<listen>unix:path={ws.rt}/bus</listen>
<auth>EXTERNAL</auth>
<policy context="default"><allow send_destination="*" eavesdrop="true"/><allow eavesdrop="true"/>
<allow own="*"/></policy></busconfig>
""")

    def start(self, ws: Workspace) -> Workspace:
        with self.lock:
            if ws.state == "running" and _alive(ws.compositor_pid, ws.compositor_start):
                return ws
            ws.state = "starting"
            # fresh private runtime dir
            shutil.rmtree(ws.rt, ignore_errors=True)
            paths.ensure_private_dir(ws.dir)
            paths.ensure_private_dir(ws.rt)
            for stale in ("display",):
                try:
                    os.unlink(os.path.join(ws.dir, stale))
                except OSError:
                    pass
            self._write_config(ws)
            if ws.home == "private":
                paths.ensure_private_dir(os.path.join(ws.dir, "home"))
            node = ws.render_node or find_render_node()
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.path.join(ws.dir, "home"),
                   "XDG_RUNTIME_DIR": ws.rt, "WLR_BACKENDS": "headless", "WLR_LIBINPUT_NO_DEVICES": "1",
                   "XKB_DEFAULT_LAYOUT": ws.xkb_layout, "LANG": os.environ.get("LANG", "C.UTF-8"),
                   "XDG_CURRENT_DESKTOP": "labwc:wlroots", "GSETTINGS_BACKEND": "memory",
                   "XDG_DATA_DIRS": os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share")}
            if ws.gpu == "auto" and node:
                env.update(WLR_RENDERER="gles2", WLR_RENDER_DRM_DEVICE=node)
            else:
                env.update(WLR_RENDERER="pixman")
            log = open(os.path.join(ws.dir, "compositor.log"), "ab")
            # private session bus (no activatable services: nothing gets spawned behind the app's back)
            bus = self._wrap(ws, "bus", {"PATH": env["PATH"], "HOME": env["HOME"]},
                             ["dbus-daemon", f"--config-file={os.path.join(ws.dir, 'bus.conf')}", "--nofork",
                              "--nopidfile"], stdout=log)
            p = self._wrap(ws, "compositor", env,
                           ["labwc", "-C", os.path.join(ws.dir, "labwc"), "-s", os.path.join(ws.dir, "startup.sh")],
                           stdout=log)
            ws.compositor_pid = p.pid
            ws._bus_pid = bus.pid  # type: ignore[attr-defined]
            threading.Thread(target=lambda: (p.wait(), self._on_compositor_exit(ws, p.pid)),
                             daemon=True).start()
            threading.Thread(target=bus.wait, daemon=True).start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if os.path.exists(ws.socket) and os.path.exists(os.path.join(ws.dir, "display")):
                break
            if p.poll() is not None:
                break
            time.sleep(0.05)
        if p.poll() is not None or not os.path.exists(ws.socket):
            ws.state = "failed"
            self._save(ws)
            self.emit("workspace.state", {"id": ws.id, "name": ws.name, "state": "failed"})
            raise ManagerError("E_START_FAILED", f"compositor failed to start; see {ws.dir}/compositor.log")
        with self.lock:
            ws.compositor_start = _proc_start(p.pid)
            dsp = open(os.path.join(ws.dir, "display")).read().strip() if os.path.exists(
                os.path.join(ws.dir, "display")) else ""
            ws.x_display = dsp or None
            ws.state = "running"
            self._save(ws)
        try:
            s = self.session(ws)
            if (ws.width, ws.height, ws.scale) != (1280, 720, 1.0):
                s.set_display(ws.width, ws.height, ws.scale)
            else:
                s.set_display(1280, 720, 1.0)
        except (SessionError, Exception) as e:  # keep running even if resize failed
            self.emit("workspace.warning", {"id": ws.id, "message": f"initial display setup: {e}"})
        self.emit("workspace.state", {"id": ws.id, "name": ws.name, "state": "running"})
        return ws

    def _on_compositor_exit(self, ws: Workspace, pid: int):
        with self.lock:
            if ws.compositor_pid != pid:
                return
            sess = self.sessions.pop(ws.id, None)
            ws.compositor_pid = None
            ws.state = "stopped" if ws.state != "failed" else "failed"
            self._save(ws)
        if sess:
            sess.close()
        self.emit("workspace.state", {"id": ws.id, "name": ws.name, "state": ws.state})

    # ---- stop / destroy
    def stop(self, ws: Workspace):
        with self.lock:
            sess = self.sessions.pop(ws.id, None)
            if sess:
                sess.close()
            ws.state = "stopping"
        if self.use_systemd:
            subprocess.run(["systemctl", "--user", "stop", ws.slice], env=self._host_bus_env(),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        pids = [ws.compositor_pid] + [a.get("pid") for a in ws.apps.values()]
        for pid in pids:
            if pid and _proc_start(pid):
                try:
                    os.killpg(pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(p and _proc_start(p) for p in pids):
            time.sleep(0.1)
        for pid in pids:  # last resort, exact pids we started
            if pid and _proc_start(pid):
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        with self.lock:
            ws.state = "stopped"
            ws.compositor_pid = None
            for a in ws.apps.values():
                a["running"] = False
            self._save(ws)
        self.emit("workspace.state", {"id": ws.id, "name": ws.name, "state": "stopped"})

    def destroy(self, ref: str):
        ws = self.get(ref)
        self.stop(ws)
        with self.lock:
            self.workspaces.pop(ws.id, None)
        shutil.rmtree(ws.dir, ignore_errors=True)
        self.emit("workspace.destroyed", {"id": ws.id, "name": ws.name})

    # ---- mutation
    def rename(self, ref: str, new_name: str) -> Workspace:
        ws = self.get(ref)
        if not NAME_RE.match(new_name or ""):
            raise ManagerError("E_BAD_NAME", "invalid name")
        with self.lock:
            if any(w.name.lower() == new_name.lower() and w.id != ws.id for w in self.workspaces.values()):
                raise ManagerError("E_EXISTS", f"a workspace named {new_name!r} already exists")
            old = ws.name
            ws.name = new_name
            self._save(ws)
        self.emit("workspace.renamed", {"id": ws.id, "old": old, "name": new_name})
        return ws

    def assign(self, ref: str, kind: str, label: str = "") -> Workspace:
        ws = self.get(ref)
        if kind not in AGENT_KINDS:
            raise ManagerError("E_BAD_AGENT", f"agent kind must be one of {sorted(AGENT_KINDS)}")
        with self.lock:
            ws.agent = {"kind": kind, "label": label or AGENT_KINDS[kind]["label"]}
            self._save(ws)
        self.emit("workspace.assigned", {"id": ws.id, "name": ws.name, "agent": ws.agent})
        return ws

    def set_input_mode(self, ref: str, mode: str, reason: str = "") -> Workspace:
        ws = self.get(ref)
        if mode not in ("agent", "manual"):
            raise ManagerError("E_BAD_MODE", "mode must be agent|manual")
        with self.lock:
            changed = ws.input_mode != mode
            ws.input_mode = mode
            ws.takeover_reason = reason if mode == "manual" else ""
            self._save(ws)
        if changed:
            self.emit("workspace.input_mode", {"id": ws.id, "name": ws.name, "input_mode": mode, "reason": reason})
        return ws

    def configure(self, ref: str, **opts) -> Workspace:
        ws = self.get(ref)
        restart = False
        with self.lock:
            for k in ("clipboard", "window_policy", "memory_max"):
                if k in opts and opts[k] is not None:
                    setattr(ws, k, opts[k])
                    restart = restart or k in ("window_policy", "memory_max")
            if "gpu" in opts and opts["gpu"] is not None:
                if opts["gpu"] not in ("auto", "off"):
                    raise ManagerError("E_BAD_GPU", "gpu must be 'auto' or 'off'")
                restart = restart or opts["gpu"] != ws.gpu
                ws.gpu = opts["gpu"]
            self._save(ws)
        if any(opts.get(k) is not None for k in ("width", "height", "scale")):
            w = int(opts.get("width") or ws.width)
            h = int(opts.get("height") or ws.height)
            sc = float(opts.get("scale") or ws.scale)
            if not (320 <= w <= 7680 and 240 <= h <= 4320 and 0.5 <= sc <= 4):
                raise ManagerError("E_BAD_SIZE", "width 320-7680, height 240-4320, scale 0.5-4")
            if ws.state == "running":
                self.session(ws).set_display(w, h, sc)
            ws.width, ws.height, ws.scale = w, h, sc
            self._save(ws)
        if restart and ws.state == "running":
            # gpu/window_policy/memory change needs a compositor restart; apps are closed.
            self.stop(ws)
            self.start(ws)
        self.emit("workspace.configured", self.summary(ws))
        return ws

    # ---- sessions
    def session(self, ws: Workspace) -> WorkspaceSession:
        with self.lock:
            s = self.sessions.get(ws.id)
            if s is not None and s.alive:
                return s
            if ws.state != "running":
                raise ManagerError("E_NOT_RUNNING", f"workspace {ws.name!r} is {ws.state}")
            try:
                s = WorkspaceSession(ws.socket, ws.xkb_layout)
            except Exception as e:
                raise ManagerError("E_SESSION", f"cannot connect to workspace compositor: {e}")
            self.sessions[ws.id] = s
            return s

    # ---- apps
    def launch(self, ws: Workspace, argv: List[str], cwd: Optional[str] = None,
               extra_env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        if not argv:
            raise ManagerError("E_BAD_ARGS", "empty command")
        if ws.state != "running":
            raise ManagerError("E_NOT_RUNNING", f"workspace {ws.name!r} is {ws.state}")
        try:
            display = ws.x_display
            base = appenv.build_env(ws.rt, x_display=display, bus_address=f"unix:path={ws.rt}/bus",
                                    extra=extra_env)
            appenv.assert_no_host_sockets(base, paths.host_runtime_dir())
        except appenv.WorkspaceNotRunning as e:
            raise ManagerError("E_NOT_RUNNING", str(e))
        except ValueError as e:
            raise ManagerError("E_BAD_ENV", str(e))
        if ws.home == "private":
            base["HOME"] = os.path.join(ws.dir, "home")
        base["GSETTINGS_BACKEND"] = "memory"
        base["NO_AT_BRIDGE"] = "1"
        base["GTK_USE_PORTAL"] = "0"
        if ws.gpu == "off":
            base["LIBGL_ALWAYS_SOFTWARE"] = "1"
            base["GSK_RENDERER"] = "cairo"
        exe = argv[0]
        if os.sep not in exe:
            found = shutil.which(exe, path=base["PATH"])
            if not found:
                raise ManagerError("E_NO_SUCH_PROGRAM", f"{exe!r} not found in PATH")
            argv = [found] + list(argv[1:])
        with self.lock:
            n = ws.next_app
            ws.next_app += 1
        logp = os.path.join(ws.dir, f"app-{n}.log")
        lf = open(logp, "ab")
        cwd = cwd or (base["HOME"] if os.path.isdir(base["HOME"]) else "/")
        p = self._wrap(ws, f"app{n}", base, argv, cwd=cwd, stdout=lf)
        app = {"n": n, "pid": p.pid, "argv": argv, "started": time.time(), "log": logp, "running": True,
               "start": None}
        time.sleep(0.05)
        app["start"] = _proc_start(p.pid)
        with self.lock:
            ws.apps[str(n)] = app
            self._save(ws)

        def reap():
            code = p.wait()
            with self.lock:
                a = ws.apps.get(str(n))
                if a:
                    a["running"] = False
                    a["exit_code"] = code
                    self._save(ws)
            self.emit("app.exited", {"workspace": ws.name, "n": n, "exit_code": code})
        threading.Thread(target=reap, daemon=True).start()
        self.emit("app.started", {"workspace": ws.name, "n": n, "argv": argv})
        return {"app": n, "pid": p.pid, "log": logp, "argv": argv}

    def kill_app(self, ws: Workspace, n: int):
        app = ws.apps.get(str(n))
        if not app:
            raise ManagerError("E_NOT_FOUND", f"no app {n}")
        pid = app.get("pid")
        if pid and _alive(pid, app.get("start")):
            try:
                os.killpg(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGTERM)

    # ---- reporting
    def usage(self, ws: Workspace) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        if self.use_systemd and ws.state == "running":
            try:
                r = subprocess.run(["systemctl", "--user", "show", ws.slice, "-p",
                                    "MemoryCurrent,CPUUsageNSec,TasksCurrent"], env=self._host_bus_env(),
                                   capture_output=True, text=True, timeout=5)
                for line in r.stdout.splitlines():
                    k, _, v = line.partition("=")
                    if v.isdigit():
                        out[{"MemoryCurrent": "memory_bytes", "CPUUsageNSec": "cpu_ns",
                             "TasksCurrent": "tasks"}[k]] = int(v)
            except (subprocess.SubprocessError, OSError):
                pass
        return out

    def summary(self, ws: Workspace) -> Dict[str, Any]:
        kind = ws.agent.get("kind", "none")
        wins: List[Dict[str, Any]] = []
        s = self.sessions.get(ws.id)
        if s is not None and s.alive:
            try:
                wins = s.toplevels_snapshot()
            except Exception:
                wins = []
        focused = next((w for w in wins if w.get("focused")), None)
        now = time.time()
        return {
            "id": ws.id, "name": ws.name, "state": ws.state,
            "agent": {"kind": kind, "label": ws.agent.get("label", ""),
                      "icon_hint": AGENT_KINDS.get(kind, AGENT_KINDS["other"])["icon_hint"]},
            "input_mode": ws.input_mode, "takeover_reason": ws.takeover_reason,
            "display": {"width": ws.width, "height": ws.height, "scale": ws.scale},
            "gpu": ws.gpu, "clipboard": ws.clipboard, "window_policy": ws.window_policy, "home": ws.home,
            "activity": {"last_action": ws.last_action, "last_action_at": ws.last_action_at or None,
                         "idle_seconds": (now - ws.last_action_at) if ws.last_action_at else None,
                         "window_count": len(wins), "focused_window": focused["title"] if focused else None},
            "apps": [{"n": a["n"], "pid": a["pid"], "argv": a["argv"], "running": a.get("running", False)}
                     for a in ws.apps.values()],
            "viewers": ws.viewers, "created_at": ws.created_at,
        }

    def note_action(self, ws: Workspace, kind: str, actor: str):
        now = time.time()
        ws.last_action, ws.last_action_at = f"{actor}:{kind}", now
        if kind in ("click", "move", "drag", "type", "key", "scroll"):
            ws.last_input_at = now
        self.emit("workspace.activity", {"id": ws.id, "name": ws.name, "actor": actor, "action": kind, "at": now})

    def shutdown_sessions(self):
        with self.lock:
            for s in self.sessions.values():
                s.close()
            self.sessions.clear()
