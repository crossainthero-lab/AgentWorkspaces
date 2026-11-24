"""Fully private environment for every real-agent run and every stand-in desktop test.

RULE (after an incident in which a test URL opened in the user's real, running Chrome): no process of a test may see the
user's real HOME, real XDG_CONFIG_HOME / XDG_DATA_HOME / XDG_CACHE_HOME / XDG_STATE_HOME, real browser profiles, the
real D-Bus session bus, the real XDG_RUNTIME_DIR or the real Wayland / X sockets. This module builds such an
environment (throw-away HOME and XDG dirs, private runtime dir, system-only PATH, no display or bus variables) and
CHECKS it: `assert_private()` aborts the run, in code, before any real agent starts.

Credentials, in the minimal form each agent supports:
  Claude Code   CLAUDE_CONFIG_DIR=<private>/claude containing one symlink to the login file (nothing else of ~/.claude)
  Hermes        SPARK_LLM_API_KEY in the environment of that process only (read from the user's .env in memory)
"""
from __future__ import annotations

import os
import pwd
import re
import shutil
import tempfile
from typing import Dict, Iterable, List, Optional

SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASS|CREDENTIAL|AUTH", re.I)
FORBIDDEN_NAMES = ("DISPLAY", "WAYLAND_DISPLAY", "WAYLAND_SOCKET", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS",
                   "SESSION_MANAGER", "SSH_AUTH_SOCK", "GNOME_SETUP_DISPLAY", "XDG_SESSION_ID", "DESKTOP_STARTUP_ID",
                   "XDG_ACTIVATION_TOKEN", "GPG_AGENT_INFO", "PULSE_SERVER", "PIPEWIRE_REMOTE")
SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def real_home() -> str:
    return pwd.getpwuid(os.getuid()).pw_dir


def real_runtime_dir() -> str:
    return f"/run/user/{os.getuid()}"


class PrivateEnv:
    def __init__(self, tag: str = "priv", parent: Optional[str] = None):
        parent = parent or real_runtime_dir()
        self.root = tempfile.mkdtemp(prefix=f"awt-{tag}-", dir=parent)
        os.chmod(self.root, 0o700)
        self.home = self._mk("home")
        self.config = self._mk("home/.config")
        self.data = self._mk("home/.local/share")
        self.cache = self._mk("home/.cache")
        self.state = self._mk("home/.local/state")
        self.rt = self._mk("rt")
        self.tmp = self._mk("tmp")

    def _mk(self, rel: str) -> str:
        p = os.path.join(self.root, rel)
        os.makedirs(p, mode=0o700, exist_ok=True)
        return p

    def env(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        e = {"PATH": SYSTEM_PATH, "HOME": self.home, "XDG_CONFIG_HOME": self.config, "XDG_DATA_HOME": self.data,
             "XDG_CACHE_HOME": self.cache, "XDG_STATE_HOME": self.state, "XDG_RUNTIME_DIR": self.rt, "TMPDIR": self.tmp,
             "LANG": "C.UTF-8", "TERM": "dumb", "NO_COLOR": "1", "BROWSER": "/bin/true", "USER": pwd.getpwuid(os.getuid()).pw_name,
             "LOGNAME": pwd.getpwuid(os.getuid()).pw_name, "GIT_TERMINAL_PROMPT": "0"}
        e.update(extra or {})
        return e

    def claude_login(self) -> Dict[str, str]:
        """CLAUDE_CONFIG_DIR with only a symlink to the login file: the process cannot see the rest of ~/.claude."""
        d = self._mk("claude")
        src = os.path.join(real_home(), ".claude", ".credentials.json")
        if os.path.exists(src):
            os.symlink(src, os.path.join(d, ".credentials.json"))
        return {"CLAUDE_CONFIG_DIR": d}

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


def assert_private(env: Dict[str, str], allowed_roots: Iterable[str] = (), label: str = "agent") -> List[str]:
    """Raise AssertionError when `env` could expose the real home / desktop / bus. `allowed_roots`: private directories
    (the stand-in desktop's) whose paths may appear. Returns the printable plan (secrets redacted)."""
    rh, rrt = real_home(), real_runtime_dir()
    allowed = tuple(os.path.realpath(a) for a in allowed_roots)
    problems = []
    for k, v in env.items():
        low = os.path.realpath(v) if v.startswith("/") else v
        under_allowed = any(low == a or low.startswith(a + os.sep) for a in allowed)
        if k in FORBIDDEN_NAMES and not under_allowed and not (k in ("WAYLAND_DISPLAY",) and not v.startswith("/") and allowed):
            problems.append(f"{k} is set")
        if SECRET_NAME.search(k):
            continue
        if rh and (rh in v) and not under_allowed:
            problems.append(f"{k} contains the real home path")
        if k == "XDG_RUNTIME_DIR" and os.path.realpath(v) == rrt:
            problems.append("XDG_RUNTIME_DIR is the real runtime dir")
        if re.search(re.escape(rrt) + r"/(bus|wayland-\d|pipewire|pulse|\.X)", v):
            problems.append(f"{k} names a real session socket")
        if k.startswith("XDG_") and k.endswith(("_HOME",)) and not (low.startswith(os.path.realpath(env.get("HOME", "/nonexistent")) + os.sep) or under_allowed):
            problems.append(f"{k} is outside the private home")
    if "HOME" not in env or os.path.realpath(env["HOME"]) == os.path.realpath(rh):
        problems.append("HOME is the real home")
    plan = [f"{label} environment ({len(env)} variables):"]
    for k in sorted(env):
        plan.append(f"  {k}=" + ("<redacted>" if SECRET_NAME.search(k) else env[k]))
    if problems:
        raise AssertionError(f"{label}: NOT private, run aborted: {sorted(set(problems))}")
    return plan
