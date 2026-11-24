"""Markdown tables from reliability result files (.scratch/rel/*.jsonl).

  python3 -I tests/acceptance/rel_table.py LABEL=FILE[:FROM:TO] ...      (FROM:TO = line slice, 1-based inclusive)
"""
import json
import sys


def load(spec):
    label, _, rest = spec.partition("=")
    parts = rest.split(":")
    rows = [json.loads(l) for l in open(parts[0])]
    if len(parts) == 3:
        rows = rows[int(parts[1]) - 1:int(parts[2])]
    return label, rows


def main(specs):
    tot = ok = 0
    for spec in specs:
        label, rows = load(spec)
        n, k = len(rows), sum(1 for r in rows if r["success"])
        tot, ok = tot + n, ok + k
        secs = [r["seconds"] for r in rows if r.get("seconds")]
        print(f"\n#### {label}: {k}/{n} = {100 * k / max(n, 1):.0f}% (median {sorted(secs)[len(secs) // 2]:.0f} s)\n")
        print("| task | result | how it was verified (independently of the agent) |\n|---|---|---|")
        for r in rows:
            v = r.get("verify") or {}
            how = (v.get("how") or "").replace("|", "/").replace("/run/user/1000/awt-rel-", "<run>/")
            note = "" if r["success"] else f" (exit {r['exit_code']})"
            print(f"| {r['task']} | {'pass' if r['success'] else 'FAIL' + note} | {how[:170]} |")
    print(f"\nTOTAL {ok}/{tot} = {100 * ok / max(tot, 1):.1f}%")


if __name__ == "__main__":
    main(sys.argv[1:])
