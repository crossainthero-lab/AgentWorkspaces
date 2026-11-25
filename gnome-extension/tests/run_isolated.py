#!/usr/bin/python3
"""Isolated-session test for the AgentWorkspaces GNOME Shell extension.

Starts ONE headless GNOME Shell (gnome-shell --headless, no X11) that is cut off from the user's session:
  * env -i with a private HOME, XDG_{CONFIG,CACHE,DATA}_HOME and XDG_RUNTIME_DIR under a private base dir
  * GSETTINGS_BACKEND=keyfile (the user's dconf is never read or written)
  * private session bus AND private "system" bus (the real system bus, GDM, logind, polkit are unreachable)
  * extension installed only into the private XDG_DATA_HOME
Key presses are injected through a Clutter virtual keyboard INSIDE that shell. Nothing is sent to the host.
Prints PASS/FAIL per check. Exit code 0 only if all checks pass.

Usage: tests/run_isolated.py [--keep] [--coexist]

--coexist: instead of the full suite, run a shorter check with desktop-cube and tilingshell (read-only copies of
the locally installed extensions, enabled ONLY in the private session) loaded next to this extension.
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

import gi
from gi.repository import Gio, GLib

HERE = os.path.dirname(os.path.abspath(__file__))
EXT_SRC = os.path.dirname(HERE)
UUID = "agent-workspaces@agentworkspaces.dev"
BASE = f"/run/user/{os.getuid()}/aw-ext-test"
HOME, CFG, CACHE, DATA, RT, BUS = (f"{BASE}/{d}" for d in ("home", "cfg", "cache", "data", "rt", "bus"))
DISPLAY_NAME = "aw-gs"
KEYFILE = f"{CFG}/glib-2.0/settings/keyfile"
RESULTS = []
PROCS = []  # (name, Popen)  every process this script starts; cleaned by exact handle


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""), flush=True)


sys.path.insert(0, HERE)
from gnome_session import assert_private_bus  # noqa: E402


def private_env(extra=None):
    e = {"PATH": "/usr/bin:/bin", "HOME": HOME, "XDG_CONFIG_HOME": CFG, "XDG_CACHE_HOME": CACHE,
         "XDG_DATA_HOME": DATA, "XDG_RUNTIME_DIR": RT, "XDG_DATA_DIRS": "/usr/share",
         "XDG_CONFIG_DIRS": "/etc/xdg", "GSETTINGS_BACKEND": "keyfile", "LANG": "C.UTF-8",
         "DBUS_SESSION_BUS_ADDRESS": f"unix:path={BUS}/session.sock",
         "DBUS_SYSTEM_BUS_ADDRESS": f"unix:path={BUS}/system.sock", "GIO_USE_VFS": "local",
         "NO_AT_BRIDGE": "1", "XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "GNOME"}
    e.update(extra or {})
    assert_private_bus(e["DBUS_SESSION_BUS_ADDRESS"], BASE)      # SAFETY: never the real bus
    assert_private_bus(e["DBUS_SYSTEM_BUS_ADDRESS"], BASE)
    return e


def start(name, argv, env, logfile):
    p = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=open(logfile, "ab"),
                         stderr=subprocess.STDOUT, start_new_session=True)
    PROCS.append((name, p))
    return p


def stop(p, name="", timeout=6):
    if p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(5)


OTHERS = ["desktop-cube@schneegans.github.com", "tilingshell@ferrarodomenico.com"]


def write_keyfile(extra="", others=()):
    os.makedirs(os.path.dirname(KEYFILE), exist_ok=True)
    # Mirrors the keybinding values of the target machine (read with `gsettings get`), applied ONLY to the
    # private keyfile backend.
    with open(KEYFILE, "w") as f:
        f.write(f"""[org/gnome/shell]
enabled-extensions=[{', '.join(repr(u) for u in [UUID, *others])}]
disable-user-extensions=false

[org/gnome/shell/extensions/agent-workspaces]
test-hooks=true

