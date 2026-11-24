#!/usr/bin/env python3
"""Collect GPU-acceleration evidence on THIS machine (needs an NVIDIA driver; prints what it measured).

1. compositor renderer: starts a workspace compositor with wlroots debug logging and prints the EGL vendor/driver
2. GPU processes: `nvidia-smi pmon` while a Vulkan app (vkcube) and a GTK4 app run inside a workspace
3. the same with gpu=off: the compositor must NOT appear as a GPU process
"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests", "integration"), os.path.join(ROOT, "tests")]
import common as K  # noqa: E402


def pmon():
    out = subprocess.run(["nvidia-smi", "pmon", "-c", "3", "-s", "um"], capture_output=True, text=True).stdout
    return out


def comm_pids(c, ws_id):
    r = subprocess.run(["systemctl", "--user", "show", f"aw-{ws_id}.slice", "-p", "ControlGroup"], capture_output=True, text=True).stdout
    cg = "/sys/fs/cgroup" + r.strip().split("=", 1)[1]
    d = {}
    for root, _, files in os.walk(cg):
        if "cgroup.procs" in files:
            for p in open(os.path.join(root, "cgroup.procs")).read().split():
                try:
                    d[int(p)] = open(f"/proc/{p}/comm").read().strip()
                except OSError:
                    pass
    return d


def main():
    import signal
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(1))
    print("GPU:", subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"], capture_output=True, text=True).stdout.strip())
    # 1. renderer
    base = K.short = os.path.join(f"/run/user/{os.getuid()}", f"awt-gpu-{os.getpid()}")
    rt = os.path.join(base, "rt"); os.makedirs(rt, mode=0o700)
    env = {"PATH": os.environ["PATH"], "HOME": base, "XDG_RUNTIME_DIR": rt, "WLR_BACKENDS": "headless", "WLR_LIBINPUT_NO_DEVICES": "1",
           "WLR_RENDERER": "gles2", "WLR_RENDER_DRM_DEVICE": "/dev/dri/renderD128"}
    p = subprocess.Popen(["/usr/bin/env", "-i"] + [f"{k}={v}" for k, v in env.items()] + ["labwc", "-d"], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, start_new_session=True)
    time.sleep(2.5)
    p.terminate()
    try:
        log = p.communicate(timeout=5)[0]
    except subprocess.TimeoutExpired:
        p.kill(); log = ""
    print("\n== compositor renderer (wlroots debug log, headless backend, gles2 on /dev/dri/renderD128)")
    for l in log.splitlines():
        if any(k in l for k in ("EGL vendor", "EGL driver name", "Opening DRM render node", "Using EGL device", "WLR_RENDERER option")):
            print("  ", l.split("] ", 1)[-1] if "] " in l else l)
    import shutil
    shutil.rmtree(base, ignore_errors=True)
    # 2/3. pmon
    c = K.start_daemon("gpu")
    try:
        for gpu in ("auto", "off"):
            w = c.call("workspace.create", name=f"g-{gpu}", width=1280, height=720, gpu=gpu, xwayland=False)
            c.call("workspace.launch", workspace=f"g-{gpu}", argv=["vkcube", "--wsi", "wayland"])
            time.sleep(5)
            pids = comm_pids(c, w["id"])
            out = pmon()
            print(f"\n== gpu={gpu}: processes in this workspace's cgroup: {sorted(set(pids.values()))}")
            print("   nvidia-smi pmon (3 samples, only rows whose PID belongs to this workspace):")
            for l in out.splitlines():
                f = l.split()
                if l.startswith("#") and "gpu" in l or l.startswith("# Idx"):
                    print("   ", l)
                elif len(f) > 3 and f[1].isdigit() and int(f[1]) in pids:
                    print("   ", l)
            if not any(len(l.split()) > 3 and l.split()[1].isdigit() and int(l.split()[1]) in pids for l in out.splitlines()):
                print("    (none of this workspace's processes use the GPU)")
            c.call("workspace.destroy", workspace=f"g-{gpu}")
    finally:
        K.stop_daemon(c)


if __name__ == "__main__":
    main()
