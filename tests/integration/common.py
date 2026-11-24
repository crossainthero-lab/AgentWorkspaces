"""Shared fixtures for the REAL integration tests (they start real labwc compositors and real apps).

All GUI programs run only inside throw-away workspace compositors (or the HostSim stand-in for the user's
desktop). Nothing here talks to the real desktop session.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import zlib
import struct

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from aw.client import Client  # noqa: E402

PROBE = os.path.join(ROOT, "tests", "apps", "gtk_probe.py")
BASE = None
DAEMON = None


def have(cmd):
    return shutil.which(cmd) is not None


def _spawn_daemon(extra_env=None):
    global DAEMON
    env = {"PATH": os.environ["PATH"], "HOME": os.path.expanduser("~"), "AW_BASE_DIR": BASE, "PYTHONPATH": ROOT,
           "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
           "DBUS_SESSION_BUS_ADDRESS": os.environ.get("DBUS_SESSION_BUS_ADDRESS",
                                                      f"unix:path=/run/user/{os.getuid()}/bus"),
           "LANG": "C.UTF-8"}   # NB: no WAYLAND_DISPLAY / DISPLAY: the daemon never needs the host display
    env.update(extra_env or {})
    DAEMON = subprocess.Popen([sys.executable, "-m", "aw.daemon"], env=env, cwd=ROOT,
                              stdout=open(os.path.join(BASE, "daemon.log"), "wb"), stderr=subprocess.STDOUT,
                              start_new_session=True)
    for _ in range(200):
        if os.path.exists(os.path.join(BASE, "api.sock")):
            try:
                import socket as _s
                t = _s.socket(_s.AF_UNIX)
                t.connect(os.path.join(BASE, "api.sock"))
                t.close()
                break
            except OSError:
                pass
        time.sleep(0.05)


def start_daemon(tag="int", extra_env=None):
    global BASE
    BASE = os.path.join(f"/run/user/{os.getuid()}", f"awt-{tag}-{os.getpid()}")
    os.makedirs(BASE, mode=0o700, exist_ok=True)
    os.environ["AW_BASE_DIR"] = BASE
    _spawn_daemon(extra_env)
    return Client(autostart=False)


def restart_daemon(kill=True, extra_env=None):
    """Kill the daemon by its exact pid (SIGKILL = a crash, SIGTERM = a clean stop) and start a new one on the
    same base dir. Workspaces keep running; the new daemon must adopt them."""
    global DAEMON
    old = DAEMON
    if old and old.poll() is None:
        old.send_signal(signal.SIGKILL if kill else signal.SIGTERM)
        old.wait(10)
    _spawn_daemon(extra_env)
    return Client(autostart=False)


def stop_daemon(client=None):
    global DAEMON
    try:
        c = client or Client(autostart=False)
        for w in c.call("workspace.list")["workspaces"]:
            c.call("workspace.destroy", workspace=w["id"])
    except Exception:
        pass
    if DAEMON and DAEMON.poll() is None:
        DAEMON.terminate()
        try:
            DAEMON.wait(5)
        except subprocess.TimeoutExpired:
            DAEMON.kill()
    shutil.rmtree(BASE, ignore_errors=True) if BASE else None


def decode_png(png):
    pos, idat, ihdr = 8, b"", None
    while pos < len(png):
        ln, tag = struct.unpack(">I4s", png[pos:pos + 8])
        data = png[pos + 8:pos + 8 + ln]
        if tag == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", data)
        elif tag == b"IDAT":
            idat += data
        pos += 12 + ln
    w, h = ihdr[0], ihdr[1]
    raw = zlib.decompress(idat)
    rows = [raw[y * (w * 3 + 1) + 1:(y + 1) * (w * 3 + 1)] for y in range(h)]
    return w, h, rows


def pixel(png, x, y):
    w, h, rows = decode_png(png)
    r = rows[y]
    return tuple(r[x * 3:x * 3 + 3])


def log_events(path):
    if not os.path.exists(path):
        return []
    out = []
    for line in open(path):
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def wait_for(pred, timeout=10.0, step=0.1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


def launch_probe(c, ws, banner, color, logname):
    log = os.path.join(BASE, logname)
    open(log, "w").close()
    r = c.call("workspace.launch", workspace=ws, argv=["/usr/bin/python3", PROBE, banner, log, color])
    assert wait_for(lambda: any(e.get("ev") == "ready" for e in log_events(log)), 15), "probe did not start"
    assert wait_for(lambda: [w for w in c.call("window.list", workspace=ws)["windows"] if w["maximized"]], 10), \
        "window never became maximized"
    time.sleep(1.0)
    return log, r
