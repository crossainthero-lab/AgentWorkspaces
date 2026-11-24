#!/usr/bin/env python3
"""Viewer measurements. The viewer runs in HostSim (a headless stand-in desktop, never the real one).

latency = time from an agent pointer move in the workspace until the pixels of the viewer window (read from the
HostSim compositor with screencopy) change. Method: 30 samples, each polling HostSim region captures back-to-back.
"""
import json
import os
import statistics
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests"), os.path.join(ROOT, "tests", "integration")]
import common as K  # noqa: E402
from harness import HostSim  # noqa: E402


def proc_stats(pid):
    st = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
    ticks = int(st[11]) + int(st[12])
    pss = int([l for l in open(f"/proc/{pid}/smaps_rollup") if l.startswith("Pss:")][0].split()[1])
    return ticks / os.sysconf("SC_CLK_TCK"), pss


def main():
    import signal
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(1))   # let finally: clean up
    c = K.start_daemon("mview")
    host = HostSim(K.BASE, size=(1400, 900))
    out = {}
    try:
        c.call("workspace.create", name="v", width=1280, height=720)
        log = os.path.join(K.BASE, "v.log"); open(log, "w").close()
        c.call("workspace.launch", workspace="v", argv=[sys.executable, K.PROBE, "VIEW", log, "#2b6cb0"])
        K.wait_for(lambda: c.call("window.list", workspace="v")["windows"], 15)
        time.sleep(1.5)
        vp = host.spawn([sys.executable, "-m", "aw.viewer", "v"], {"AW_BASE_DIR": K.BASE, "PYTHONPATH": K.ROOT})
        K.wait_for(lambda: c.call("workspace.get", workspace="v")["viewers"] == 1, 15)
        time.sleep(2)
        w = [x for x in host.sess.list_windows() if "AgentWorkspaces" in x["title"]][0]
        host.sess.window_action(w["id"], "maximize")
        time.sleep(1.5)
        # idle viewer cost
        t0, _ = proc_stats(vp.pid); w0 = time.monotonic(); time.sleep(8)
        t1, pss = proc_stats(vp.pid)
        out["viewer_idle"] = {"cpu_percent_8s": round((t1 - t0) / (time.monotonic() - w0) * 100, 2), "pss_kib": pss}
        # input->viewer pixels
        region = [0, 150, 1400, 200]   # band containing the probe's 'pointer: x,y' label as drawn inside the viewer
        lat = []
        for i in range(33):
            base = host.sess.capture_raw(region)[2]
            t = time.perf_counter()
            c.call("input.move", workspace="v", x=300 + (i % 2) * 200 + i * 3, y=300)
            for _ in range(200):
                if host.sess.capture_raw(region)[2] != base:
                    break
            lat.append((time.perf_counter() - t) * 1000)
        lat = sorted(lat[3:])
        out["agent_move_to_viewer_pixels_ms"] = {"n": len(lat), "median": round(statistics.median(lat), 1),
                                                 "p95": round(lat[int(len(lat) * .95) - 1], 1), "min": round(lat[0], 1), "max": round(lat[-1], 1)}
        # continuous motion: viewer CPU while the workspace pointer moves for 5 s
        t0, _ = proc_stats(vp.pid); w0 = time.monotonic(); n = 0
        while time.monotonic() - w0 < 5:
            c.call("input.move", workspace="v", x=200 + (n * 7) % 600, y=300 + (n * 5) % 200); n += 1; time.sleep(0.016)
        t1, pss = proc_stats(vp.pid)
        out["viewer_active_60hz_motion"] = {"cpu_percent_5s": round((t1 - t0) / (time.monotonic() - w0) * 100, 1), "pss_kib": pss, "moves": n}
        print(json.dumps(out, indent=2))
    finally:
        host.close()
        K.stop_daemon(c)


if __name__ == "__main__":
    main()
