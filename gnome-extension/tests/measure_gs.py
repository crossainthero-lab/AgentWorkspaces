#!/usr/bin/python3
"""Measure the real viewer inside the ISOLATED headless GNOME Shell (same setup as run_real.py): CPU, memory,
agent-action-to-viewer latency, and which processes load the NVIDIA GL stack. Prints JSON.

Environment honesty: headless Mutter with a virtual monitor, no real display, one machine. What this can and cannot
say is written next to each number in docs/measurements.md.
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(os.path.dirname(HERE))]
os.environ["AW_VIEWER_METRICS"] = "1"     # the viewer prints frame hand-off timestamps when it closes
from gnome_session import Session, REALISTIC_COPY, REALISTIC_SYSTEM  # noqa: E402
from live_env import Daemon, PROBE_APP, wait_for  # noqa: E402

VIEWER = "dev.agentworkspaces.Viewer"
HZ = os.sysconf("SC_CLK_TCK")


def cpu_ticks(pid):
    with open(f"/proc/{pid}/stat") as f:
        p = f.read().rsplit(")", 1)[1].split()
    return int(p[11]) + int(p[12])


def pss_kib(pid):
    try:
        for line in open(f"/proc/{pid}/smaps_rollup"):
            if line.startswith("Pss:"):
                return int(line.split()[1])
    except OSError:
        pass
    return None


def tree(pid):
    """pid and its descendants (exact parent links from /proc)."""
    kids = {}
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                ppid = int(open(f"/proc/{d}/stat").read().rsplit(")", 1)[1].split()[1])
                kids.setdefault(ppid, []).append(int(d))
            except (OSError, ValueError, IndexError):
                pass
    out, todo = [], [pid]
    while todo:
        p = todo.pop()
        out.append(p)
        todo += kids.get(p, [])
    return out


def sample(pids, seconds, work=None):
    t0 = time.monotonic()
    a = {p: cpu_ticks(p) for p in pids}
    if work:
        work(seconds)
    else:
        time.sleep(seconds)
    dt = time.monotonic() - t0
    return {p: round((cpu_ticks(p) - a[p]) / HZ / dt * 100, 1) for p in pids}


def maps_gl(pid):
    try:
        txt = open(f"/proc/{pid}/maps").read()
    except OSError:
        return None
    return {"nvidia_libs": txt.count("libnvidia") + txt.count("libEGL_nvidia") + txt.count("libGLX_nvidia"),
            "swrast_or_llvmpipe": ("libgallium" in txt) or ("swrast" in txt) or ("llvmpipe" in txt)}


def main():
    out = {"env": {}}
    s = Session(monitors=("1600x900",), copy_extensions=REALISTIC_COPY, enable_extensions=REALISTIC_SYSTEM,
                dump_user_settings=True)
    d = None
    try:
        s.start()
        time.sleep(2)
        s.eval("Main.overview.hide(); return 1")
        d = Daemon(s).start()
        c = d.client()
        ag = d.agent_client("research")
        c.call("workspace.create", name="research", agent_kind="claude-code", width=1280, height=720)
        lg = s.base + "/p.log"
        open(lg, "w").close()
        c.call("workspace.launch", workspace="research", argv=[sys.executable, PROBE_APP, "R", lg, "#2b6cb0"])
        wait_for(lambda: os.path.getsize(lg) > 0, 20)
        time.sleep(1.5)
        s.launch(["gnome-calculator"], "calc.log")
        time.sleep(3)
        s.chord("Control_L", "Alt_L", "Up")
        wait_for(lambda: any(w["app_id"] == VIEWER for w in s.state()["windows"]), 20)
        time.sleep(4)
        vpid = d.viewer_pids()[0]
        spid = s.shell.pid
        ws = c.call("workspace.get", workspace="research")
        comp = ws.get("compositor_pid")
        out["env"] = {"cpu_count": os.cpu_count(), "shell": subprocess.run(["gnome-shell", "--version"], capture_output=True,
                      text=True).stdout.strip(), "monitor": "virtual 1600x900 (headless)", "workspace": "1280x720, gpu=auto",
                      "viewer_pid_is_daemon_child": True}
        out["gl_libraries_mapped"] = {"gnome-shell (isolated)": maps_gl(spid), "viewer": maps_gl(vpid)}
        try:
            pm = subprocess.run(["nvidia-smi", "pmon", "-c", "1", "-s", "u"], capture_output=True, text=True, timeout=15).stdout
            listed = {int(l.split()[1]) for l in pm.splitlines() if l.strip() and not l.startswith("#") and l.split()[1].isdigit()}
            out["nvidia_pmon_lists"] = {"gnome-shell (isolated)": spid in listed, "viewer": vpid in listed}
        except (OSError, subprocess.SubprocessError):
            out["nvidia_pmon_lists"] = "nvidia-smi not available"
        shell_tree = tree(spid)

        # idle: viewer open, workspace idle
        r = sample([vpid, spid], 10)
        out["idle_10s_cpu_percent_of_one_core"] = {"viewer": r[vpid], "gnome-shell": r[spid]}
        out["pss_mib"] = {"viewer": round(pss_kib(vpid) / 1024, 1), "gnome-shell": round(pss_kib(spid) / 1024, 1)}

        # busy: the agent moves the pointer ~60 times a second for 8 s (probe repaints a label each move)
        def drive(seconds):
            end = time.monotonic() + seconds
            i = 0
            while time.monotonic() < end:
                ag.call("input.move", workspace="research", x=200 + (i * 7) % 900, y=200 + (i * 5) % 300)
                i += 1
                time.sleep(max(0, 1 / 60 - 0.002))
            out["busy_moves_sent"] = i
        r = sample([vpid, spid], 8, drive)
        out["busy_8s_60hz_cpu_percent_of_one_core"] = {"viewer": r[vpid], "gnome-shell": r[spid]}
        out["pss_mib_busy"] = {"viewer": round(pss_kib(vpid) / 1024, 1), "gnome-shell": round(pss_kib(spid) / 1024, 1)}
        # the same motion while the viewer is NOT visible (user is on a personal workspace): the workspace keeps
        # running, the viewer should stop capturing/painting
        s.chord("Control_L", "Alt_L", "Down")
        time.sleep(2)
        r = sample([vpid, spid], 8, drive)
        out["busy_8s_60hz_viewer_on_other_workspace_cpu_percent"] = {"viewer": r[vpid], "gnome-shell": r[spid]}
        s.chord("Control_L", "Alt_L", "Up")
        time.sleep(2)
        u0 = c.call("workspace.usage", workspace="research")
        time.sleep(5)
        u1 = c.call("workspace.usage", workspace="research")
        out["workspace_idle_cpu_percent_5s"] = round((u1["cpu_ns"] - u0["cpu_ns"]) / 5e9 * 100, 2)
        out["workspace_memory_mib"] = round(u1["memory_bytes"] / 2**20, 1)

        # latency: monotonic time of an agent move (just before the call) -> first frame the viewer handed to GTK
        marks = []
        for i in range(30):
            time.sleep(0.45)
            t = time.monotonic()
            ag.call("input.move", workspace="research", x=150 + i * 17, y=250 + (i % 5) * 31)
            marks.append(t)
        time.sleep(1)
        # close the viewer window: it prints its frame timeline on exit
        s.eval("""for (const a of global.get_window_actors()) { const w = a.meta_window;
                  if ((w.get_gtk_application_id?.() ?? w.get_wm_class()) === 'dev.agentworkspaces.Viewer') w.delete(global.get_current_time()); }
                  return 1""")
        time.sleep(2)
        vlog = open(os.path.join(d.base, "viewer.log")).read().splitlines()
        frames = [float(l.split()[1]) for l in vlog if l.startswith("frame ")]
        out["viewer_renderer"] = next((l for l in vlog if l.startswith("renderer=")), None)
        lat = []
        for t in marks:
            nxt = next((f for f in frames if f > t), None)
            if nxt is not None and nxt - t < 0.5:
                lat.append((nxt - t) * 1000)
        lat.sort()
        if lat:
            out["agent_move_to_viewer_frame_ms"] = {"n": len(lat), "median": round(lat[len(lat) // 2], 1),
                                                    "p95": round(lat[int(len(lat) * 0.95) - 1], 1), "min": round(lat[0], 1),
                                                    "max": round(lat[-1], 1)}
        out["note_latency"] = ("agent API call -> daemon -> workspace compositor -> screencopy -> viewer main loop "
                               "(texture handed to GTK). Excludes GTK render + the isolated shell's composite + scanout "
                               "(headless virtual monitor: one 60 Hz frame at most).")
    finally:
        if d:
            d.stop()
        s.stop()
        out["leftover"] = s.mine()
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
