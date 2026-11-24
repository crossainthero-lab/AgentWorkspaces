#!/usr/bin/env python3
"""Measurements for docs/measurements.md. Prints a JSON report; every number states its method.

Run on the machine whose numbers you want (the report records hostname-independent hardware info only):
    python3 scripts/measure.py > measure.json
Needs labwc; GPU numbers need nvidia-smi.
"""
import json
import os
import statistics
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests", "integration"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import common as K  # noqa: E402

PROBE = os.path.join(ROOT, "tests", "apps", "gtk_probe.py")


def cgroup_stats(c, ws_id):
    r = subprocess.run(["systemctl", "--user", "show", f"aw-{ws_id}.slice", "-p", "MemoryCurrent,CPUUsageNSec,TasksCurrent"],
                       capture_output=True, text=True).stdout
    return {k: int(v) for k, _, v in (l.partition("=") for l in r.splitlines()) if v.isdigit()}


def pss_of_slice(ws_id):
    r = subprocess.run(["systemctl", "--user", "show", f"aw-{ws_id}.slice", "-p", "ControlGroup"], capture_output=True, text=True).stdout
    cg = "/sys/fs/cgroup" + r.strip().split("=", 1)[1]
    total = rss = 0
    procs = {}
    for root, dirs, files in os.walk(cg):
        if "cgroup.procs" in files:
            for p in open(os.path.join(root, "cgroup.procs")).read().split():
                try:
                    comm = open(f"/proc/{p}/comm").read().strip()
                    sm = open(f"/proc/{p}/smaps_rollup").read()
                except OSError:
                    continue
                pss = int([l for l in sm.splitlines() if l.startswith("Pss:")][0].split()[1])
                rs = int([l for l in sm.splitlines() if l.startswith("Rss:")][0].split()[1])
                total += pss
                rss += rs
                procs[f"{comm}:{p}"] = pss
    return {"pss_kib": total, "rss_kib_sum": rss, "per_process_pss_kib": procs}


def cpu_over(c, ws_id, seconds):
    a = cgroup_stats(c, ws_id)["CPUUsageNSec"]
    t0 = time.monotonic()
    time.sleep(seconds)
    b = cgroup_stats(c, ws_id)["CPUUsageNSec"]
    return (b - a) / 1e9 / (time.monotonic() - t0) * 100.0


def pcts(xs):
    xs = sorted(xs)
    return {"n": len(xs), "median_ms": round(statistics.median(xs), 2), "p95_ms": round(xs[int(len(xs) * .95) - 1], 2),
            "min_ms": round(xs[0], 2), "max_ms": round(xs[-1], 2)}


def main():
    c = K.start_daemon("meas")
    out = {"method": {}, "results": {}}
    try:
        for gpu in ("auto", "off"):
            name = f"m-{gpu}"
            t0 = time.perf_counter()
            w = c.call("workspace.create", name=name, width=1280, height=720, gpu=gpu)
            create_ms = (time.perf_counter() - t0) * 1000
            time.sleep(1.5)
            idle = pss_of_slice(w["id"])
            idle_cpu = cpu_over(c, w["id"], 8)
            res = {"create_ms": round(create_ms), "idle_compositor_only": {"pss_kib": idle["pss_kib"], "cgroup_memory_bytes": cgroup_stats(c, w["id"]).get("MemoryCurrent"),
                                                                            "cpu_percent_8s": round(idle_cpu, 2)}}
            log = os.path.join(K.BASE, f"{name}.log")
            open(log, "w").close()
            c.call("workspace.launch", workspace=name, argv=["/usr/bin/python3", PROBE, "M", log, "#2b6cb0"])
            K.wait_for(lambda: c.call("window.list", workspace=name)["windows"], 15)
            time.sleep(2)
            act = pss_of_slice(w["id"])
            res["with_gtk4_app"] = {"pss_kib": act["pss_kib"], "cgroup_memory_bytes": cgroup_stats(c, w["id"]).get("MemoryCurrent"),
                                    "cpu_percent_idle_8s": round(cpu_over(c, w["id"], 8), 2), "per_process_pss_kib": act["per_process_pss_kib"]}
            # screenshot latency: full daemon path (client call -> capture -> png encode -> base64 -> client)
            lat = []
            for _ in range(60):
                t = time.perf_counter()
                c.call("screen.screenshot", workspace=name, inline=True)
                lat.append((time.perf_counter() - t) * 1000)
            res["screenshot_roundtrip_1280x720"] = pcts(lat[5:])
            lat = []
            for _ in range(60):
                r = c.call("screen.screenshot", workspace=name, inline=False)
                lat.append(r["capture_ms"])
            res["screenshot_daemon_side_capture_encode_ms"] = pcts(lat[5:])
            # input -> pixels: move the pointer over the probe; wait until the probe's pointer label changes on screen
            from aw.client import Client
            import base64
            lat = []
            for i in range(30):
                base = c.call("screen.screenshot", workspace=name, region=[400, 100, 400, 60], inline=True)["base64"]
                t = time.perf_counter()
                c.call("input.move", workspace=name, x=200 + (i % 2) * 120 + i, y=250)
                for _ in range(400):
                    cur = c.call("screen.screenshot", workspace=name, region=[400, 100, 400, 60], inline=True)["base64"]
                    if cur != base:
                        break
                lat.append((time.perf_counter() - t) * 1000)
            res["input_to_pixels_change"] = pcts(lat[3:])
            res["agent_gpu_mode"] = gpu
            out["results"][name] = res
            c.call("workspace.destroy", workspace=name)
        # two concurrent workspaces
        ws = [c.call("workspace.create", name=f"c{i}", width=1280, height=720)["id"] for i in range(2)]
        for i in range(2):
            log = os.path.join(K.BASE, f"c{i}.log"); open(log, "w").close()
            c.call("workspace.launch", workspace=f"c{i}", argv=["/usr/bin/python3", PROBE, f"C{i}", log, "#2b6cb0"])
        time.sleep(4)
        out["results"]["two_workspaces_with_apps"] = {"total_pss_kib": sum(pss_of_slice(w)["pss_kib"] for w in ws)}
        for i in range(2):
            c.call("workspace.destroy", workspace=f"c{i}")
        if subprocess.run(["which", "nvidia-smi"], capture_output=True).returncode == 0:
            out["gpu"] = subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
                                        capture_output=True, text=True).stdout.strip()
        out["host"] = {"cpus": os.cpu_count(), "kernel": os.uname().release}
        print(json.dumps(out, indent=2))
    finally:
        K.stop_daemon(c)


if __name__ == "__main__":
    main()
