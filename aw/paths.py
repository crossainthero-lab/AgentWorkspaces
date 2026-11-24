"""Filesystem locations. Everything lives under the user's runtime dir or XDG dirs; nothing global."""
from __future__ import annotations

import os


def host_runtime_dir() -> str:
    d = os.environ.get("XDG_RUNTIME_DIR")
    if not d:
        d = f"/run/user/{os.getuid()}"
    return d


def base_dir() -> str:
    """Runtime base. Override with AW_BASE_DIR (tests, side-by-side instances)."""
    return os.environ.get("AW_BASE_DIR") or os.path.join(host_runtime_dir(), "agent-workspaces")


def api_socket() -> str:
    return os.path.join(base_dir(), "api.sock")


def ws_dir(ws_id: str) -> str:
    return os.path.join(base_dir(), ws_id)


def ws_rt(ws_id: str) -> str:
    """The workspace's private XDG_RUNTIME_DIR (holds its Wayland socket and D-Bus socket)."""
    return os.path.join(ws_dir(ws_id), "rt")


def state_dir() -> str:
    d = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(d, "agent-workspaces")


def data_dir() -> str:
    """Persistent data owned by AgentWorkspaces (named browser profiles, authorised profile copies)."""
    d = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(d, "agent-workspaces")


def config_dir() -> str:
    d = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(d, "agent-workspaces")


def ensure_private_dir(path: str) -> str:
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path
