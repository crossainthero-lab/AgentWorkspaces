"""Sanitised evidence of reliability runs: final workspace screenshot, short transcript, the verification result.

  python3 -m tests.acceptance.collect_release_evidence NAME=FILE[:FROM:TO] ...
Writes docs/evidence/release/<NAME>/<task>.{png,txt}.
"""
import json
import os
import shutil
import sys

from tests.acceptance import transcript, rel_table

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "docs", "evidence", "release")
HOME = os.path.expanduser("~")


def scrub(t):
    import re
    t = t.replace(HOME, "~").replace(os.path.basename(HOME), "<user>")
    t = re.sub(r"/run/user/\d+/awt-[A-Za-z0-9_\-]+", "<run>", t)
    return t


def main(specs):
    for spec in specs:
        name, rows = rel_table.load(spec)
        d = os.path.join(OUT, name)
        os.makedirs(d, exist_ok=True)
        for r in rows:
            kind = {"claude-code": "claude", "hermes": "hermes"}[r["agent"]]
            run = r["run_dir"]
            if os.path.exists(os.path.join(run, "final.png")):
                shutil.copyfile(os.path.join(run, "final.png"), os.path.join(d, f"{r['task']}.png"))
            lines = open(os.path.join(run, "agent.stdout"), errors="replace").read().splitlines()
            out, usage = getattr(transcript, kind)(lines)
            v = r.get("verify") or {}
            head = (f"prompt: {scrub(r['prompt'])}\nmodel: {r.get('model')}\nresult: {'PASS' if r['success'] else 'FAIL'}\n"
                    f"verified by: {scrub(str(v.get('how')))}\nseconds: {r['seconds']}  exit: {r['exit_code']}\n"
                    f"host display variables seen in the agent process group: {r.get('agent_env_host_display_vars')}\n"
                    f"host wayland sockets unchanged: {r.get('host_sockets_unchanged')}\n\n")
            open(os.path.join(d, f"{r['task']}.txt"), "w").write(head + scrub("\n".join(out)) + "\n\nUSAGE " + json.dumps(usage) + "\n")
        print("wrote", d, len(rows))


if __name__ == "__main__":
    main(sys.argv[1:])
