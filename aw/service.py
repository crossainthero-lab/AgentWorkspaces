"""Make sure the AgentWorkspaces service (the daemon) is running, without ever hiding a failure.

Order: the systemd user unit `agent-workspaces.service` (when it is installed and this is the normal runtime
directory), else the daemon itself, started the way the CLI starts it. Every wait is bounded. The result says exactly
what was tried and why it failed, so a viewer or an extension can show an accurate message instead of doing nothing.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from typing import Tuple

from . import paths

UNIT = "agent-workspaces.service"


def alive(path: str | None = None) -> bool:
    """True when something answers on the API socket."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.connect(path or paths.api_socket())
        return True
    except OSError:
        return False
    finally:
        s.close()


def _wait(seconds: float, path: str | None = None) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if alive(path):
            return True
        time.sleep(0.1)
    return False


def unit_installed() -> bool:
    cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.exists(os.path.join(cfg, "systemd", "user", UNIT))


def use_systemd() -> bool:
    """The unit serves the default socket only: a private AW_BASE_DIR (tests, side-by-side instances) never touches it."""
    default = os.path.join(paths.host_runtime_dir(), "agent-workspaces")
    return not os.environ.get("AW_BASE_DIR") and paths.base_dir() == default and unit_installed()


def start_unit(timeout: float = 5.0) -> Tuple[bool, str]:
    try:
        r = subprocess.run(["systemctl", "--user", "start", UNIT], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"systemctl --user start {UNIT}: {e}"
    if r.returncode != 0:
        return False, f"systemctl --user start {UNIT} failed: {(r.stderr or r.stdout).strip() or r.returncode}"
    return True, ""


def spawn_daemon() -> Tuple[bool, str]:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {k: v for k, v in os.environ.items() if not k.startswith("AW_TOKEN") and not k.startswith("AW_ADMIN")}
    env.setdefault("AW_IDLE_EXIT_SECONDS", "1800")          # on-demand daemon: leave nothing behind when unused
    env["PYTHONPATH"] = root + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        log = os.path.join(paths.ensure_private_dir(paths.base_dir()), "daemon.log")
        subprocess.Popen([sys.executable, "-m", "aw.daemon"], env=env, stdin=subprocess.DEVNULL,
                         stdout=open(log, "ab"), stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as e:
        return False, f"could not run the daemon: {e}"
    return True, ""


def ensure_daemon(timeout: float = 8.0) -> Tuple[bool, str]:
    """(running, message). message is empty on success, else what to tell the person."""
    if alive():
        return True, ""
    if os.environ.get("AW_NO_AUTOSTART"):
        return False, "automatic start is switched off (AW_NO_AUTOSTART)"
    tried = []
    if use_systemd():
        ok, why = start_unit()
        if ok and _wait(timeout):
            return True, ""
        tried.append(why or "the unit started but the socket did not appear")
    ok, why = spawn_daemon()
    if ok and _wait(timeout):
        return True, ""
    tried.append(why or f"the daemon did not open {paths.api_socket()} within {timeout:.0f} s "
                 f"(see {os.path.join(paths.base_dir(), 'daemon.log')})")
    return False, "; ".join(tried)
