"""CLI for browsers and browser profiles (docs/browsers.md). Registered from aw/cli.py."""
from __future__ import annotations

import json
import os
import sys
from typing import Any, List

from . import browsers as B


def add_parsers(sub) -> None:
    br = sub.add_parser("browsers", help="installed browsers and their profile names/ids, and this workspace's "
                                         "own profiles")
    br.add_argument("workspace", metavar="NAME")
    lb = sub.add_parser("launch-browser", help="start a browser inside a workspace (private profile by default)")
    lb.add_argument("workspace", metavar="NAME")
    lb.add_argument("--browser", default=None, help="id from `browsers` (google-chrome, firefox, ...); "
                    "default: the first launchable Chromium-based browser")
    lb.add_argument("--profile", default="throwaway",
                    help="throwaway (default, deleted with the workspace) | agent:NAME (kept) | "
                         "authorized:PROFILE_ID (a human-authorised copy, see `profile authorize`)")
    lb.add_argument("--url")
    lb.add_argument("--backend", choices=["wayland", "x11"], default="wayland")
    lb.add_argument("args", nargs="*", help="extra browser arguments (automation/profile flags are refused)")
    pr = sub.add_parser("profile", help="browser profile access: authorize/revoke are HUMAN ONLY (interactive "
                                        "terminal); an agent can only request")
    pr.add_argument("op", choices=["list", "request", "authorize", "revoke"])
    pr.add_argument("browser", nargs="?")
    pr.add_argument("profile", nargs="?")
    pr.add_argument("--workspace", "-w", required=True)
    pr.add_argument("--reason", default="")
    pr.add_argument("--ttl-hours", type=float, default=8.0, help="how long the copy may be used (max 8)")
    pr.add_argument("--refresh", action="store_true", help="replace an existing authorised copy")


def run(args, c, show, out_json: bool) -> int:
    cmd = args.cmd
    if cmd == "browsers":
        res = c.call("browser.list", workspace=args.workspace)
        if out_json:
            show(res)
            return 0
        for b in res["browsers"]:
            flag = "ok " if b["launchable"] else "NO "
            print(f"[{flag}] {b['id']:<24} {b['label']:<18} {b['variant']:<9} "
                  f"{(b['reason'] or '')[:60]}")
            for p in b["profiles"]:
                print(f"        profile {p['id']!r}  name {p['name']!r}")
        for a in res["agent_profiles"]:
            print(f"agent profile: {a['browser']}/{a['name']}")
        for a in res["authorized"]:
            print(f"authorized copy: {a['browser']}:{a['profile']} ({a['status']})")
        return 0
    if cmd == "launch-browser":
        prof: Any = args.profile
        res = c.call("browser.launch", workspace=args.workspace, browser=args.browser, profile=prof,
                     url=args.url, args=list(args.args), backend=args.backend)
        show(res, f"launched {res['browser']} (app {res['app']}, pid {res['pid']}) with a "
                  f"{res['profile']['mode']} profile")
        return 0
    return _profile(args, c, show, out_json)


def _profile(args, c, show, out_json: bool) -> int:
    ws, op = args.workspace, args.op
    if op == "list":
        show(c.call("browser.profile.list", workspace=ws))
        return 0
    if not args.browser or not args.profile:
        print("usage: agent-workspaces profile OP BROWSER PROFILE --workspace NAME", file=sys.stderr)
        return 2
    if op == "request":
        res = c.call("browser.profile.request", workspace=ws, browser=args.browser, profile=args.profile,
                     reason=args.reason)
        show(res, f"requested ({res['status']}); {res.get('how', '')}")
        return 0
    # authorize / revoke: the person, at a terminal
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(f"refusing: `profile {op}` is human only and needs an interactive terminal "
              "(an agent can only run `profile request`)", file=sys.stderr)
        return 2
    if op == "revoke":
        if input(f"Delete (overwrite and remove) the copy of {args.browser}:{args.profile} held by workspace "
                 f"{ws!r} and revoke its permission? [y/N] ").strip().lower() != "y":
            print("not changed")
            return 1
        res = c.call("browser.profile.revoke", workspace=ws, browser=args.browser, profile=args.profile)
        show(res, f"revoked; {res['files_overwritten']} files overwritten and removed")
        return 0
    # authorize: show exactly what happens, check the host profile is closed, ask for the workspace name
    home = os.path.expanduser("~")
    try:
        b = B.find_browser(B.discover(home), args.browser)
    except B.BrowserError as e:
        print(f"error: {e.message}", file=sys.stderr)
        return 4
    prof = next((p for p in b["profiles"] if args.profile in (p["id"], p["name"])), None)
    if not prof:
        print(f"error: {b['label']} has no profile {args.profile!r}; known: {[p['id'] for p in b['profiles']]}",
              file=sys.stderr)
        return 4
    state, detail = B.host_profile_state(b["family"], prof["root"], prof["id"])
    plan = B.plan_clone(b, prof)
    print(f"\nAuthorise workspace {ws!r} to use a COPY of your {b['label']} profile {prof['name']!r}.\n")
    print("  Will copy (read only from the original, which is never changed):")
    for item in plan["copies"] or ["(nothing found)"]:
        print(f"    - {item}")
    print("  Will NOT copy: " + "; ".join(plan["never_copied"]))
    print("  This gives the agent your signed-in sessions (cookies) for that profile. The copy is private to "
          "AgentWorkspaces (mode 0700),\n  is deleted (overwritten, then removed) when you run `profile revoke`, "
          "when the workspace is destroyed or when the permission expires.")
    print("  Not guaranteed: overwriting cannot erase blocks on SSD/copy-on-write storage; sites may notice a "
          "second device.\n")
    if state != "closed":
        print(f"Your own {b['label']} appears to have this profile open ({detail}).\n"
              "Close it completely first; nothing was copied and no lock file was touched.", file=sys.stderr)
        return 3
    typed = input(f"Type the workspace name ({ws}) to confirm, anything else cancels: ").strip()
    if typed != ws:
        print("cancelled")
        return 1
    res = c.call("browser.profile.authorize", workspace=ws, browser=b["id"], profile=prof["id"],
                 ttl_seconds=min(max(args.ttl_hours, 0.01), 8.0) * 3600, refresh=args.refresh)
    show(res, f"authorised: copied {', '.join(res['copied']) or 'nothing'} ({res['bytes']} bytes); valid until "
              f"{res['grant']['expires_at']:.0f} (epoch). Launch with: agent-workspaces launch-browser {ws} "
              f"--browser {b['id']} --profile authorized:{prof['id']}")
    return 0
