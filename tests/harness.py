"""Test harness for REAL integration tests.

HostSim is a stand-in for the user's personal desktop: a throw-away headless labwc that the viewer
window is shown in. It exists so that no test ever opens a window on the real desktop and no test
ever sends input to the real session. Everything here is clearly NOT the user's session.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from typing import Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aw import appenv  # noqa: E402
from aw.session import WorkspaceSession  # noqa: E402


def short_base(tag: str) -> str:
    """A short private base dir (Wayland socket paths are limited to 108 bytes)."""
    b = os.path.join(f"/run/user/{os.getuid()}", f"awt-{tag}-{os.getpid()}")
    os.makedirs(b, mode=0o700, exist_ok=True)
    return b


class HostSim:
    def __init__(self, base: str, size=(1600, 900), gpu: bool = False):
        self.dir = os.path.join(base, "hostsim")
        self.rt = os.path.join(self.dir, "rt")
        os.makedirs(self.rt, mode=0o700, exist_ok=True)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.dir,
               "XDG_RUNTIME_DIR": self.rt, "WLR_BACKENDS": "headless", "WLR_LIBINPUT_NO_DEVICES": "1",
               "WLR_RENDERER": "pixman", "XKB_DEFAULT_LAYOUT": "us", "GSETTINGS_BACKEND": "memory"}
        self.proc = subprocess.Popen(["/usr/bin/env", "-i"] + [f"{k}={v}" for k, v in env.items()] + ["labwc"],
                                     stdout=open(os.path.join(self.dir, "labwc.log"), "wb"),
                                     stderr=subprocess.STDOUT, start_new_session=True)
        sock = os.path.join(self.rt, "wayland-0")
        for _ in range(100):
            if os.path.exists(sock):
                break
            time.sleep(0.05)
        self.sock = sock
        self.sess = WorkspaceSession(sock)
        self.sess.set_display(*size)
        self.children: List[subprocess.Popen] = []

    def env(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        e = appenv.build_env(self.rt)
        e["HOME"] = self.dir
        e["GSETTINGS_BACKEND"] = "memory"
        e["NO_AT_BRIDGE"] = "1"
        e["GTK_USE_PORTAL"] = "0"
        e["GDK_BACKEND"] = "wayland"
        e["GSK_RENDERER"] = "cairo"
        e.update(extra or {})
        return e

    def spawn(self, argv: List[str], extra_env: Optional[Dict[str, str]] = None, log: Optional[str] = None):
        e = self.env(extra_env)
        assert "DISPLAY" not in e and e["XDG_RUNTIME_DIR"] == self.rt
        p = subprocess.Popen(argv, env=e, cwd=ROOT, stdin=subprocess.DEVNULL,
                             stdout=open(log or os.devnull, "wb"), stderr=subprocess.STDOUT,
                             start_new_session=True)
        self.children.append(p)
        return p

    def shot(self) -> bytes:
        return self.sess.screenshot_png()

    def close(self):
        try:
            self.sess.close()
        except Exception:
            pass
        for p in self.children:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for p in self.children:
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.dir, ignore_errors=True)
