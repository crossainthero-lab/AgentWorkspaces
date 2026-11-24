#!/usr/bin/python3
"""Containment probe run INSIDE a workspace (sandboxed or not). Reports what the process can reach, as JSON,
to the file given as argv[1]. Arguments: out_json canary_path real_home api_socket token_file other_ws_dir
ro_dir rw_dir host_runtime_dir. It only reads existence/readability, it never prints secret contents."""
import json
import os
import socket
import sys

out, canary, real_home, api_sock, token_file, other_ws, ro_dir, rw_dir, host_rt = sys.argv[1:10]
abstract_name = sys.argv[10] if len(sys.argv) > 10 else None
r = {}


def can(fn):
    try:
        return bool(fn())
    except Exception:
        return False


r["canary_readable"] = can(lambda: open(canary).read().startswith("CANARY"))
def _entries():
    try:
        return sorted(os.listdir(real_home))
    except OSError:
        return []


r["real_home_entries"] = _entries()
r["api_socket_visible"] = can(lambda: os.path.exists(api_sock))
r["token_file_readable"] = can(lambda: len(open(token_file).read()) > 10)


def connect():
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(2)
    s.connect(api_sock)
    s.close()
    return True


r["api_socket_connectable"] = can(connect)
def connect_abstract():
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(2)
    s.connect("\0" + abstract_name)
    s.close()
    return True


r["abstract_socket_connectable"] = can(connect_abstract) if abstract_name else None
r["network_namespace"] = os.readlink("/proc/self/ns/net")
r["other_workspace_dir_visible"] = can(lambda: os.path.exists(other_ws))
r["host_runtime_entries"] = can(lambda: True) and sorted(os.listdir(host_rt)) if os.path.isdir(host_rt) else []
r["host_wayland_socket_visible"] = can(lambda: os.path.exists(os.path.join(host_rt, "wayland-0")))
r["host_session_bus_visible"] = can(lambda: os.path.exists(os.path.join(host_rt, "bus")))
r["host_pids_visible"] = sum(1 for n in os.listdir("/proc") if n.isdigit())
r["parent_environ_readable"] = can(lambda: len(open(f"/proc/{os.getppid()}/environ", "rb").read()) > 0)
r["ssh_dir_visible"] = can(lambda: os.path.exists(os.path.join(real_home, ".ssh")))


def write(path):
    with open(path, "w") as f:
        f.write("x")
    os.unlink(path)
    return True


r["write_usr"] = can(lambda: write("/usr/aw-probe"))
r["write_ro_mount"] = can(lambda: write(os.path.join(ro_dir, "aw-probe-ro")))
r["write_rw_mount"] = can(lambda: write(os.path.join(rw_dir, "aw-probe-rw")))
r["write_home"] = can(lambda: write(os.path.join(os.environ["HOME"], "aw-probe-home")))
marker = os.path.join("/tmp", "aw-probe-tmp-" + str(os.getpid()))
r["write_tmp"] = can(lambda: open(marker, "w").write("t"))
r["tmp_marker"] = marker
r["home"] = os.environ.get("HOME")
r["xdg_runtime_dir"] = os.environ.get("XDG_RUNTIME_DIR")
r["wayland_display"] = os.environ.get("WAYLAND_DISPLAY")
r["uid"] = os.getuid()
with open(out, "w") as f:
    json.dump(r, f)
