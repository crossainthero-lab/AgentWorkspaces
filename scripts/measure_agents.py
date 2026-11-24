#!/usr/bin/env python3
"""Measure what an agent's computer use costs, through the MCP server (no LLM involved, so numbers are
the integration's own, not a model's).

  python3 scripts/measure_agents.py [--shots 30]

Starts a private daemon (own AW_BASE_DIR), runs the MCP server over stdio exactly as an agent would, and reports:
  * first graphical call (workspace create + compositor start) and warm attach latency
  * launch_app (editor, browser) until the screenshot is returned
  * screenshot round trip through MCP (median / p95), PNG bytes, daemon capture time
  * memory/CPU of the workspace cgroup, PSS of the MCP server and daemon, GPU processes of the compositor
Everything runs inside throw-away workspaces; no display variable is passed to any process.
"""
import argparse
import base64
import json
import os
import statistics
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
from tests.integration import common  # noqa: E402


def pss_mib(pid):
    try:
        for line in open(f"/proc/{pid}/smaps_rollup"):
            if line.startswith("Pss:"):
                return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", type=int, default=30)
    a = ap.parse_args()
    cli = common.start_daemon("meas")
    out = {}
    try:
        env = {"PATH": os.environ["PATH"], "HOME": os.path.expanduser("~"), "AW_BASE_DIR": common.BASE,
               "AW_AGENT_KIND": "claude-code", "AW_APP_DENY": "agentdock", "LANG": "C.UTF-8"}
        p = subprocess.Popen([sys.executable, os.path.join(ROOT, "bin", "agent-workspaces"), "mcp"], env=env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                             cwd=os.path.join(common.BASE))
        n = [0]

        def rpc(method, params=None):
            n[0] += 1
            t = time.perf_counter()
            p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": n[0], "method": method, "params": params or {}}) + "\n")
            p.stdin.flush()
            r = json.loads(p.stdout.readline())
            return r, (time.perf_counter() - t) * 1000

        def tool(name, args=None):
            r, ms = rpc("tools/call", {"name": name, "arguments": args or {}})
            return r["result"], ms

        _, ms = rpc("initialize", {"protocolVersion": "2025-06-18"})
        out["mcp_initialize_ms"] = round(ms)
        res, ms = tool("ensure_workspace")
        out["first_call_create_and_start_workspace_ms"] = round(ms)
        out["first_call_text"] = res["content"][0]["text"]
        _, ms = tool("ensure_workspace")
        out["warm_attach_ms"] = round(ms)
        res, ms = tool("launch_app", {"app": "text editor"})
        out["launch_text_editor_to_screenshot_ms"] = round(ms)
        res, ms = tool("launch_app", {"app": "browser", "args": ["about:blank"]})
        out["launch_browser_to_screenshot_ms"] = round(ms)
        time.sleep(2)
        lat, cap, size = [], [], []
        for _ in range(a.shots):
            res, ms = tool("screenshot")
            lat.append(ms)
            img = [c for c in res["content"] if c["type"] == "image"][0]
            size.append(len(base64.b64decode(img["data"])))
            txt = [c for c in res["content"] if c["type"] == "text"][0]["text"]
            cap.append(float(txt.split("captured in ")[1].split(" ms")[0]))
        lat.sort()
        out["screenshot_roundtrip_ms"] = {"median": round(statistics.median(lat), 1), "p95": round(lat[int(len(lat) * .95) - 1], 1),
                                          "min": round(lat[0], 1), "n": len(lat)}
        out["screenshot_daemon_capture_ms_median"] = round(statistics.median(cap), 1)
        out["screenshot_png_bytes_median"] = int(statistics.median(size))
        t = time.perf_counter()
        tool("mouse_click", {"x": 100, "y": 100})
        out["mouse_click_roundtrip_ms"] = round((time.perf_counter() - t) * 1000, 1)
        ws = cli.call("workspace.list")["workspaces"][0]
        u = cli.call("workspace.usage", workspace=ws["id"])
        out["workspace_with_editor_and_browser"] = {"memory_MiB": round(u["memory_bytes"] / 2 ** 20), "cpu_seconds_total": round(u["cpu_ns"] / 1e9, 2),
                                                    "tasks": u["tasks"], "gpu_setting": ws["gpu"]}
        out["mcp_server_pss_MiB"] = pss_mib(p.pid)
        dpid = common.DAEMON.pid
        out["daemon_pss_MiB"] = pss_mib(dpid)
        # CPU over 5 s of idle (nothing changing on screen)
        u1 = cli.call("workspace.usage", workspace=ws["id"])["cpu_ns"]
        time.sleep(5)
        u2 = cli.call("workspace.usage", workspace=ws["id"])["cpu_ns"]
        out["workspace_idle_cpu_percent_of_one_core"] = round((u2 - u1) / 5e9 * 100, 1)
        try:
            smi = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader"],
                                 capture_output=True, text=True, timeout=10).stdout
            pm = subprocess.run(["nvidia-smi", "pmon", "-c", "1", "-s", "um"], capture_output=True, text=True, timeout=15).stdout
            comp = [l.split() for l in pm.splitlines() if "labwc" in l or "chrome" in l]
            out["gpu_pmon_workspace_processes"] = [" ".join(c) for c in comp][:6]
        except Exception as e:  # noqa
            out["gpu"] = f"nvidia-smi unavailable: {e}"
        p.stdin.close()
        p.wait(5)
    finally:
        common.stop_daemon(cli)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
