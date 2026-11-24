#!/usr/bin/env python3
"""Measure the overhead of the optional bubblewrap sandbox (docs/isolation-model.md).

1. exec cost: `true` plain vs wrapped, 200 runs each (no compositor involved)
2. GTK4 app: launch -> window ready, plain workspace vs sandboxed workspace, N runs each (one compositor at a time)
3. memory: cgroup memory of the app's systemd scope and RSS of the bwrap processes, plain vs sandboxed

Runs against a throw-away daemon under /run/user/UID/awt-*; nothing is shown on your desktop.
"""
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, os.path.join(ROOT, "tests", "integration"))
import common as K  # noqa: E402
from aw import sandbox  # noqa: E402

N_EXEC = 200
N_APP = int(os.environ.get("N_APP", "5"))


def exec_cost():
    home = tempfile.mkdtemp(prefix="aw-m-", dir="/tmp")
    rt = os.path.join(home, "rt")
    os.makedirs(rt)
    pre = ["bwrap"] + sandbox.build_argv(home, rt, home, [], None, "/", True)[1:] + ["--", "/usr/bin/true"]
    res = {}
    for name, cmd in (("plain", ["/usr/bin/env", "-i", "/usr/bin/true"]),
                      ("sandboxed", ["/usr/bin/env", "-i", "PATH=/usr/bin"] + pre)):
        ts = []
        for _ in range(N_EXEC):
            t = time.perf_counter()
            subprocess.run(cmd, check=True)
            ts.append((time.perf_counter() - t) * 1000)
        shutil.rmtree(home, ignore_errors=True) if name == "sandboxed" else None
        res[name] = {"median_ms": round(statistics.median(ts), 2), "p95_ms": round(sorted(ts)[int(.95 * len(ts))], 2)}
    return res


def tree_rss_kib(pid):
    """RSS of pid and all descendants (exact /proc walk, no pattern matching)."""
    total, stack, seen = 0, [pid], set()
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        try:
            for line in open(f"/proc/{p}/status"):
                if line.startswith("VmRSS:"):
                    total += int(line.split()[1])
            for t in os.listdir(f"/proc/{p}/task"):
                stack += [int(x) for x in open(f"/proc/{p}/task/{t}/children").read().split()]
        except OSError:
            pass
    return total, len(seen)


def app_runs(c, name, sandboxed, logdir):
    kw = {"sandbox": True, "sandbox_mounts": [os.path.join(ROOT, "tests") + ":ro", logdir + ":rw"]} if sandboxed else {}
    c.call("workspace.create", name=name, width=900, height=600, **kw)
    lat, mem, rss, nproc = [], [], [], []
    try:
        for i in range(N_APP):
            log = os.path.join(logdir, f"{name}-{i}.log")
            open(log, "w").close()
            t = time.perf_counter()
            r = c.call("workspace.launch", workspace=name, argv=["/usr/bin/python3", os.path.join(
                ROOT, "tests", "apps", "gtk_probe.py"), name, log, "#2b6cb0"])
            ok = K.wait_for(lambda: any(e.get("ev") == "ready" for e in K.log_events(log)), 20, 0.02)
            assert ok, "app not ready"
            lat.append((time.perf_counter() - t) * 1000)
            time.sleep(1.0)
            scope = f"aw-{c.call('workspace.get', workspace=name)['id']}-app{r['app']}.scope"
            out = subprocess.run(["systemctl", "--user", "show", scope, "-p", "MemoryCurrent"], capture_output=True,
                                 text=True).stdout
            v = out.split("=")[1].strip() if "=" in out else ""
            if v.isdigit():
                mem.append(int(v) / 1048576)
            kib, n = tree_rss_kib(r["pid"])
            rss.append(kib / 1024)
            nproc.append(n)
            c.call("app.kill", workspace=name, app=r["app"])
            time.sleep(0.8)
    finally:
        c.call("workspace.destroy", workspace=name)
    f = lambda xs: round(statistics.median(xs), 1) if xs else None  # noqa: E731
    return {"launch_to_ready_ms_median": f(lat), "launch_to_ready_ms_all": [round(x) for x in lat],
            "app_scope_memory_mib_median": f(mem), "app_tree_rss_mib_median": f(rss), "processes": f(nproc)}


def main():
    out = {"exec": exec_cost()}
    c = K.start_daemon("msb")
    logdir = tempfile.mkdtemp(prefix="aw-m-", dir="/tmp")
    try:
        out["gtk4_app"] = {"plain": app_runs(c, "mplain", False, logdir),
                           "sandboxed": app_runs(c, "mbox", True, logdir)}
    finally:
        K.stop_daemon(c)
        shutil.rmtree(logdir, ignore_errors=True)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
