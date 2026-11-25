"""A private, headless GNOME Shell session for AgentWorkspaces tests, screenshots and recordings.

Everything lives under one private base directory in /run/user/UID:
  * env built from scratch (no host display, bus or runtime dir), private HOME / XDG_* dirs
  * GSETTINGS_BACKEND=keyfile: the user's dconf is never written (optionally their *extension settings* are read
    once, read-only, with `dconf dump`, and loaded into the PRIVATE keyfile)
  * a private D-Bus session bus AND a private stand-in "system" bus, so GDM, logind and polkit are unreachable
  * extensions: this extension and a test probe are installed into the private XDG_DATA_HOME only; other
    extensions are read-only COPIES of the locally installed ones, enabled only in this session
  * input is synthesised with Clutter virtual devices INSIDE that shell; nothing is sent to any other session
Teardown stops processes by exact handle, then reaps anything still carrying the private XDG_RUNTIME_DIR
(matched on that exact environment entry, never by name).

Modelled on the sibling Gnotch test harness (shellsession.py / realistic.py); written for this repository.
"""
import glob
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time

import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
EXT_SRC = os.path.dirname(HERE)
UUID = "agent-workspaces@agentworkspaces.dev"
PROBE = "probe@agentworkspaces.test"
REAL_EXT_DIR = os.path.expanduser("~/.local/share/gnome-shell/extensions")
DISPLAY_NAME = "aw-gs"

# The GNOME defaults of the machine this was developed on (read with gsettings, applied only to the private keyfile)
WM_KEYS = """[org/gnome/desktop/wm/keybindings]
switch-to-workspace-up=['<Control><Alt>Up']
switch-to-workspace-down=['<Control><Alt>Down']
switch-to-workspace-left=['<Super>Page_Up', '<Super>KP_Prior', '<Super><Alt>Left', '<Control><Alt>Left']
switch-to-workspace-right=['<Super>Page_Down', '<Super>KP_Next', '<Super><Alt>Right', '<Control><Alt>Right']
"""

# What the developer's own session enables (extension code is copied, settings are dumped read-only)
REALISTIC_COPY = ["desktop-cube@schneegans.github.com", "tilingshell@ferrarodomenico.com", "blur-my-shell@aunetx",
                  "just-perfection-desktop@just-perfection", "user-theme@gnome-shell-extensions.gcampax.github.com"]
REALISTIC_SYSTEM = ["ubuntu-dock@ubuntu.com"]


FORBIDDEN_COMMANDS = {"loginctl", "gnome-session-quit", "gnome-session", "gnome-screensaver-command", "dbus-send", "gdbus",
                      "busctl", "systemctl"}


def real_bus_address():
    return f"unix:path=/run/user/{os.getuid()}/bus"


def assert_private_bus(addr, session_base=None):
    """SAFETY: a tool that talks D-Bus and finds no (or the wrong) address silently falls back to the REAL session bus,
    where org.gnome.SessionManager, org.gnome.ScreenSaver or login1 would act on the person's real desktop. Every
    subprocess of the isolated session must be given the PRIVATE address explicitly, and this aborts when it is missing,
    empty, the real bus, or not inside this session's private directory."""
    if not addr or not addr.startswith("unix:path="):
        raise SystemExit(f"ABORT: DBUS_SESSION_BUS_ADDRESS is missing or not a private unix path: {addr!r}")
    path = addr[len("unix:path="):].split(",")[0]
    real = real_bus_address()
    if addr == real or path == real[len("unix:path="):] or os.path.realpath(path) == os.path.realpath(real[len("unix:path="):]):
        raise SystemExit(f"ABORT: D-Bus address {addr!r} is the REAL session bus")
    if session_base and not os.path.realpath(path).startswith(os.path.realpath(session_base) + os.sep):
        raise SystemExit(f"ABORT: D-Bus address {addr!r} is outside the isolated session {session_base!r}")


def guard_command(argv, env):
    """Used for every program the harness starts: private bus in its environment, no session/lock/login tools at all."""
    assert_private_bus(env.get("DBUS_SESSION_BUS_ADDRESS"))
    if os.path.basename(argv[0]) in FORBIDDEN_COMMANDS:
        raise SystemExit(f"ABORT: {argv[0]} is never run by the isolated tests")
    for k in ("WAYLAND_DISPLAY", "DISPLAY"):
        if k in env and not (k == "WAYLAND_DISPLAY" and env[k] == DISPLAY_NAME):
            raise SystemExit(f"ABORT: {k} in the environment of {argv[0]} is not the isolated shell's")


