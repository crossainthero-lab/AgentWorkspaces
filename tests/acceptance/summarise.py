"""Tabulate result.json files from run_scenarios (newest run per agent/task unless --all)."""
import glob
import json
import os
import sys

from tests.acceptance import transcript

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(all_runs=False):
    rows = []
    for f in sorted(glob.glob(os.path.join(ROOT, ".scratch", "runs", "*", "result.json"))):
        r = json.load(open(f))
        d = r["run_dir"]
        lines = open(os.path.join(d, "agent.stdout"), errors="replace").read().splitlines()
        kind = "claude" if r["agent"] == "claude-code" else "codex"
        _, usage = (transcript.claude if kind == "claude" else transcript.codex)(lines)
        ws = r.get("workspace_state") or {}
        u = ws.get("usage") or {}
        rows.append({"dir": os.path.basename(d), "agent": r["agent"], "task": r.get("task", "?"), "s": r["seconds"],
                     "exit": r["exit_code"], "par": bool(r.get("parallel_with")), "agent_pss_mib": r.get("agent_peak_pss_mib_incl_mcp_server"),
                     "agent_cpu_s": r.get("agent_cpu_seconds_children"), "ws_mem_mib": round(u.get("memory_bytes", 0) / 2 ** 20),
                     "ws_cpu_s": round(u.get("cpu_ns", 0) / 1e9, 1), "viewers": ws.get("viewers"), "windows": ws.get("windows"),
                     "files": r.get("home_files"), "hostvars": r.get("agent_env_host_display_vars"), "usage": usage})
    if not all_runs:
        latest = {}
        for x in rows:
            latest[(x["agent"], x["task"], x["par"])] = x
        rows = list(latest.values())
    for x in rows:
        print(json.dumps(x, default=str))


if __name__ == "__main__":
    main("--all" in sys.argv)
