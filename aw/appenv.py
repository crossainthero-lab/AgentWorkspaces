"""Build the environment for processes started inside a workspace.

Rules (enforced here and covered by tests/unit/test_appenv.py):
  * The environment is built from scratch from a small allow-list. It is never a copy of the
    caller's environment minus a few names.
  * WAYLAND_DISPLAY / XDG_RUNTIME_DIR point at the workspace's private runtime dir, DISPLAY at the
    workspace's own XWayland (or is unset). No host display socket can appear.
  * Fail closed: if the workspace compositor socket does not exist, no environment is returned.
"""
from __future__ import annotations

import os
from typing import Dict, Optional

# Variables copied from the caller. Anything not listed here is dropped.
ALLOW = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "SHELL", "TZ", "TERM",
         "XDG_DATA_DIRS", "XDG_CONFIG_DIRS", "XCURSOR_THEME", "XCURSOR_SIZE")
ALLOW_PREFIX = ("LC_",)

# Names that must never reach a workspace app, even if a caller passes them in `extra`.
FORBIDDEN = ("WAYLAND_DISPLAY", "WAYLAND_SOCKET", "DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS",
             "DBUS_SYSTEM_BUS_ADDRESS", "SESSION_MANAGER", "XDG_SESSION_ID", "XDG_SESSION_TYPE",
             "XDG_SESSION_PATH", "XDG_SEAT", "XDG_VTNR", "GNOME_SETUP_DISPLAY", "GNOME_TERMINAL_SCREEN",
             "GNOME_TERMINAL_SERVICE", "DESKTOP_STARTUP_ID", "XDG_ACTIVATION_TOKEN", "SSH_AUTH_SOCK",
             "GPG_AGENT_INFO", "XDG_RUNTIME_DIR", "PIPEWIRE_REMOTE", "PULSE_SERVER", "GDK_BACKEND",
             "QT_QPA_PLATFORM", "MOZ_ENABLE_WAYLAND", "ELECTRON_OZONE_PLATFORM_HINT")


class WorkspaceNotRunning(Exception):
    pass


def build_env(rt_dir: str, x_display: Optional[str] = None, bus_address: Optional[str] = None,
              extra: Optional[Dict[str, str]] = None, host_env: Optional[Dict[str, str]] = None,
              wayland_name: str = "wayland-0") -> Dict[str, str]:
    """Return the environment for a workspace app. Raises WorkspaceNotRunning if the
    workspace socket is absent (never falls back to anything else)."""
    sock = os.path.join(rt_dir, wayland_name)
    if not os.path.isabs(rt_dir) or not os.path.exists(sock):
        raise WorkspaceNotRunning(f"workspace compositor socket not found: {sock}")
    host = os.environ if host_env is None else host_env
    env: Dict[str, str] = {}
    for k, v in host.items():
        if k in ALLOW or k.startswith(ALLOW_PREFIX):
            env[k] = v
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["XDG_RUNTIME_DIR"] = rt_dir
    env["WAYLAND_DISPLAY"] = wayland_name
    env["XDG_SESSION_TYPE"] = "wayland"
    env["XDG_CURRENT_DESKTOP"] = "labwc:wlroots"
    # toolkit hints: prefer native Wayland inside the workspace
    env["GDK_BACKEND"] = "wayland,x11"
    env["QT_QPA_PLATFORM"] = "wayland;xcb"
    env["MOZ_ENABLE_WAYLAND"] = "1"
    env["ELECTRON_OZONE_PLATFORM_HINT"] = "wayland"
    env["SDL_VIDEODRIVER"] = "wayland,x11"
    if x_display:
        env["DISPLAY"] = x_display
    if bus_address:
        env["DBUS_SESSION_BUS_ADDRESS"] = bus_address
    for k, v in (extra or {}).items():
        if k in FORBIDDEN:
            raise ValueError(f"refusing to pass {k} into a workspace")
        env[k] = v
    return env


def assert_no_host_sockets(env: Dict[str, str], host_runtime_dir: Optional[str]) -> None:
    """Defensive check used before every launch and in tests."""
    xrd = env.get("XDG_RUNTIME_DIR", "")
    if host_runtime_dir and os.path.realpath(xrd) == os.path.realpath(host_runtime_dir):
        raise AssertionError("workspace XDG_RUNTIME_DIR equals the host runtime dir")
    wd = env.get("WAYLAND_DISPLAY", "")
    if wd.startswith("/") or not wd.startswith("wayland-"):
        raise AssertionError(f"unexpected WAYLAND_DISPLAY {wd!r}")
    if not os.path.exists(os.path.join(xrd, wd)):
        raise AssertionError("workspace socket missing")
