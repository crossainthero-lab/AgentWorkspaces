"""REAL tests of the graphical-isolation guard (aw/guard.py) against a STAND-IN personal desktop.

The stand-in is a throw-away headless labwc ("HostSim") with a probe window that logs every input event, a private
Xvfb X server and a private D-Bus session bus that can activate real applications (as the user's session bus can).
The agent's shell is a real `bash -c` started with the stand-in's WAYLAND_DISPLAY / XDG_RUNTIME_DIR / DISPLAY /
DBUS_SESSION_BUS_ADDRESS as its "host" environment, exactly as an agent started from a desktop terminal would see it.
Nothing here touches the real desktop.

Measured: after every attempt the stand-in must show no new window (Wayland toplevels and X clients), no change of
focus and no event in the probe's log. A CONTROL run first proves the stand-in does show windows when the guard is not
applied (otherwise "nothing appeared" would mean nothing).
"""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as K  # noqa: E402
from aw import guard, guard_install  # noqa: E402
from harness import HostSim  # noqa: E402

HOST = None
XVFB = None
BUS = None
HOME = None
DISPLAY = None


def _on_term(signum, frame):
    tearDownModule()           # an interrupted run must not leave a compositor, Xvfb or bus behind
    os._exit(143)


def setUpModule():
    global HOST, XVFB, BUS, HOME, DISPLAY
    signal.signal(signal.SIGTERM, _on_term)
    if not K.have("labwc") or not K.have("Xvfb"):
        raise unittest.SkipTest("labwc / Xvfb not installed")
    base = os.path.join(f"/run/user/{os.getuid()}", f"awt-guard-{os.getpid()}")
    os.makedirs(base, mode=0o700)
    K.BASE = base
    HOST = HostSim(base)
    HOME = os.path.join(base, "home")
    os.makedirs(HOME)
    n = 700 + os.getpid() % 200
    DISPLAY = f":{n}"
    XVFB = subprocess.Popen(["Xvfb", DISPLAY, "-nolisten", "tcp", "-screen", "0", "800x600x24"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(60):
        if os.path.exists(f"/tmp/.X11-unix/X{n}"):
            break
        time.sleep(0.1)
    benv = standin_env(with_bus=False)
    BUS = subprocess.Popen(["dbus-daemon", "--session", f"--address=unix:path={HOST.rt}/bus", "--nofork", "--nopidfile"],
                           env=benv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(60):
        if os.path.exists(os.path.join(HOST.rt, "bus")):
            break
        time.sleep(0.1)
    hostlog = os.path.join(base, "host-probe.log")
    HOST.probe_log = hostlog
    HOST.spawn([sys.executable, K.PROBE, "PERSONAL", hostlog, "#718096"])
    for _ in range(100):
        if any(e.get("ev") == "ready" for e in K.log_events(hostlog)):
            break
        time.sleep(0.1)
    time.sleep(1.0)


def tearDownModule():
    for p in (BUS, XVFB):
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
    if HOST:
        HOST.close()
    shutil.rmtree(K.BASE, ignore_errors=True)


def standin_env(with_bus=True):
    e = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": HOME or os.path.expanduser("~"), "LANG": "C.UTF-8",
         "XDG_RUNTIME_DIR": HOST.rt, "WAYLAND_DISPLAY": "wayland-0", "DISPLAY": DISPLAY or ":0",
         "GSETTINGS_BACKEND": "memory", "GTK_USE_PORTAL": "0", "NO_AT_BRIDGE": "1", "GSK_RENDERER": "cairo",
         "XDG_CURRENT_DESKTOP": "labwc:wlroots", "WLR_RENDERER": "pixman"}
    if with_bus:
        e["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={HOST.rt}/bus"
    return e


def claude_guard_env():
    """Exactly what `integrate claude-code install` writes into settings.json 'env' (applied by Claude Code to its shell)."""
    e = standin_env()
    e.update(guard_install.claude_env(HOST.rt))
    return e


def hermes_guard_env():
    """Hermes: the host environment is untouched, the hook rewrites the command (prelude)."""
    return standin_env()


def host_state():
    wins = sorted((w.get("title") or "", w.get("app_id") or "") for w in HOST.sess.list_windows())
    x = subprocess.run(["xlsclients", "-display", DISPLAY], capture_output=True, text=True, timeout=10).stdout.split("\n")
    focused = [w.get("title") for w in HOST.sess.list_windows() if w.get("focused")]
    return {"windows": wins, "x_clients": sorted(l for l in x if l.strip()), "focused": focused,
            "probe_events": len(K.log_events(HOST.probe_log))}


def run_shell(cmd, env, wait=2.5, rewrite=False):
    """Run a command in a real shell with env; return (exit code or None if still running, stdout+stderr)."""
    if rewrite:
        cmd = guard.rewrite(cmd, HOST.rt)
    out = tempfile.NamedTemporaryFile(prefix="awg", delete=False)
    p = subprocess.Popen(["bash", "-c", cmd], env=env, cwd=HOME, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                         start_new_session=True)
    t0 = time.time()
    while time.time() - t0 < wait and p.poll() is None:
        time.sleep(0.1)
    rc = p.poll()
    time.sleep(0.5)
    return rc, p, out.name


def reap(p):
    """Stop the whole process group of a test shell, by exact group id; also leftovers it started."""
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        p.wait(3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def kill_standin_extras(baseline):
    """Close any window the control runs opened (control only) by closing them through the stand-in compositor."""
    for w in HOST.sess.list_windows():
        if (w.get("title") or "", w.get("app_id") or "") not in baseline["windows"]:
            try:
                HOST.sess.window_action(w["id"], "close")
            except Exception:
                pass
    time.sleep(1.0)


# Attempts that the DENY layer does not see (they look like ordinary programs): the ENVIRONMENT must stop them.
ENV_ONLY = [
    "gnome-calculator", "gnome-text-editor", "nautilus", "xeyes", "xclock",
    "sh -c 'gnome-calculator'", "nohup gnome-calculator >/dev/null 2>&1 &", "(setsid gnome-calculator &) ; sleep 1",
    "bash -c 'cd / && exec gnome-calculator'", "env -i PATH=$PATH HOME=$HOME gnome-calculator",
    "env -i PATH=$PATH HOME=$HOME xeyes", "timeout 5 gnome-calculator", "nice gnome-calculator",
    "python3 -c \"import subprocess; subprocess.Popen(['gnome-calculator'])\"",
    "python3 -c \"import gi; gi.require_version('Gtk','4.0'); from gi.repository import Gtk; "
    "a=Gtk.Application(application_id='x.y'); a.connect('activate', lambda a: Gtk.ApplicationWindow(application=a).present()); a.run([])\"",
    "python3 -c \"import webbrowser; webbrowser.open('http://127.0.0.1:9/')\"",
    "printf '#!/bin/sh\\nexec gnome-calculator\\n' > /tmp/awg_run.sh; sh /tmp/awg_run.sh",
    "unset WAYLAND_DISPLAY DISPLAY DBUS_SESSION_BUS_ADDRESS XDG_RUNTIME_DIR; gnome-calculator",
    "unset WAYLAND_DISPLAY XDG_RUNTIME_DIR; xeyes",
    "export -n XDG_RUNTIME_DIR; gnome-calculator",
    "env -u WAYLAND_DISPLAY -u XDG_RUNTIME_DIR -u DISPLAY gnome-calculator",
    "google-chrome --user-data-dir=$HOME/chrome-p --no-first-run --no-default-browser-check about:blank",
    "google-chrome --user-data-dir=$HOME/chrome-p2 --no-first-run --ozone-platform=x11 about:blank",
    "firefox --profile $HOME/ff-p --new-instance about:blank",
    "gnome-calculator --version; gnome-calculator",
    "GDK_BACKEND=x11 gnome-calculator", "QT_QPA_PLATFORM=xcb xeyes", "SDL_VIDEODRIVER=x11 xeyes",
]
# Attempts the DENY layer must refuse (they would work even in the guarded environment or are hidden launchers)
DENIED = [
    "xdg-open https://example.org", "xdg-open /tmp", "gio open https://example.org", "gtk-launch org.gnome.Calculator",
    "gapplication launch org.gnome.Calculator", "gapplication action org.gnome.Calculator new-window",
    "dbus-send --session --dest=org.gnome.Calculator --type=method_call /org/gnome/Calculator org.freedesktop.Application.Activate dict:string:variant:",
    "gdbus call --session --dest org.freedesktop.Application --object-path /x --method org.freedesktop.Application.Activate '{}'",
    "busctl --user call org.gnome.Calculator /org/gnome/Calculator org.freedesktop.Application Activate a{sv} 0",
    "systemd-run --user gnome-calculator", "systemctl --user start app-gnome-calculator.service",
    "xdotool key a", "xdotool mousemove 10 10 click 1", "ydotool click 0x00", "wtype hello", "wmctrl -l", "scrot /tmp/s.png",
    "grim /tmp/s.png", "gnome-screenshot -f /tmp/s.png", "import -window root /tmp/s.png", "xclip -selection clipboard -o",
    "WAYLAND_DISPLAY=wayland-0 gnome-calculator", f"WAYLAND_DISPLAY={'$XDG_RUNTIME_DIR'}/wayland-0 gnome-calculator",
    "DISPLAY=:0 xeyes", "export DISPLAY=$DISPLAY_HOST; xeyes", "XDG_RUNTIME_DIR=/run/user/1000 gnome-calculator",
    "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus gapplication list", "env WAYLAND_DISPLAY=wayland-1 foot",
    "nohup xdg-open https://example.org &", "setsid -f xdg-open x", "sh -c 'xdg-open x'", "bash -c \"gio open x\"",
    "echo $(xdg-open x)", "true && xdotool key a", "sudo -n xdotool key a", "xargs xdg-open < /dev/null",
    "google-chrome https://example.org", "google-chrome-stable --new-window https://example.org", "firefox https://example.org",
    "chromium-browser https://example.org", "x-www-browser https://example.org", "sensible-browser https://example.org",
    "python3 -c \"import os; os.environ['WAYLAND_DISPLAY']='wayland-0'\"", "ls /run/user/1000/wayland-0", "cat /tmp/.X11-unix/X0",
    "at now <<< 'gnome-calculator'", "notify-send hello", "gsettings set org.gnome.desktop.interface color-scheme prefer-dark",
    "sed -i 's/x/y/' ~/.claude/settings.json", "echo {} > ~/.hermes/config.yaml", "rm ~/.hermes/shell-hooks-allowlist.json",
]


class Control(unittest.TestCase):
    """Without the guard the stand-in DOES show these windows: the test can see what it is looking for."""

    def test_unguarded_apps_appear_on_the_standin_desktop(self):
        base = host_state()
        env = standin_env()
        procs = []
        try:
            for cmd, what in (("gnome-calculator", "wayland"), ("xeyes", "x"), ("gapplication launch org.gnome.Calculator", "dbus")):
                rc, p, _ = run_shell(cmd, env, wait=4)
                procs.append(p)
            time.sleep(2)
            now = host_state()
            self.assertGreater(len(now["windows"]), len(base["windows"]), f"control: no Wayland window appeared: {now}")
            self.assertGreater(len(now["x_clients"]), len(base["x_clients"]), f"control: no X client appeared: {now}")
        finally:
            for p in procs:
                reap(p)
            kill_standin_extras(base)


class Battery(unittest.TestCase):
    def _env_only(self, env, rewrite):
        base = host_state()
        leaks, outcomes = [], []
        for cmd in ENV_ONLY:
            d = guard.check_command(cmd)
            if d.deny:
                outcomes.append((cmd, "denied"))
                continue
            rc, p, _ = run_shell(cmd, env, wait=2.0, rewrite=rewrite)
            now = host_state()
            reap(p)
            outcomes.append((cmd, "ran-and-failed-safe" if now == base else "LEAK"))
            if now != base:
                leaks.append((cmd, now))
                kill_standin_extras(base)
        return base, leaks, outcomes

    def test_control_how_many_of_the_attempts_would_leak_without_the_guard(self):
        base = host_state()
        leaked = []
        for cmd in ENV_ONLY:
            rc, p, _ = run_shell(cmd, standin_env(), wait=2.0)
            now = host_state()
            reap(p)
            if now != base:
                leaked.append(cmd)
                kill_standin_extras(base)
        print(f"\nCONTROL: {len(leaked)} of {len(ENV_ONLY)} env-only attempts open a window or X client on the stand-in WITHOUT the guard")
        self.assertGreaterEqual(len(leaked), 10, "the stand-in must be able to show what the guard prevents")

    def test_environment_stops_ordinary_looking_launches_claude_mechanism(self):
        base, leaks, outcomes = self._env_only(claude_guard_env(), rewrite=False)
        self.assertEqual(leaks, [], f"windows or events appeared on the stand-in desktop: {leaks}")
        self.assertEqual(host_state(), base)
        print(f"\nCLAUDE mechanism: {len(outcomes)} env-only attempts, leaks=0, "
              f"denied-by-rule={sum(1 for _, o in outcomes if o == 'denied')}")

    def test_environment_stops_ordinary_looking_launches_hermes_mechanism(self):
        base, leaks, outcomes = self._env_only(hermes_guard_env(), rewrite=True)
        self.assertEqual(leaks, [], f"windows or events appeared on the stand-in desktop: {leaks}")
        print(f"\nHERMES mechanism: {len(outcomes)} env-only attempts, leaks=0")

    def test_deny_layer_refuses_the_rest_and_the_standin_stays_untouched(self):
        base = host_state()
        not_denied = [c for c in DENIED if not guard.check_command(c).deny]
        self.assertEqual(not_denied, [], f"the deny layer let these through: {not_denied}")
        # and even if a denied command were executed raw in the Claude-guarded environment, nothing appears
        # (belt and braces for the launchers the environment alone stops: those that need the bus or an unset display)
        env = claude_guard_env()
        for cmd in ("gapplication launch org.gnome.Calculator", "gio open /tmp", "xdg-open https://example.org",
                    "systemd-run --user gnome-calculator"):
            rc, p, _ = run_shell(cmd, env, wait=3)
            reap(p)
        self.assertEqual(host_state(), base, "even raw, the guarded environment kept the launchers away from the stand-in")
        print(f"\nDENY layer: {len(DENIED)} attempts all refused")


class OrdinaryWork(unittest.TestCase):
    """The guard must not get in the way of normal terminal, coding and development work."""

    def test_ordinary_commands_behave_identically_guarded_and_unguarded(self):
        work = tempfile.mkdtemp(prefix="awwork")
        open(os.path.join(work, "Makefile"), "w").write("all:\n\t@echo made-$(shell echo ok)\n")
        open(os.path.join(work, "t.py"), "w").write("import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(2+2, 4)\nunittest.main()\n")
        srv = subprocess.Popen([sys.executable, "-u", "-m", "http.server", "0", "--bind", "127.0.0.1"], cwd=work, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, start_new_session=True)
        try:
            port = None
            line = srv.stdout.readline()
            port = int(line.split("port")[1].split()[0]) if "port" in line else 8000
            cmds = [
                "ls -la | sed 's/^.*[0-9] //' | sort", "rm -rf .git && git init -q . && git add -A && git -c user.email=a@b -c user.name=x commit -qm init && git status --short && git log --oneline | wc -l",
                "python3 -c 'print(sum(range(100)))'", "python3 t.py 2>&1 | tail -2", "pip --version | cut -d' ' -f1,2", "make", "ssh -V 2>&1 | cut -c1-12",
                f"curl -s http://127.0.0.1:{port}/Makefile", "docker --version | cut -c1-14", "systemctl status ssh >/dev/null 2>&1; echo rc-system=$?",
                "systemctl --user list-units --no-legend --type=target 2>&1 | head -1 | awk '{print $1}'", "echo $HOME | wc -c", "cat /etc/os-release | head -1",
                "node --version 2>&1 | cut -c1-2", "npm --version 2>&1 | cut -c1-1", "tar czf a.tgz Makefile && tar tzf a.tgz",
                "env | grep -c '^PATH='", "timeout 3 sleep 1; echo slept", "nohup sh -c 'echo bg' >bg.out 2>&1 & wait; cat bg.out",
            ]
            for envname, envf, rw in (("claude", claude_guard_env, False), ("hermes", hermes_guard_env, True)):
                for cmd in cmds:
                    self.assertFalse(guard.check_command(cmd).deny, f"wrongly denied: {cmd}")
                    plain = subprocess.run(["bash", "-c", cmd], env=standin_env(), cwd=work, capture_output=True, text=True, timeout=60)
                    c2 = guard.rewrite(cmd, HOST.rt) if rw else cmd
                    guarded = subprocess.run(["bash", "-c", c2], env=envf(), cwd=work, capture_output=True, text=True, timeout=60)
                    self.assertEqual((plain.returncode, plain.stdout), (guarded.returncode, guarded.stdout),
                                     f"{envname}: output differs for: {cmd}\n{plain.stdout!r} vs {guarded.stdout!r}\n{guarded.stderr[-200:]}")
            print(f"\nORDINARY work: {len(cmds)} commands x 2 mechanisms identical (exit code and output)")
            # a headless browser screenshot works too
            for envf, rw in ((claude_guard_env, False), (hermes_guard_env, True)):
                shot = os.path.join(work, "shot.png")
                if os.path.exists(shot):
                    os.remove(shot)
                cmd = f"google-chrome --headless=new --disable-gpu --no-sandbox --screenshot={shot} --window-size=400,300 file://{work}/Makefile"
                if rw:
                    cmd = guard.rewrite(cmd, HOST.rt)
                subprocess.run(["bash", "-c", cmd], env=envf(), cwd=work, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=90)
                self.assertTrue(os.path.exists(shot) and os.path.getsize(shot) > 500, "headless Chrome screenshot failed under the guard")
        finally:
            os.killpg(srv.pid, signal.SIGTERM)
            shutil.rmtree(work, ignore_errors=True)


class Limits(unittest.TestCase):
    """What the guard can and cannot prevent, measured (documented in docs/RELEASE.md)."""

    def test_unsetting_does_not_bring_the_host_display_back(self):
        # covered by ENV_ONLY (unset/env -u/export -n); here the exit status is the point
        env = claude_guard_env()
        rc, p, out = run_shell("unset XDG_RUNTIME_DIR WAYLAND_DISPLAY DISPLAY; xeyes", env, wait=5)
        reap(p)
        self.assertNotEqual(rc, 0)

    def test_a_script_that_builds_the_display_variables_itself_is_NOT_caught_by_the_deny_rules(self):
        """Honest limit: obfuscated assignment passes the text rules (the environment still has to be re-pointed at the
        host to reach it; both must be re-assigned). This documents the gap; it is not a promise."""
        cmd = "python3 -c \"import os,subprocess; e=dict(os.environ); e.pop('WAYLAND'+'_SOCKET',None); e['WAYLAND'+'_DISPLAY']='wayland'+'-0'; e['XDG_RUNTIME'+'_DIR']='%s'; e['DBUS_SESSION'+'_BUS_ADDRESS']='unix:path=%s/bus'; subprocess.Popen(['gnome-calculator'], env=e, stderr=open('/tmp/awgap.err','w'))\"" % (HOST.rt, HOST.rt)
        self.assertFalse(guard.check_command(cmd).deny, "if this now denies, update docs/RELEASE.md: the rule got stricter")
        base = host_state()
        rc, p, outf = run_shell(cmd, claude_guard_env(), wait=4)
        time.sleep(5.0)
        leaked = host_state() != base
        if not leaked:
            print("DEBUG gap script output:", open(outf).read()[-400:], open("/tmp/awgap.err").read()[-500:])
        reap(p)
        kill_standin_extras(base)
        self.assertTrue(leaked, "expected the documented gap (a hostile or very determined script) to be reproducible")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class SingleInstance(unittest.TestCase):
    """The incident class: a browser started from an agent's shell hands its page to the browser ALREADY RUNNING on the
    desktop through its profile (a socket in the user's real home), so a faked display does not help. Reproduced here
    with a THROW-AWAY profile: a Chrome runs on the stand-in desktop with the stand-in HOME; its web server proves
    whether a handoff happened (a request carrying a Chrome user agent)."""

    def setUp(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.log = []
        log = self.log

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                log.append((self.path, self.headers.get("User-Agent", "")))
                b = b"<html><title>handoff</title><body>hello</body></html>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = host_state()
        self.chrome = subprocess.Popen(
            ["google-chrome", "--ozone-platform=wayland", "--no-first-run", "--no-default-browser-check", "--password-store=basic", "about:blank"],
            env=standin_env(), cwd=HOME, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(100):
            if len(host_state()["windows"]) > len(self.base["windows"]):
                break
            time.sleep(0.2)
        time.sleep(2.0)
        self.after_start = host_state()

    def tearDown(self):
        reap(self.chrome)
        self.srv.shutdown()
        kill_standin_extras(self.base)

    def fetched(self, path):
        return [e for e in self.log if e[0] == path and "Chrome" in e[1]]

    def test_handoff_happens_without_the_guard_and_the_guard_denies_every_variant(self):
        self.assertGreater(len(self.after_start["windows"]), len(self.base["windows"]), "test setup: the stand-in Chrome did not start")
        url = f"http://127.0.0.1:{self.port}"
        # CONTROL (stand-in profile, unguarded): the handoff reaches the running browser
        rc, p, _ = run_shell(f"google-chrome {url}/control", standin_env(), wait=8)
        reap(p)
        time.sleep(2)
        self.assertTrue(self.fetched("/control"), "control: expected the running Chrome to receive the URL by handoff")
        # the environment alone does NOT stop it (that is why the deny layer exists)
        rc, p, _ = run_shell(f"google-chrome {url}/envonly", claude_guard_env(), wait=8)
        reap(p)
        time.sleep(2)
        env_alone = bool(self.fetched("/envonly"))
        variants = [
            f"google-chrome {url}/v1", f"/usr/bin/google-chrome-stable {url}/v2", f"/opt/google/chrome/chrome {url}/v3",
            f"env FOO=1 google-chrome {url}/v4", f"sh -c 'google-chrome {url}/v5'", f"bash -lc \"google-chrome {url}/v6\"",
            f"nohup google-chrome {url}/v7 >/dev/null 2>&1 &", f"(setsid google-chrome {url}/v8 &)", f"timeout 5 google-chrome {url}/v9",
            f"xdg-open {url}/v10", f"gio open {url}/v11", f"sensible-browser {url}/v12", f"x-www-browser {url}/v13",
            f"python3 -m webbrowser {url}/v14", f"python3 -c \"import webbrowser; webbrowser.open('{url}/v15')\"", f"open {url}/v16",
            f"google-chrome --user-data-dir={HOME}/.config/google-chrome {url}/v17",
            f"google-chrome --headless --user-data-dir={HOME}/.config/google-chrome --dump-dom {url}/v18",
            f"f(){{ google-chrome \"$@\"; }}; f {url}/v19", f"for u in {url}/v20; do google-chrome $u; done",
            f"ln -s /usr/bin/google-chrome-stable /tmp/awg_chr; /tmp/awg_chr {url}/v21", f"cp /usr/bin/google-chrome-stable /tmp/awg_chr2",
            f"BROWSER=google-chrome python3 -c \"import webbrowser; webbrowser.open('{url}/v22')\"",
        ]
        not_denied = [v for v in variants if not guard.check_command(v).deny]
        self.assertEqual(not_denied, [], f"handoff variants the deny layer lets through: {not_denied}")
        time.sleep(1)
        later = [e for e in self.log if e[0].startswith("/v")]
        self.assertEqual(later, [], "a denied variant reached the server")
        print(f"\nSINGLE-INSTANCE: control handoff reproduced; environment alone stops it: {env_alone}; "
              f"{len(variants)} variants denied by the guard (none executed)")