[org/gnome/desktop/wm/keybindings]
switch-to-workspace-up=['<Control><Alt>Up']
switch-to-workspace-down=['<Control><Alt>Down']
switch-to-workspace-left=['<Super>Page_Up', '<Super>KP_Prior', '<Super><Alt>Left', '<Control><Alt>Left']
switch-to-workspace-right=['<Super>Page_Down', '<Super>KP_Next', '<Super><Alt>Right', '<Control><Alt>Right']
{extra}""")


def read_key(group, key):
    kf = GLib.KeyFile()
    kf.load_from_file(KEYFILE, GLib.KeyFileFlags.NONE)
    try:
        return kf.get_value(group, key)
    except GLib.Error:
        return None


def parse_strv(v):
    return list(GLib.Variant.parse(GLib.VariantType('as'), v).unpack()) if v else []


def setup_dirs():
    shutil.rmtree(BASE, ignore_errors=True)
    for d in (HOME, CFG, CACHE, DATA, RT, BUS):
        os.makedirs(d, mode=0o700, exist_ok=True)
    ext_dst = f"{DATA}/gnome-shell/extensions/{UUID}"
    shutil.copytree(EXT_SRC, ext_dst, ignore=shutil.ignore_patterns("tests", "*.compiled", "__pycache__"))
    subprocess.check_call(["glib-compile-schemas", f"{ext_dst}/schemas"])
    os.makedirs(f"{BUS}/services", exist_ok=True)
    # only the extensions service (hosts the preferences window) is activatable on the private session bus
    shutil.copy("/usr/share/dbus-1/services/org.gnome.Shell.Extensions.service", f"{BUS}/services/")
    for n in ("system", "session"):
        with open(f"{BUS}/{n}.conf", "w") as f:
            f.write(f"""<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig><type>session</type><keep_umask/><listen>unix:path={BUS}/{n}.sock</listen><auth>EXTERNAL</auth>
<servicedir>{BUS}/services</servicedir>
<policy context="default"><allow send_destination="*" eavesdrop="true"/><allow eavesdrop="true"/><allow own="*"/></policy>
</busconfig>""")


def wait_for(pred, timeout, step=0.1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


def start_buses():
    for n in ("system", "session"):
        env = private_env({"WAYLAND_DISPLAY": DISPLAY_NAME}) if n == "session" else {"PATH": "/usr/bin:/bin", "HOME": HOME}
        start(f"{n}-bus", ["dbus-daemon", f"--config-file={BUS}/{n}.conf", "--nofork", "--nopidfile"], env,
              f"{BASE}/{n}-bus.log")
    assert wait_for(lambda: os.path.exists(f"{BUS}/session.sock") and os.path.exists(f"{BUS}/system.sock"), 5)


def start_shell():
    logf = f"{BASE}/shell.log"
    p = start("gnome-shell", ["gnome-shell", "--headless", "--wayland", "--no-x11", "--virtual-monitor", "1280x720",
                              "--wayland-display", DISPLAY_NAME], private_env(), logf)
    return p, logf


class Hook:
    """Client for the extension's test-only D-Bus interface on the PRIVATE session bus."""

    def __init__(self):
        self.conn = Gio.DBusConnection.new_for_address_sync(
            f"unix:path={BUS}/session.sock",
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)

    def call(self, method, args=None, sig=None, reply=None, name="dev.agentworkspaces.ShellExtension.Test",
             path="/dev/agentworkspaces/ShellExtension", iface="dev.agentworkspaces.ShellExtension.Test"):
        v = GLib.Variant(sig, args) if sig else None
        r = self.conn.call_sync(name, path, iface, method, v, GLib.VariantType(reply) if reply else None,
                                Gio.DBusCallFlags.NONE, 8000, None)
        return r.unpack() if r else None

    def state(self):
        return json.loads(self.call("State", reply="(s)")[0])

    def press(self, *names):
        self.call("Press", (list(names),), "(as)")

    def ext_call(self, method, uuid):
        return self.call(method, (uuid,), "(s)", name="org.gnome.Shell", path="/org/gnome/Shell",
                         iface="org.gnome.Shell.Extensions")


