"""Two real agents at the same time, each in its own workspace, judged independently (see reliability.py).

  python3 -m tests.acceptance.parallel_pair          # Claude Code: files-1, Hermes: screenshot-describe, simultaneously
"""
import json
import os
import random
import threading
import time

from tests.acceptance import harness, reliability as R


def main():
    common, cli = harness.start_daemon("pair")
    rng = random.Random(int(time.time()))
    site = R.Site(rng)
    datadir = os.path.join(common.BASE, "data")
    os.makedirs(datadir)
    T = R.tasks(rng, site, datadir)
    out_dir = os.path.join(R.ROOT, ".scratch", "rel")
    os.environ["AW_REL_SHELL"] = "1"
    extra = {"AW_APP_DENY": "agentdock,waydroid,steam"}
    plan = [("claude-code", "files-1"), ("hermes", "screenshot-describe")]
    res = [None, None]
    before = harness.host_fingerprint()

    def one(i, agent, tid):
        prompt, verify = T[tid]
        res[i] = R.run_one(agent, tid, prompt, verify, site, common.BASE, cli, out_dir, extra)
    ths = [threading.Thread(target=one, args=(i, a, t)) for i, (a, t) in enumerate(plan)]
    t0 = time.time()
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    after = harness.host_fingerprint()
    with open(os.path.join(out_dir, "pair.jsonl"), "a") as jl:
        for r in res:
            r["parallel"] = True
            r["host_sockets_unchanged"] = before == after
            jl.write(json.dumps({k: r.get(k) for k in ("agent", "model", "task", "prompt", "success", "seconds", "exit_code", "workspace",
                                                        "agent_env_host_display_vars", "host_sockets_unchanged", "verify", "run_dir", "parallel")}, default=str) + "\n")
            print(("PASS " if r["success"] else "FAIL ") + r["agent"], r["task"], r["seconds"], "s |", (r.get("verify") or {}).get("how"))
    print("both ran for", round(time.time() - t0, 1), "s; workspaces", [r["workspace"] for r in res])
    site.close()
    common.stop_daemon(cli)


if __name__ == "__main__":
    main()
