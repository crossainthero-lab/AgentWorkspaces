"""Optional per-workspace filesystem/process sandbox for the APPS of a workspace (bubblewrap).

Opt-in (`create --sandbox`). What it does (measured and tested, see docs/isolation-model.md):
  * the app sees a private HOME (the workspace's own home dir), its own runtime dir (Wayland/D-Bus sockets of
    this workspace only), a private /tmp and /var/tmp, and a read-only view of the system
    (/usr, a short list of /etc files, /sys)
  * your real home, your real XDG_RUNTIME_DIR (the API socket, token files, the host Wayland/D-Bus/PipeWire
    sockets, other workspaces) are NOT mounted: a path outside the allowed set does not exist inside
  * only the project directories you pass with `--sandbox-mount PATH[:ro|:rw]` are added (default ro)
  * own PID namespace (the host's processes and /proc/<pid>/environ are not visible), own IPC and UTS
    namespace, `--new-session` (no TIOCSTI injection into your terminal), all capabilities dropped,
    `--die-with-parent`
What it does NOT do: no seccomp filter, the network is shared with the host by default (so abstract unix sockets of
the host are reachable and the app can use your network; `--sandbox-no-network` unshares it), the GPU devices are passed through, the compositor and the
workspace's session bus are outside the sandbox and are trusted. It is not a hardened boundary against kernel
or driver exploits. Graphical isolation is a separate property from this one.
"""
from __future__ import annotations

import os
import shutil
from typing import Any, Dict, List, Optional, Tuple

# files of /etc an app legitimately needs (everything else in /etc stays invisible)
ETC_FILES = ("ssl", "ca-certificates", "pki", "fonts", "resolv.conf", "hosts", "nsswitch.conf", "passwd", "group",
             "ld.so.cache", "ld.so.conf", "ld.so.conf.d", "alternatives", "localtime", "timezone", "glvnd",
             "vulkan", "OpenCL", "xdg", "gtk-3.0", "mime.types", "host.conf", "gai.conf", "protocols", "services",
             "X11", "os-release", "locale.alias")
# directories whose exposure would defeat the point; a mount may not be one of them or contain one
SENSITIVE_HOME = (".ssh", ".gnupg", ".aws", ".kube", ".docker", ".netrc", ".config/gh", ".config/google-chrome",
                  ".config/chromium", ".mozilla", ".config/BraveSoftware", ".local/share/keyrings",
                  ".config/agent-workspaces", ".claude", ".codex", ".config/gcloud", ".password-store")
FORBIDDEN_ROOTS = ("/", "/run", "/proc", "/sys", "/dev", "/etc", "/var", "/boot", "/root")


class SandboxError(Exception):
    pass


def available() -> Optional[str]:
    return shutil.which("bwrap")


def parse_mounts(items: Optional[List[Any]]) -> List[Dict[str, Any]]:
    out = []
    for it in items or []:
        if isinstance(it, dict):
            path, ro = it.get("path"), bool(it.get("ro", True))
        else:
            path, ro = str(it), True
            for suffix, val in ((":rw", False), (":ro", True)):
                if path.endswith(suffix):
                    path, ro = path[:-len(suffix)], val
        out.append({"path": path, "ro": ro})
    return out


def validate_mounts(mounts: List[Dict[str, Any]], base_dir: str, host_runtime_dir: str,
                    home: Optional[str] = None) -> List[Dict[str, Any]]:
    home = os.path.realpath(home or os.path.expanduser("~"))
    forbidden = [os.path.realpath(base_dir)]
    hrt = os.path.realpath(host_runtime_dir)
    sensitive = [os.path.join(home, s) for s in SENSITIVE_HOME]
    out = []
    for m in mounts:
        p = m.get("path")
        if not isinstance(p, str) or not os.path.isabs(p):
            raise SandboxError(f"sandbox mount must be an absolute path: {p!r}")
        rp = os.path.realpath(p)
        if not os.path.exists(rp):
            raise SandboxError(f"sandbox mount does not exist: {p}")
        if rp in FORBIDDEN_ROOTS:
            raise SandboxError(f"refusing to mount {rp}")
        if rp == hrt or hrt.startswith(rp + os.sep):
            raise SandboxError(f"refusing to mount {rp}: it is or contains your runtime directory ({hrt}) with "
                               "the host display, session bus, audio and API sockets")
        for f in forbidden:
            if rp == f or rp.startswith(f + os.sep) or f.startswith(rp + os.sep):
                raise SandboxError(f"refusing to mount {rp}: it contains or is inside the AgentWorkspaces / "
                                   "runtime directory (API socket, credentials, other workspaces)")
        for s in sensitive:
            if os.path.exists(s) and (rp == s or rp.startswith(s + os.sep) or s.startswith(rp + os.sep)):
                raise SandboxError(f"refusing to mount {rp}: it contains or is inside {s} (credentials). "
                                   "Mount the project directory itself.")
        out.append({"path": rp, "ro": bool(m.get("ro", True))})
    return out


def build_argv(ws_dir: str, ws_rt: str, ws_home: str, mounts: List[Dict[str, Any]], x_display: Optional[str] = None,
               cwd: Optional[str] = None, gpu: bool = True, net: bool = True) -> List[str]:
    """The bwrap prefix: `bwrap <this> -- program args...`. Pure function (unit-testable)."""
    a: List[str] = ["bwrap", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup-try",
                    "--new-session", "--die-with-parent", "--cap-drop", "ALL",
                    "--ro-bind", "/usr", "/usr", "--ro-bind-try", "/opt", "/opt", "--symlink", "usr/bin", "/bin", "--symlink", "usr/sbin", "/sbin",
                    "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
                    "--proc", "/proc", "--dev", "/dev", "--ro-bind-try", "/sys", "/sys",
                    "--tmpfs", "/tmp", "--tmpfs", "/var/tmp", "--ro-bind-try", "/var/cache/fontconfig",
                    "/var/cache/fontconfig", "--ro-bind-try", "/run/systemd/resolve", "/run/systemd/resolve"]
    if not net:
        a += ["--unshare-net"]
    for f in ETC_FILES:
        a += ["--ro-bind-try", f"/etc/{f}", f"/etc/{f}"]
    if gpu:
        a += ["--dev-bind-try", "/dev/dri", "/dev/dri"]
        for n in ("nvidiactl", "nvidia0", "nvidia1", "nvidia-uvm", "nvidia-uvm-tools", "nvidia-modeset"):
            a += ["--dev-bind-try", f"/dev/{n}", f"/dev/{n}"]
    # the workspace's own home and runtime dir (Wayland socket, session bus), same paths as outside
    a += ["--bind", ws_home, ws_home, "--bind", ws_rt, ws_rt]
    if x_display and x_display.startswith(":"):
        sock = f"/tmp/.X11-unix/X{x_display[1:].split('.')[0]}"
        a += ["--bind-try", sock, sock]          # only THIS workspace's X socket, not the host's X0
    for m in mounts:
        a += ["--ro-bind" if m.get("ro", True) else "--bind", m["path"], m["path"]]
    a += ["--setenv", "XDG_RUNTIME_DIR", ws_rt, "--chdir", cwd or ws_home]
    return a