class FakeDaemon(threading.Thread):
    """STAND-IN for agent-workspacesd: speaks the JSON-lines API (list + enter) on the private runtime dir and
    launches the stand-in viewer with an environment that only references the isolated shell."""

    def __init__(self):
        super().__init__(daemon=True)
        self.dir = f"{RT}/agent-workspaces"
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        self.path = f"{self.dir}/api.sock"
        self.srv = socket.socket(socket.AF_UNIX)
        self.srv.bind(self.path)
        self.srv.listen(4)
        self.calls = []
        self.running = True

    def run(self):
        while self.running:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            with c, c.makefile("rwb") as f:
                line = f.readline()
                if not line:
                    continue
                req = json.loads(line)
                self.calls.append(req["method"])
                if req["method"] == "workspace.list":
                    res = {"workspaces": [{"name": "demo", "id": "w1", "state": "running", "activity": {}}]}
                elif req["method"] == "workspace.enter":
                    env = {"PATH": "/usr/bin:/bin", "HOME": HOME, "XDG_RUNTIME_DIR": RT, "WAYLAND_DISPLAY": DISPLAY_NAME,
                           "GDK_BACKEND": "wayland", "NO_AT_BRIDGE": "1", "GSETTINGS_BACKEND": "memory"}
                    assert os.path.exists(f"{RT}/{DISPLAY_NAME}"), "isolated shell socket missing; refusing to launch"
                    p = subprocess.Popen(["/usr/bin/python3", f"{HERE}/standin_viewer.py", req["params"].get("workspace", "demo")],
                                         env=env, stdin=subprocess.DEVNULL, stdout=open(f"{BASE}/viewer.log", "ab"),
                                         stderr=subprocess.STDOUT, start_new_session=True)
                    PROCS.append(("standin-viewer", p))
                    res = {"launched": True, "pid": p.pid}
                else:
                    res = {}
                f.write((json.dumps({"v": 2, "id": req.get("id"), "ok": True, "result": res}) + "\n").encode())
                f.flush()

    def close(self):
        self.running = False
        try:
            self.srv.close()
        except OSError:
            pass
        try:
            os.unlink(self.path)
        except OSError:
            pass


def settle(t=0.9):
    time.sleep(t)


def main_coexist():
    src_root = os.path.expanduser("~/.local/share/gnome-shell/extensions")
    setup_dirs()
    for u in OTHERS:
        shutil.copytree(f"{src_root}/{u}", f"{DATA}/gnome-shell/extensions/{u}")  # private copy; source untouched
    # a foreign extension binding the same accelerator as ours, to prove it is reported and left alone
    write_keyfile(extra="[org/gnome/shell/extensions/tilingshell]\nmove-window-up=['<Control><Alt>Down']\n", others=OTHERS)
    start_buses()
    shell, logf = start_shell()
    hook = None

    def ready():
        nonlocal hook
        if shell.poll() is not None:
            raise SystemExit("gnome-shell exited early")
        try:
            hook = Hook()
            return hook.state()
        except GLib.Error:
            return None
    check("shell starts with desktop-cube + tilingshell + this extension", wait_for(ready, 40, 0.5) is not None)
    FakeDaemon().start()      # the stand-in service lists one running workspace, so going up is allowed
    settle(2.5)
    hook.call("HideOverview")
    settle(1)
    listed = hook.call("ListExtensions", None, None, "(a{sa{sv}})", name="org.gnome.Shell", path="/org/gnome/Shell",
                       iface="org.gnome.Shell.Extensions")[0]
    states = {u: listed.get(u, {}).get("state") for u in [UUID, *OTHERS]}
    check("all three extensions are ENABLED (state 1)", all(v == 1 for v in states.values()), str(states))
    pe_env = {"PATH": "/usr/bin:/bin", "HOME": HOME, "XDG_RUNTIME_DIR": RT, "WAYLAND_DISPLAY": DISPLAY_NAME,
              "GDK_BACKEND": "wayland", "NO_AT_BRIDGE": "1", "GSETTINGS_BACKEND": "memory"}
    start("personal-window", ["/usr/bin/python3", f"{HERE}/standin_viewer.py", "personal window",
                              "dev.agentworkspaces.TestPersonal"], pe_env, f"{BASE}/personal.log")
    wait_for(lambda: any(w["app_id"] == "dev.agentworkspaces.TestPersonal" for w in hook.state()["windows"]), 20, 0.5)
    settle(0.5)
    s0 = hook.state()["active"]
    hook.press("Control_L", "Alt_L", "Up")
    settle(1.4)
    st = hook.state()
    smp = st["slide_samples"]
    check("Ctrl+Alt+Up goes up next to desktop-cube/tilingshell and slides vertically, stacked above (not the cube)",
          st["active"] == s0 + 1 and len(smp) >= 3 and smp[0] == -720 and smp[-1] == 0, f"active={st['active']} first={smp[:1]}")
    hook.press("Control_L", "Alt_L", "Down")
    settle(1.4)
    st = hook.state()
    check("return works next to them", st["active"] == s0 and st["slide_samples"][:1] == [720])
    hook.press("Control_L", "Alt_L", "Right")
    settle(1.5)
    check("Ctrl+Alt+Right still switches (desktop-cube's own animation)", hook.state()["active"] == s0 + 1)
    log = open(logf, errors="replace").read()
    errs = [l for l in log.splitlines() if "JS ERROR" in l and ("agent-workspaces" in l or "AgentWorkspaces" in l)]
    st = hook.state()
    check("another extension's binding on our accelerator is reported and not modified",
          any("tilingshell.move-window-up" in c for c in st["conflicts"]) and
          parse_strv(read_key("org/gnome/shell/extensions/tilingshell", "move-window-up")) == ["<Control><Alt>Down"],
          str([c for c in st["conflicts"] if "tilingshell" in c]))
    check("no JS errors attributed to this extension", not errs, errs[0][:160] if errs else "")
    cube_err = [l for l in log.splitlines() if "JS ERROR" in l][:2]
    print("info: other JS errors in shell log:", cube_err or "none")
    return 0


