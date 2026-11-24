"""REAL acceptance harness: runs a real agent CLI with a real model against a real daemon and a real
headless workspace compositor. No mocks. Everything is bounded and torn down by exact PID.

Safety: the agent gets an environment built from scratch (aw.agentrun.clean_env) with the host display
variables removed and BROWSER=/bin/true. The daemon is started without any display variable (as the
integration tests do), under its own AW_BASE_DIR, and every GUI program goes through the daemon's launcher.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from aw import agentrun  # noqa: E402
from aw.client import Client  # noqa: E402

PROMPTS = {
    "browser": "Open my browser and visit agdc.dev, then tell me what the page's main heading says.",
    "editor": "Open a text editor, write a short paragraph about autumn rain and save it as autumn.txt.",
    "files": "Open the file manager and look inside the folder {dir}. Tell me which files are in it.",
    "screenshot": "Open the calculator, take a screenshot of it and tell me what you see on its screen.",
    "twoapps": "Open the calculator and a text editor. Work out 17 times 23 in the calculator, then type the "
               "result into the text editor.",
}


def start_daemon(tag: str):
    from tests.integration import common
    cli = common.start_daemon(tag)
    return common, cli


def host_fingerprint() -> Dict[str, Any]:
    """Cheap, unprivileged facts about the host desktop that must not change because of an agent run."""
    out: Dict[str, Any] = {}
    try:
        rt = f"/run/user/{os.getuid()}"
        out["host_sockets"] = sorted(n for n in os.listdir(rt) if n.startswith("wayland-") and not n.endswith(".lock"))
    except OSError:
        pass
    return out


def run_agent(agent: str, prompt: str, base: str, project: str, run_dir: str, model: Optional[str] = None,
              timeout: float = 240.0, extra_env: Optional[Dict[str, str]] = None, shots_every: float = 6.0,
              ws_name: Optional[str] = None) -> Dict[str, Any]:
    os.makedirs(run_dir, exist_ok=True)
    plan = agentrun.plan(agent, prompt, model=model, project=project, base_dir=base, extra_env=extra_env)
    assert "DISPLAY" not in plan.env and "WAYLAND_DISPLAY" not in plan.env
    out_f = open(os.path.join(run_dir, "agent.stdout"), "wb")
    err_f = open(os.path.join(run_dir, "agent.stderr"), "wb")
    t0 = time.time()
    proc = subprocess.Popen(plan.argv, env=plan.env, cwd=plan.cwd, stdin=subprocess.DEVNULL, stdout=out_f,
                            stderr=err_f, start_new_session=True)
    stop = threading.Event()
    shots: List[Dict[str, Any]] = []
    cl = Client(autostart=False)
    name = ws_name or f"{agentrun.ig.AGENTS[agent]['kind']}-{project}"

    def sampler():
        n = 0
        while not stop.wait(shots_every):
            try:
                r = cl.call("screen.screenshot", workspace=name, agent_kind="human", path=os.path.join(
                    run_dir, f"shot-{n:02d}.png"), inline=False)
                shots.append({"t": round(time.time() - t0, 1), "file": os.path.basename(r["path"])})
                n += 1
            except Exception:
                pass
    th = threading.Thread(target=sampler, daemon=True)
    th.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(proc.pid, signal.SIGTERM)        # exact process group of the agent we started
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(5)
    stop.set()
    th.join(timeout=10)
    dur = time.time() - t0
    res: Dict[str, Any] = {"agent": agent, "model": model or agentrun.CHEAP_MODEL.get(agent), "prompt": prompt,
                           "exit_code": proc.returncode, "timed_out": timed_out, "seconds": round(dur, 1),
                           "workspace": name, "run_dir": run_dir, "shots": shots}
    try:
        w = cl.call("workspace.get", workspace=name)
        res["workspace_state"] = {"state": w["state"], "agent": w["agent"]["kind"], "viewers": w["viewers"],
                                  "windows": [x.get("title") for x in w.get("windows", [])],
                                  "display": w["display"], "usage": w.get("usage")}
        cl.call("screen.screenshot", workspace=name, agent_kind="human", path=os.path.join(run_dir, "final.png"),
                inline=False)
        res["home"] = os.path.join(base, w["id"], "home")
    except Exception as e:
        res["workspace_error"] = str(e)
    plan.close()
    out_f.close()
    err_f.close()
    return res


def home_files(home: str) -> List[str]:
    out = []
    for dp, dn, fn in os.walk(home):
        dn[:] = [d for d in dn if not d.startswith(".")]
        for f in fn:
            if not f.startswith("."):
                out.append(os.path.relpath(os.path.join(dp, f), home))
    return sorted(out)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("agent")
    ap.add_argument("task", choices=sorted(PROMPTS))
    ap.add_argument("--model")
    ap.add_argument("--timeout", type=float, default=240)
    ap.add_argument("--out", default=os.path.join(ROOT, ".scratch", "runs"))
    a = ap.parse_args()
    common, cli = start_daemon("acc")
    try:
        fm = os.path.join(common.BASE, "inbox")
        os.makedirs(fm, exist_ok=True)
        for n, body in (("notes.txt", "hello"), ("invoice-2024.pdf", "x"), ("holiday.jpg", "x")):
            open(os.path.join(fm, n), "w").write(body)
        os.makedirs(os.path.join(fm, "archive"), exist_ok=True)
        prompt = PROMPTS[a.task].format(dir=fm)
        before = host_fingerprint()
        res = run_agent(a.agent, prompt, common.BASE, f"{a.task}", os.path.join(a.out, f"{a.agent}-{a.task}-{int(time.time())}"),
                        model=a.model, timeout=a.timeout,
                        extra_env={"AW_APP_DENY": "agentdock,waydroid,steam"})
        res["home_files"] = home_files(res["home"]) if res.get("home") else None
        res["home_previews"] = {f: open(os.path.join(res["home"], f), errors="replace").read(160)
                                for f in (res["home_files"] or []) if f.endswith(".txt")}
        res["host_before"], res["host_after"] = before, host_fingerprint()
        print(json.dumps(res, indent=2, default=str))
        json.dump(res, open(os.path.join(res["run_dir"], "result.json"), "w"), indent=2, default=str)
    finally:
        common.stop_daemon(cli)