def _b(s):
    return s.encode() if isinstance(s, str) else s


def dump_extension_settings():
    """`dconf dump /org/gnome/shell/extensions/` (a read of the real settings) as keyfile text with the group names
    the keyfile backend uses. Returns '' when dconf is unavailable."""
    env = {k: v for k, v in os.environ.items() if k != "XDG_CONFIG_HOME"}
    try:
        txt = subprocess.run(["dconf", "dump", "/org/gnome/shell/extensions/"], capture_output=True, text=True,
                             env=env, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    out = []
    for line in txt.splitlines():
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1]
            line = "[org/gnome/shell/extensions" + ("" if name == "/" else "/" + name) + "]"
        out.append(line)
    return "\n".join(out) + "\n"


class Session:
    def __init__(self, monitors=("1280x720",), copy_extensions=(), enable_extensions=(), extra_keyfile="",
                 pipewire=False, own_extension=True, own_settings="", dump_user_settings=False, keep=False):
        self.monitors = list(monitors)
        self.copy_extensions = list(copy_extensions)
        self.enable_extensions = list(enable_extensions)
        self.extra_keyfile = extra_keyfile
        self.pipewire = pipewire
        self.own_extension = own_extension
        self.own_settings = own_settings
        self.dump_user_settings = dump_user_settings
        self.keep = keep
        self.procs = []
        self.apps = []
        self.base = tempfile.mkdtemp(prefix="awx-", dir=f"/run/user/{os.getuid()}")
        os.chmod(self.base, 0o700)
        d = lambda n: os.path.join(self.base, n)  # noqa: E731
        self.home, self.cfg, self.cache, self.data, self.rt, self.bus = (d(n) for n in ("home", "cfg", "cache", "data", "rt", "bus"))
        for p in (self.home, self.cfg, self.cache, self.data, self.rt, self.bus):
            os.makedirs(p, mode=0o700, exist_ok=True)
        self.keyfile = f"{self.cfg}/glib-2.0/settings/keyfile"
        self.log = f"{self.base}/shell.log"
        self.env = {
            "PATH": "/usr/bin:/bin", "HOME": self.home, "XDG_CONFIG_HOME": self.cfg, "XDG_CACHE_HOME": self.cache,
            "XDG_DATA_HOME": self.data, "XDG_RUNTIME_DIR": self.rt, "XDG_DATA_DIRS": "/usr/share",
            "XDG_CONFIG_DIRS": "/etc/xdg", "GSETTINGS_BACKEND": "keyfile", "LANG": "C.UTF-8",
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={self.bus}/session.sock",
            "DBUS_SYSTEM_BUS_ADDRESS": f"unix:path={self.bus}/system.sock", "GIO_USE_VFS": "local",
            "NO_AT_BRIDGE": "1", "XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "GNOME",
        }
        for k in ("WAYLAND_DISPLAY", "DISPLAY", "XAUTHORITY"):
            assert k not in self.env
        assert_private_bus(self.env["DBUS_SESSION_BUS_ADDRESS"], self.base)
        assert_private_bus(self.env["DBUS_SYSTEM_BUS_ADDRESS"], self.base)
        self._conn = None
        self._probe = None
        self.shell = None

    # ---- lifecycle -------------------------------------------------------------------------------------
    def _write_config(self):
        ext_dir = f"{self.data}/gnome-shell/extensions"
        os.makedirs(ext_dir, exist_ok=True)
        enabled = []
        if self.own_extension:
            dst = f"{ext_dir}/{UUID}"
            shutil.copytree(EXT_SRC, dst, ignore=shutil.ignore_patterns("tests", "*.compiled", "__pycache__", "media"))
            subprocess.check_call(["glib-compile-schemas", f"{dst}/schemas"])
        shutil.copytree(f"{HERE}/{PROBE}", f"{ext_dir}/{PROBE}")
        for uuid in self.copy_extensions:
            src = f"{REAL_EXT_DIR}/{uuid}"
            if os.path.isdir(src):
                shutil.copytree(src, f"{ext_dir}/{uuid}", ignore=shutil.ignore_patterns("*.po", "*.mo"))
                enabled.append(uuid)
            else:
                print(f"note: extension {uuid} is not installed here; skipped")
        enabled += [u for u in self.enable_extensions if os.path.isdir(f"/usr/share/gnome-shell/extensions/{u}")]
        enabled = ([UUID] if self.own_extension else []) + enabled + [PROBE]   # ours loads like a freshly enabled one
        os.makedirs(os.path.dirname(self.keyfile), exist_ok=True)
        with open(self.keyfile, "w") as f:
            if self.dump_user_settings:
                f.write(dump_extension_settings())
            f.write("[org/gnome/shell]\nenabled-extensions=[" + ", ".join(repr(u) for u in enabled) +
                    "]\ndisable-user-extensions=false\n\n")
            f.write(WM_KEYS + "\n" + self.own_settings + "\n" + self.extra_keyfile)
        # private buses: only the extensions service (prefs window host) is activatable
        os.makedirs(f"{self.bus}/services", exist_ok=True)
        for svc in ("org.gnome.Shell.Extensions", "org.gnome.Shell.Screencast"):   # prefs host, screen recorder
            shutil.copy(f"/usr/share/dbus-1/services/{svc}.service", f"{self.bus}/services/")
        for n in ("system", "session"):
            with open(f"{self.bus}/{n}.conf", "w") as f:
                f.write(f"""<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig><type>session</type><keep_umask/><listen>unix:path={self.bus}/{n}.sock</listen><auth>EXTERNAL</auth>
<servicedir>{self.bus}/services</servicedir>
<policy context="default"><allow send_destination="*" eavesdrop="true"/><allow eavesdrop="true"/><allow own="*"/></policy>
</busconfig>""")

    def _spawn(self, argv, env, logfile):
        guard_command(argv, env) if "DBUS_SESSION_BUS_ADDRESS" in env else None
        p = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=open(logfile, "ab"),
                             stderr=subprocess.STDOUT, start_new_session=True)
        self.procs.append(p)
        return p

    def start(self, timeout=45):
        self._write_config()
        assert_private_bus(self.env["DBUS_SESSION_BUS_ADDRESS"], self.base)
        for n in ("system", "session"):
            self._spawn(["dbus-daemon", f"--config-file={self.bus}/{n}.conf", "--nofork", "--nopidfile"], self.env,
                        f"{self.base}/{n}-bus.log")
        self.wait(lambda: os.path.exists(f"{self.bus}/session.sock") and os.path.exists(f"{self.bus}/system.sock"), 5)
        if self.pipewire:    # private PipeWire graph: lets the Shell's screen-cast service (recording) work
            for argv in (["pipewire"], ["wireplumber"]):
                self._spawn(argv, self.env, f"{self.base}/{argv[0]}.log")
                time.sleep(0.6)
        cmd = ["gnome-shell", "--headless", "--wayland", "--no-x11", "--wayland-display", DISPLAY_NAME] + \
              [a for m in self.monitors for a in ("--virtual-monitor", m)]
        self.shell = self._spawn(cmd, self.env, self.log)
        self._connect(timeout)
        self.warm_input()
        return self

    def _connect(self, timeout):
        t0, last = time.time(), None
        while time.time() - t0 < timeout:
            if self.shell.poll() is not None:
                raise RuntimeError("gnome-shell exited early:\n" + open(self.log, errors="replace").read()[-2500:])
            try:
                self._conn = Gio.DBusConnection.new_for_address_sync(
                    self.env["DBUS_SESSION_BUS_ADDRESS"],
                    Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
                self._probe = Gio.DBusProxy.new_sync(self._conn, Gio.DBusProxyFlags.NONE, None, "dev.agentworkspaces.Probe",
                                                     "/dev/agentworkspaces/Probe", "dev.agentworkspaces.Probe", None)
                if self._probe.get_name_owner():
                    self.eval("return 1")
                    time.sleep(1.0)
                    return
            except GLib.Error as e:
                last = e
            time.sleep(0.4)
        raise RuntimeError(f"probe did not appear within {timeout}s: {last}\n" + open(self.log, errors="replace").read()[-2500:])

    def stop(self):
        for a in self.apps:
            self._kill(a.pid)
        for p in reversed(self.procs):
            self._kill(p.pid)
        self._reap()
        if not self.keep:
            shutil.rmtree(self.base, ignore_errors=True)

    def __enter__(self):
        return self.start()

    def __exit__(self, *a):
        self.stop()

    @staticmethod
    def _kill(pid, sig=signal.SIGTERM):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass

    def mine(self):
        """pids whose XDG_RUNTIME_DIR is this session's private one or lies inside its private directory tree (the
        workspaces' own runtime dirs): an exact path match on that environment entry, never a name search."""
        out = []
        prefix = _b(f"XDG_RUNTIME_DIR={self.base}/")
        exact = _b(f"XDG_RUNTIME_DIR={self.rt}\0")
        for p in glob.glob("/proc/[0-9]*/environ"):
            try:
                with open(p, "rb") as f:
                    env = f.read()
            except OSError:
                continue
            if exact in env or any(e.startswith(prefix) for e in env.split(b"\0")):
                out.append(int(p.split("/")[2]))
        return [p for p in out if p != os.getpid()]

    def _reap(self):
        for sig, wait in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 1.5)):
            for pid in self.mine():
                self._kill(pid, sig)
            t0 = time.time()
            while self.mine() and time.time() - t0 < wait:
                time.sleep(0.1)
        for p in self.procs:
            try:
                p.wait(0.2)
            except subprocess.TimeoutExpired:
                pass

    @staticmethod
    def wait(pred, timeout, step=0.1):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            v = pred()
            if v:
                return v
            time.sleep(step)
        return None

    # ---- driving the shell -----------------------------------------------------------------------------
    def eval(self, code):
        r = self._probe.call_sync("Eval", GLib.Variant("(s)", (code,)), Gio.DBusCallFlags.NONE, 30000, None)
        out = json.loads(r.unpack()[0])
        if isinstance(out, dict) and "error" in out:
            raise RuntimeError(f"shell eval failed: {out['error']}\n{out.get('stack', '')}")
        return out

    def shot(self, path):
        self._probe.call_sync("Shot", GLib.Variant("(s)", (os.path.abspath(path),)), Gio.DBusCallFlags.NONE, 30000, None)
        return path

    def wayland_display(self):
        return DISPLAY_NAME if os.path.exists(f"{self.rt}/{DISPLAY_NAME}") else None

    def app_env(self, **extra):
        """Environment for a GUI program that is meant to appear in THIS shell (built from scratch, fails closed)."""
        d = self.wayland_display()
        assert d and self.rt.startswith("/run/user/") and "awx-" in self.rt, "isolated shell display missing"
        e = dict(self.env, WAYLAND_DISPLAY=d, GDK_BACKEND="wayland", GSETTINGS_BACKEND="memory")
        e.update(extra)
        assert_private_bus(e.get("DBUS_SESSION_BUS_ADDRESS"), self.base)
        return e

    def launch(self, argv, logname="app.log", **extra):
        env = self.app_env(**extra)
        guard_command(argv, env)
        p = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                             stdout=open(f"{self.base}/{logname}", "ab"), stderr=subprocess.STDOUT, start_new_session=True)
        self.apps.append(p)
        return p

    def shell_errors(self, needle=("agent-workspaces", "AgentWorkspaces", "desktop-cube")):
        out = []
        lines = open(self.log, errors="replace").read().splitlines()
        for i, l in enumerate(lines):
            if "JS ERROR" in l or "Extension agent-workspaces" in l and "rror" in l:
                out.append(l)
        return out

    # ---- synthetic input INSIDE the private shell (virtual devices of its own compositor) ---------------
    def _input(self, js):
        return self.eval("""
          const seat = Clutter.get_default_backend().get_default_seat();
          globalThis.__aw = globalThis.__aw ?? {
            ptr: seat.create_virtual_device(Clutter.InputDeviceType.POINTER_DEVICE),
            kbd: seat.create_virtual_device(Clutter.InputDeviceType.KEYBOARD_DEVICE)};
          const t = () => GLib.get_monotonic_time();
        """ + js)

    def warm_input(self):
        self._input("return 1")
        time.sleep(0.8)
        self.move(5, 5)
        time.sleep(0.3)
        self.move(6, 6)

    def move(self, x, y, tol=3):
        for _ in range(8):
            self._input(f"__aw.ptr.notify_absolute_motion(t(), {x}, {y}); return 1")
            time.sleep(0.1)
            px, py = self.eval("const [x, y] = global.get_pointer(); return [x, y]")
            if abs(px - x) <= tol and abs(py - y) <= tol:
                return
        raise RuntimeError(f"pointer would not move to {x},{y}; stuck at {px},{py}")

    def click(self, x, y, button=1):
        self.move(x, y)
        time.sleep(0.05)
        self._input(f"__aw.ptr.notify_button(t(), {button}, Clutter.ButtonState.PRESSED); return 1")
        time.sleep(0.06)
        self._input(f"__aw.ptr.notify_button(t(), {button}, Clutter.ButtonState.RELEASED); return 1")
        time.sleep(0.15)

    def chord(self, *names, hold=0.08):
        """Real key events through Mutter's input pipeline: modifiers pressed in order, the last name is the key,
        everything released in reverse. e.g. chord('Control_L', 'Alt_L', 'Down')."""
        codes = ", ".join(f"Clutter.KEY_{n}" for n in names)
        self._input(f"const c = [{codes}]; for (const k of c) __aw.kbd.notify_keyval(t(), k, Clutter.KeyState.PRESSED); return 1")
        time.sleep(hold)
        self._input(f"const c = [{codes}]; for (const k of c.reverse()) __aw.kbd.notify_keyval(t(), k, Clutter.KeyState.RELEASED); return 1")

    def type_text(self, text, gap=0.04):
        """ASCII text as key events (keyval == code point; no shifting: use lower case)."""
        for ch in text:
            kv = ord(ch)
            self._input(f"__aw.kbd.notify_keyval(t(), {kv}, Clutter.KeyState.PRESSED); "
                        f"__aw.kbd.notify_keyval(t(), {kv}, Clutter.KeyState.RELEASED); return 1")
            time.sleep(gap)

    def key(self, name):
        self.chord(name)

    # ---- state ------------------------------------------------------------------------------------------
    def state(self):
        return self.eval("""
          const wm = global.workspace_manager;
          const wins = global.get_window_actors().map(a => a.meta_window)
            .filter(w => w.get_window_type() === Meta.WindowType.NORMAL)
            .map(w => ({title: w.get_title(), app_id: w.get_gtk_application_id?.() ?? w.get_wm_class(),
                        ws: w.get_workspace()?.index() ?? -1, maximized: w.is_maximized?.() ?? null}));
          const g = new Gio.Settings({schema_id: 'org.gnome.desktop.wm.keybindings'});
          const keys = {};
          for (const k of ['switch-to-workspace-up', 'switch-to-workspace-down', 'switch-to-workspace-left', 'switch-to-workspace-right'])
            keys[k] = g.get_strv(k);
          const wg = global.window_group;
          return {active: wm.get_active_workspace_index(), n: wm.n_workspaces, layout_rows: wm.layout_rows,
                  windows: wins, keys, overview: Main.overview.visible,
                  window_group: {ty: wg.translation_y, tx: wg.translation_x, ry: wg.rotation_angle_y, opacity: wg.opacity},
                  ui_children: Main.uiGroup.get_n_children()};
        """)

    def settings_get(self, schema, key):
        return self.eval(f"return new Gio.Settings({{schema_id: '{schema}'}}).get_value('{key}').deepUnpack();")

    def keyfile_get(self, group, key):
        kf = GLib.KeyFile()
        kf.load_from_file(self.keyfile, GLib.KeyFileFlags.NONE)
        try:
            v = kf.get_value(group, key)
        except GLib.Error:
            return None
        try:
            return list(GLib.Variant.parse(GLib.VariantType("as"), v).unpack())
        except GLib.Error:
            return v

    def ext_call(self, method, uuid):
        return self._conn.call_sync("org.gnome.Shell", "/org/gnome/Shell", "org.gnome.Shell.Extensions", method,
                                    GLib.Variant("(s)", (uuid,)), None, Gio.DBusCallFlags.NONE, 10000, None)

    def ext_state(self, uuid):
        r = self._conn.call_sync("org.gnome.Shell", "/org/gnome/Shell", "org.gnome.Shell.Extensions", "ListExtensions",
                                 None, None, Gio.DBusCallFlags.NONE, 10000, None).unpack()[0]
        return r.get(uuid, {}).get("state")
