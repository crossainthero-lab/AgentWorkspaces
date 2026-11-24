"""Run real-agent acceptance scenarios.

  python3 -m tests.acceptance.run_scenarios claude-code:browser codex:files "claude-code:editor+codex:screenshot"

Each argument is one scenario: agent:task, or several agent:task joined with '+' to run them AT THE SAME TIME
in separate workspaces. Results (JSON, screenshots, raw transcripts) go to .scratch/runs/ (not committed).
"""
import json
import os
import sys
import threading
import time

from tests.acceptance import harness

ROOT = harness.ROOT


def main(specs):
    common, cli = harness.start_daemon("accs")
    out = os.path.join(ROOT, ".scratch", "runs")
    results = []
    try:
        fm = os.path.join(common.BASE, "inbox")
        os.makedirs(os.path.join(fm, "archive"), exist_ok=True)
        for n in ("notes.txt", "invoice-2024.pdf", "holiday.jpg"):
            open(os.path.join(fm, n), "w").write("x")
        for spec in specs:
            group = spec.split("+")
            before = harness.host_fingerprint()
            res_group = [None] * len(group)

            def one(i, item):
                agent, task = item.split(":")[:2]
                noinst = item.endswith(":noinstr")
                prompt = harness.PROMPTS[task].format(dir=fm)
                r = harness.run_agent(agent, prompt, common.BASE, task, os.path.join(out, f"{agent}-{task}-{int(time.time())}"),
                                      timeout=240, instructions=not noinst, extra_env={"AW_APP_DENY": "agentdock,waydroid,steam"})
                r["task"] = task
                r["parallel_with"] = [g for g in group if g != item]
                r["home_files"] = harness.home_files(r["home"]) if r.get("home") else None
                r["home_previews"] = {f: open(os.path.join(r["home"], f), errors="replace").read(160)
                                      for f in (r["home_files"] or []) if f.endswith(".txt")}
                res_group[i] = r
            ths = [threading.Thread(target=one, args=(i, it)) for i, it in enumerate(group)]
            for t in ths:
                t.start()
            for t in ths:
                t.join()
            after = harness.host_fingerprint()
            for r in res_group:
                r["host_before"], r["host_after"] = before, after
                json.dump(r, open(os.path.join(r["run_dir"], "result.json"), "w"), indent=2, default=str)
                results.append(r)
                print(json.dumps({k: r.get(k) for k in ("agent", "task", "model", "exit_code", "timed_out", "seconds",
                                                        "workspace", "run_dir", "home_files", "parallel_with")}, default=str))
    finally:
        harness.start_daemon  # noqa
        common.stop_daemon(cli)
    return results


if __name__ == "__main__":
    main(sys.argv[1:])