def main_prefs(out):
    """Open the preferences window inside the isolated shell and save a screenshot of it."""
    setup_dirs()
    write_keyfile(extra="[org/gnome/shell/keybindings]\nshow-screen-recording-ui=['<Control><Alt>Down']\n")
    start_buses()
    shell, logf = start_shell()
    hook = None

    def ready():
        nonlocal hook
        if shell.poll() is not None:
            raise SystemExit("gnome-shell exited early")
        try:
            hook = Hook()
            return hook.state()
        except GLib.Error:
            return None
    check("shell started", wait_for(ready, 40, 0.5) is not None)
    FakeDaemon().start()
    settle(2)
    hook.call("HideOverview")
    hook.call("ExtOpenPrefs") if False else None
    hook.conn.call_sync("org.gnome.Shell", "/org/gnome/Shell", "org.gnome.Shell.Extensions", "OpenExtensionPrefs",
                        GLib.Variant("(ssa{sv})", (UUID, "", {})), None, Gio.DBusCallFlags.NONE, 8000, None)
    settle(5)
    hook.call("ClearNotifications")  # the headless shell's own "Screen Lock disabled" notice is not part of the UI
    settle(1.5)
    hook.call("Screenshot", (out,), "(s)", reply="(s)")
    settle(2)
    check("prefs screenshot written", os.path.exists(out) and os.path.getsize(out) > 1000, out)
    # ---- what the prefs window does when a shortcut is captured / reset: write the setting. Check the extension
    # reacts live. (Pointer/keyboard driving of the prefs window itself is NOT automated, see docs.)
    grp, key = "org/gnome/shell/extensions/agent-workspaces", "enter-shortcut"
    hook.call("SetShortcut", ("enter-shortcut", ["<Control><Alt>e"]), "(sas)")
    settle(1.2)
    st = hook.state()
    check("changing the enter shortcut re-binds live and GNOME's Ctrl+Alt+Up is restored (no longer ours)",
          st["keys"]["switch-to-workspace-up"] == ["<Control><Alt>Up"] and
          st["keys"]["switch-to-workspace-down"] == [], str(st["keys"]))
    s0 = st["active"]
    hook.press("Control_L", "Alt_L", "e")
    settle(1.4)
    check("the new accelerator Ctrl+Alt+E enters the agent workspace", hook.state()["active"] != s0)
    hook.press("Control_L", "Alt_L", "Down")
    settle(1.2)
    hook.call("SetShortcut", ("enter-shortcut", ["<Control><Alt>Up"]), "(sas)")
    settle(1.2)
    check("setting it back takes Ctrl+Alt+Up over again", hook.state()["keys"]["switch-to-workspace-up"] == [])
    return 0


def main():
    keep = "--keep" in sys.argv
    if "--prefs" in sys.argv:
        try:
            return main_prefs(sys.argv[sys.argv.index("--prefs") + 1])
        finally:
            cleanup()
    if "--coexist" in sys.argv:
        try:
            return main_coexist()
        finally:
            cleanup()
    # ---- safety: refuse to run if any inherited display variable could leak into children
    print("planned shell env (private):", json.dumps({k: v for k, v in private_env().items() if k in
          ("HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "DBUS_SYSTEM_BUS_ADDRESS", "GSETTINGS_BACKEND")}))
    host_rt = f"/run/user/{os.getuid()}"
    pe = private_env()
    check("private env has no host display/bus", all(k not in pe for k in ("WAYLAND_DISPLAY", "DISPLAY", "XAUTHORITY"))
          and pe["XDG_RUNTIME_DIR"] != host_rt and f"{BASE}/" in pe["DBUS_SYSTEM_BUS_ADDRESS"]
          and f"{BASE}/" in pe["DBUS_SESSION_BUS_ADDRESS"] and "system_bus_socket" not in pe["DBUS_SYSTEM_BUS_ADDRESS"])
    setup_dirs()
    write_keyfile()
    original = {k: parse_strv(read_key("org/gnome/desktop/wm/keybindings", k))
                for k in ("switch-to-workspace-up", "switch-to-workspace-down", "switch-to-workspace-left",
                          "switch-to-workspace-right")}
    daemon = None
    try:
        start_buses()
        shell, logf = start_shell()
        hook = None

        def ready():
            nonlocal hook
            if shell.poll() is not None:
                raise SystemExit("gnome-shell exited early; see " + logf)
            try:
                hook = Hook()
                return hook.state()
            except GLib.Error:
                return None
        st = wait_for(ready, 40, 0.5)
        check("isolated shell started and extension exposes test hook", st is not None)
        if st is None:
            print(open(logf).read()[-3000:])
            return finish(keep)
        settle(1.5)
        hook.call("HideOverview")  # a headless shell with no windows starts in the Overview
        settle(1.0)
        # a personal window on workspace 0 (stand-in app), so GNOME keeps that workspace like a real desktop
        pe_env = {"PATH": "/usr/bin:/bin", "HOME": HOME, "XDG_RUNTIME_DIR": RT, "WAYLAND_DISPLAY": DISPLAY_NAME,
                  "GDK_BACKEND": "wayland", "NO_AT_BRIDGE": "1", "GSETTINGS_BACKEND": "memory"}
        assert os.path.exists(f"{RT}/{DISPLAY_NAME}")
        start("personal-window", ["/usr/bin/python3", f"{HERE}/standin_viewer.py", "personal window",
                                  "dev.agentworkspaces.TestPersonal"], pe_env, f"{BASE}/personal.log")
        wait_for(lambda: any(w["app_id"] == "dev.agentworkspaces.TestPersonal" for w in hook.state()["windows"]), 20, 0.5)
        settle(0.5)
        st = hook.state()
        check("personal stand-in window is on workspace 0", any(w["app_id"] == "dev.agentworkspaces.TestPersonal" and w["ws"] == 0
                                                                 for w in st["windows"]))

        # ---- bindings: taken over while enabled, original accelerators recorded
        up_now, down_now = st["keys"]["switch-to-workspace-up"], st["keys"]["switch-to-workspace-down"]
        check("GNOME Ctrl+Alt+Up/Down removed from wm keybindings while enabled", up_now == [] and down_now == [],
              f"up={up_now} down={down_now}")
        check("Left/Right keybindings untouched", st["keys"]["switch-to-workspace-left"] == original["switch-to-workspace-left"]
              and st["keys"]["switch-to-workspace-right"] == original["switch-to-workspace-right"])
        bk = json.loads(st["backup"]) if st["backup"] else {}
        check("backup recorded before change (survives a crash)",
              bk.get("switch-to-workspace-up", {}).get("before") == original["switch-to-workspace-up"] and
              bk.get("switch-to-workspace-down", {}).get("before") == original["switch-to-workspace-down"])
        check("animations enabled in isolated shell", st["animations"] is True)

        # ---- 1) NO service and no way to start one or to open a viewer without it: nothing to show, so nothing happens (log only).
        #         (With a startable service Ctrl+Alt+Up always goes up: see run_silent.py.)
        s0 = st["active"]
        n_actors, n_ws = st["window_actors"], st["n_workspaces"]
        hook.press("Control_L", "Alt_L", "Up")
        settle(2.0)
        st1 = hook.state()
        check("no service and no command to start it or the viewer: Ctrl+Alt+Up does nothing visible (same workspace, no new window or workspace, no slide, no notification)",
              st1["active"] == s0 and st1["window_actors"] == n_actors and st1["n_workspaces"] == n_ws and not st1["sliding"] and
              st1["slide_samples"] == [] and st1["notifications"] == st["notifications"] and st1["wg"] == [0, 0, 0] and shell.poll() is None,
              json.dumps({k: st1[k] for k in ("active", "window_actors", "n_workspaces", "sliding", "notifications", "wg")}))
        log = open(logf, errors="replace").read()
        check("... and the reason is only in the shell log", "could not be started" in log and "no agent-workspaces command found" in log)
        check("no JS error from the extension in shell log", "JS ERROR" not in log or "agent-workspaces" not in
              "\n".join(l for l in log.splitlines() if "JS ERROR" in l))

        # ---- 2) with daemon: viewer is requested and opened on the agent workspace
        daemon = FakeDaemon()
        daemon.start()
        hook.press("Control_L", "Alt_L", "Up")
        stv = wait_for(lambda: (lambda s: s if any(w["app_id"] == "dev.agentworkspaces.Viewer" for w in s["windows"]) else None)(hook.state()), 20, 0.5)
        check("daemon asked: workspace.list + workspace.enter", daemon.calls[:2] == ["workspace.list", "workspace.enter"],
              str(daemon.calls))
        check("stand-in viewer window appeared on the agent workspace", stv is not None and any(
            w["app_id"] == "dev.agentworkspaces.Viewer" and w["ws"] == stv["agent"] == stv["active"] for w in stv["windows"]),
            json.dumps(stv["windows"]) if stv else "")
        settle(0.8)
        try:
            hook.call("ClearNotifications")
            settle(1)
            hook.call("Screenshot", (f"{BASE}/enter.png",), "(s)", reply="(s)")
            settle(1)
        except GLib.Error as e:
            print("screenshot hook:", e)
        agent_idx = hook.state()["agent"]
        origin_idx = hook.state()["origin"]
        check("origin workspace recorded", origin_idx == s0, f"origin={origin_idx}")
        hook.press("Control_L", "Alt_L", "Up")
        settle(0.8)
        check("Ctrl+Alt+Up while already on the agent desktop is a no-op", hook.state()["active"] == agent_idx)
        hook.press("Control_L", "Alt_L", "Down")
        settle(1.2)
        check("return keeps the viewer window on the agent workspace",
              hook.state()["active"] == s0 and any(w["app_id"] == "dev.agentworkspaces.Viewer" and w["ws"] == agent_idx
                                                   for w in hook.state()["windows"]))
        daemon.calls.clear()
        hook.press("Control_L", "Alt_L", "Up")
        settle(1.2)
        s3 = hook.state()
        check("re-enter reuses the existing viewer workspace without a second viewer",
              s3["active"] == agent_idx and "workspace.enter" not in daemon.calls and
              sum(1 for w in s3["windows"] if w["app_id"] == "dev.agentworkspaces.Viewer") == 1, str(daemon.calls))
        hook.press("Control_L", "Alt_L", "Down")
        settle(1.0)

        # ---- 3) horizontal navigation untouched
        hook.press("Control_L", "Alt_L", "Right")
        settle(1.0)
        r1 = hook.state()["active"]
        hook.press("Control_L", "Alt_L", "Left")
        settle(1.0)
        l1 = hook.state()["active"]
        check("Ctrl+Alt+Right/Left walk the personal workspaces only: Right skips the agent desktop above and reaches the trailing empty workspace",
              r1 == s0 + 2 and l1 == s0, f"right->{r1} left->{l1} (agent desktop is workspace {agent_idx})")
        hook.press("Control_L", "Alt_L", "Down")
        settle(0.6)
        check("Ctrl+Alt+Down on a personal workspace is passed through (no-op in horizontal layout)", hook.state()["active"] == s0)

        # ---- 4) disable restores bindings; enable takes over again
        before_disable = hook.state()
        try:
            hook.ext_call("DisableExtension", UUID)
            settle(1.5)
            kd = parse_strv(read_key("org/gnome/desktop/wm/keybindings", "switch-to-workspace-down"))
            ku = parse_strv(read_key("org/gnome/desktop/wm/keybindings", "switch-to-workspace-up"))
            check("disable() restores GNOME bindings exactly (private keyfile)",
                  kd == original["switch-to-workspace-down"] and ku == original["switch-to-workspace-up"], f"up={ku} down={kd}")
            check("backup cleared after restore", read_key("org/gnome/shell/extensions/agent-workspaces", "backup") in (None, "''", ""),
                  str(read_key("org/gnome/shell/extensions/agent-workspaces", "backup")))
            hook.ext_call("EnableExtension", UUID)
            settle(2)
            st4 = wait_for(lambda: hook.state(), 10, 0.5)
            check("re-enable takes over again", st4 and st4["keys"]["switch-to-workspace-down"] == [])
        except GLib.Error as e:
            check("disable/enable via org.gnome.Shell.Extensions D-Bus", False, str(e))

        # ---- 5) crash while overridden -> stale backup repaired on next start, other bindings preserved
        pid = shell.pid
        os.kill(pid, signal.SIGKILL)
        shell.wait(10)
        settle(1)
        stale_backup = read_key("org/gnome/shell/extensions/agent-workspaces", "backup")
        stale_keys = parse_strv(read_key("org/gnome/desktop/wm/keybindings", "switch-to-workspace-down"))
        check("after SIGKILL the keyfile still shows the removed binding and a non-empty backup (the crash case)",
              stale_keys == [] and stale_backup not in (None, "''", ""), f"keys={stale_keys}")
        # user adds another accelerator meanwhile; a foreign key also uses Ctrl+Alt+Down
        kf = GLib.KeyFile()
        kf.load_from_file(KEYFILE, GLib.KeyFileFlags.NONE)
        kf.set_value("org/gnome/desktop/wm/keybindings", "switch-to-workspace-up", "['<Super>Up']")
        kf.set_value("org/gnome/shell/keybindings", "show-screen-recording-ui", "['<Control><Alt>Down']")
        kf.save_to_file(KEYFILE)
        for f in os.listdir(RT):  # stale wayland socket of the killed shell
            if f.startswith(DISPLAY_NAME):
                os.unlink(f"{RT}/{f}")
        if os.path.exists(f"{BUS}/session.sock"):
            pass
        shell, logf2 = start_shell()
        st5 = wait_for(ready, 40, 0.5)
        check("shell restarts with stale backup", st5 is not None)
        settle(1.5)
        log2 = open(logf2, errors="replace").read()
        check("extension reports restoring the stale backup on start", "restored GNOME keybindings (stale backup" in log2)
        st5 = hook.state()
        check("conflict with a foreign GNOME key is reported, not modified",
              any("show-screen-recording-ui" in c for c in st5["conflicts"]) and
              parse_strv(read_key("org/gnome/shell/keybindings", "show-screen-recording-ui")) == ["<Control><Alt>Down"],
              str(st5["conflicts"]))
        check("binding the user added meanwhile is preserved by the merge (<Super>Up kept)",
              "<Super>Up" in st5["keys"]["switch-to-workspace-up"] or st5["keys"]["switch-to-workspace-up"] == ["<Super>Up"],
              str(st5["keys"]["switch-to-workspace-up"]))
        hook.ext_call("DisableExtension", UUID)
        settle(1.5)
        ku = set(parse_strv(read_key("org/gnome/desktop/wm/keybindings", "switch-to-workspace-up")))
        kd = parse_strv(read_key("org/gnome/desktop/wm/keybindings", "switch-to-workspace-down"))
        check("after crash + restart + disable, Ctrl+Alt+Up/Down are back and <Super>Up kept",
              ku == {"<Control><Alt>Up", "<Super>Up"} and kd == original["switch-to-workspace-down"], f"up={sorted(ku)} down={kd}")
        return finish(keep, shell)
    finally:
        if daemon:
            daemon.close()
        cleanup()


def finish(keep, shell=None):
    return 0


def cleanup():
    # stop in reverse start order, by exact handle
    for name, p in reversed(PROCS):
        pid = p.pid
        stop(p, name)
        print(f"cleanup: stopped {name} pid={pid} exit={p.returncode}")
    # verify nothing is left that carries our private runtime dir
    left = []
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                env = open(f"/proc/{d}/environ", "rb").read().split(b"\0")
                if f"XDG_RUNTIME_DIR={RT}".encode() in env:
                    left.append((int(d), open(f"/proc/{d}/comm").read().strip()))
            except OSError:
                pass
    print("cleanup: leftover processes carrying the private runtime dir:", left or "none")
    for pid, comm in left:  # exact PIDs verified through the private env marker
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"cleanup: SIGTERM leftover {comm} pid={pid}")
        except OSError:
            pass


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        import traceback
        traceback.print_exc()
        RESULTS.append(("test script completed without exception", False))
    finally:
        failed = [n for n, ok in RESULTS if not ok]
        print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed" + (f"; FAILED: {failed}" if failed else ""))
        if "--keep" not in sys.argv:
            shutil.rmtree(BASE, ignore_errors=True)
        sys.exit(1 if failed else 0)
