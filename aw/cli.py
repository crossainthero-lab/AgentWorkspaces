"""agent-workspaces: command line interface."""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
from typing import List, Optional

from . import __version__
from .client import ApiError, Client


def _p(obj):
    print(json.dumps(obj, indent=2, sort_keys=True))


def _agent(args):
    """Who is calling. CLI calls are 'agent' by default so they honour manual takeover;
    --human is for the person at the keyboard."""
    return "human" if getattr(args, "human", False) else (getattr(args, "as_agent", None) or None)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="agent-workspaces",
                                 description="Separate graphical workspaces for AI agents.")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def ws_arg(p, name="workspace"):
        p.add_argument(name, metavar="NAME", help="workspace name or id")

    def caller(p):
        p.add_argument("--human", action="store_true",
                       help="act as the human user (bypasses manual-takeover pause)")
        p.add_argument("--as-agent", metavar="KIND", help="identify as an agent kind (claude-code, codex, ...)")

    sub.add_parser("daemon", help="run the workspace daemon in the foreground")
    sub.add_parser("status", help="daemon status + workspace summary").add_argument("workspace", nargs="?")
    sub.add_parser("list", help="list workspaces").add_argument("--all", action="store_true")

    c = sub.add_parser("create", help="create and start a workspace")
    c.add_argument("--name", required=True)
    c.add_argument("--agent", default="none", help="claude-code|codex|antigravity|hermes|other|none")
    c.add_argument("--agent-label", default="")
    c.add_argument("--size", default="1280x720", help="WIDTHxHEIGHT")
    c.add_argument("--scale", type=float, default=1.0)
    c.add_argument("--gpu", choices=["auto", "off"], default="auto")
    c.add_argument("--clipboard", choices=["isolated", "to-workspace", "from-workspace", "both"],
                   default="isolated")
    c.add_argument("--window-policy", choices=["maximized", "floating"], default="maximized")
    c.add_argument("--home", choices=["private", "shared"], default="private")
    c.add_argument("--memory-max", help="systemd MemoryMax for the compositor, e.g. 2G")
    c.add_argument("--no-xwayland", action="store_true",
                   help="do not give apps a DISPLAY: X11 apps will not run, and GPU drivers cannot wake XWayland (~90 MB)")
    c.add_argument("--no-start", action="store_true")

    r = sub.add_parser("rename"); ws_arg(r); r.add_argument("new_name")
    a = sub.add_parser("assign", help="assign an agent to a workspace"); ws_arg(a)
    a.add_argument("agent", help="claude-code|codex|antigravity|hermes|other|none")
    a.add_argument("--label", default="")
    cf = sub.add_parser("configure"); ws_arg(cf)
    cf.add_argument("--size"); cf.add_argument("--scale", type=float)
    cf.add_argument("--gpu", choices=["auto", "off"])
    cf.add_argument("--clipboard", choices=["isolated", "to-workspace", "from-workspace", "both"])
    cf.add_argument("--window-policy", choices=["maximized", "floating"])
    for n in ("start", "stop", "destroy", "resume", "usage"):
        ws_arg(sub.add_parser(n))
    pz = sub.add_parser("pause", help="manual takeover: reject agent input"); ws_arg(pz)
    pz.add_argument("--reason", default="")

    ln = sub.add_parser("launch", help="launch a program inside a workspace: launch NAME -- cmd ...")
    ws_arg(ln); ln.add_argument("--cwd"); ln.add_argument("command", nargs=argparse.REMAINDER)
    en = sub.add_parser("enter", help="open the viewer window for a workspace"); ws_arg(en)
    en.add_argument("--takeover", action="store_true", help="also pause agent input")
    en.add_argument("--wait", action="store_true")

    sh = sub.add_parser("screenshot", help="save a PNG of the workspace screen"); ws_arg(sh)
    sh.add_argument("-o", "--output", default="-", help="file path (default: workspace-NAME.png)")
    sh.add_argument("--region", help="x,y,w,h"); sh.add_argument("--cursor", action="store_true")
    sh.add_argument("--max-width", type=int); caller(sh)

    for n in ("windows", "display-info"):
        p = sub.add_parser(n); ws_arg(p); caller(p)
    wa = sub.add_parser("window-action"); ws_arg(wa); wa.add_argument("window", type=int)
    wa.add_argument("action", choices=["focus", "close", "maximize", "unmaximize", "minimize",
                                       "unminimize", "fullscreen", "unfullscreen", "restore"]); caller(wa)
    ck = sub.add_parser("click"); ws_arg(ck); ck.add_argument("x", type=float, nargs="?")
    ck.add_argument("y", type=float, nargs="?"); ck.add_argument("--button", default="left")
    ck.add_argument("--count", type=int, default=1); caller(ck)
    mv = sub.add_parser("move"); ws_arg(mv); mv.add_argument("x", type=float); mv.add_argument("y", type=float); caller(mv)
    dr = sub.add_parser("drag"); ws_arg(dr)
    for n in ("x1", "y1", "x2", "y2"):
        dr.add_argument(n, type=float)
    caller(dr)
    ty = sub.add_parser("type"); ws_arg(ty); ty.add_argument("text"); caller(ty)
    ky = sub.add_parser("key"); ws_arg(ky); ky.add_argument("combo"); caller(ky)
    sc = sub.add_parser("scroll"); ws_arg(sc); sc.add_argument("dy", type=float)
    sc.add_argument("--dx", type=float, default=0); sc.add_argument("--at", help="x,y"); caller(sc)
    cb = sub.add_parser("clipboard"); ws_arg(cb); cb.add_argument("op", choices=["get", "set"])
    cb.add_argument("text", nargs="?"); caller(cb)
    ev = sub.add_parser("events", help="stream events as JSON lines"); ev.add_argument("workspace", nargs="?")
    ap_ = sub.add_parser("approvals"); ap_.add_argument("op", choices=["list", "approve", "deny", "request"])
    ap_.add_argument("arg", nargs="?"); ap_.add_argument("--workspace"); ap_.add_argument("--title", default="")
    sub.add_parser("mcp", help="run the MCP server on stdio").add_argument("--workspace")
    ig = sub.add_parser("integrate", help="opt-in agent integration (reversible)")
    ig.add_argument("agent", choices=["claude-code", "codex", "antigravity", "hermes"])
    ig.add_argument("action", choices=["status", "install", "uninstall", "show"])
    ig.add_argument("--workspace", help="default workspace for this agent")
    ig.add_argument("--config", help="override the agent config path (testing)")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "daemon":
        from .daemon import serve
        return serve()
    if args.cmd == "mcp":
        from .mcp import run
        return run(args.workspace)
    if args.cmd == "integrate":
        from .integrations import run as irun
        return irun(args)
    try:
        return _run(args)
    except ApiError as e:
        print(f"error: {e.message} [{e.code}]", file=sys.stderr)
        return {"E_PAUSED": 3, "E_NOT_FOUND": 4, "E_NOT_RUNNING": 5, "E_ASSIGNMENT": 6}.get(e.code, 1)


def _caller_kw(args):
    k = _agent(args)
    return {"agent_kind": k} if k else {}


def _run(args) -> int:
    c = Client()
    out = args.json
    cmd = args.cmd

    def show(res, text=None):
        if out or text is None:
            _p(res)
        else:
            print(text)

    if cmd == "list":
        res = c.call("workspace.list")
        if out:
            _p(res)
            return 0
        ws = res["workspaces"]
        if not ws and not args.all:
            print("no workspaces")
            return 0
        print(f"{'NAME':<20} {'ID':<8} {'STATE':<9} {'AGENT':<18} {'INPUT':<7} {'SIZE':<10} WINDOWS")
        for w in ws:
            d = w["display"]
            print(f"{w['name']:<20} {w['id']:<8} {w['state']:<9} {w['agent']['label'][:17]:<18} "
                  f"{w['input_mode']:<7} {d['width']}x{d['height']:<5} {w['activity']['window_count']}")
        return 0
    if cmd == "status":
        if args.workspace:
            _p(c.call("workspace.get", workspace=args.workspace))
        else:
            _p(c.call("hello"))
        return 0
    if cmd == "create":
        w, _, h = args.size.partition("x")
        res = c.call("workspace.create", name=args.name, agent_kind=args.agent, agent_label=args.agent_label,
                     width=int(w), height=int(h), scale=args.scale, gpu=args.gpu, clipboard=args.clipboard,
                     window_policy=args.window_policy, home=args.home, memory_max=args.memory_max,
                     start=not args.no_start, xwayland=not args.no_xwayland)
        show(res, f"created workspace {res['name']} ({res['id']}) state={res['state']}")
        return 0
    if cmd == "rename":
        res = c.call("workspace.rename", workspace=args.workspace, name=args.new_name)
        show(res, f"renamed to {res['name']}")
        return 0
    if cmd == "assign":
        res = c.call("workspace.assign", workspace=args.workspace, agent_kind=args.agent, agent_label=args.label)
        show(res, f"{res['name']} assigned to {res['agent']['label']}")
        return 0
    if cmd == "configure":
        kw = {}
        if args.size:
            w, _, h = args.size.partition("x")
            kw.update(width=int(w), height=int(h))
        for k in ("scale", "gpu", "clipboard", "window_policy"):
            if getattr(args, k) is not None:
                kw[k] = getattr(args, k)
        res = c.call("workspace.configure", workspace=args.workspace, **kw)
        show(res, f"configured {res['name']}")
        return 0
    if cmd in ("start", "stop", "destroy", "resume", "usage"):
        res = c.call(f"workspace.{cmd}", workspace=args.workspace)
        if cmd == "usage" or out:
            _p(res)
        else:
            print(f"{cmd}: ok")
        return 0
    if cmd == "pause":
        res = c.call("workspace.pause", workspace=args.workspace, reason=args.reason)
        show(res, f"{res['name']}: agent input paused (manual takeover)")
        return 0
    if cmd == "launch":
        argvv = list(args.command)
        if argvv and argvv[0] == "--":
            argvv = argvv[1:]
        res = c.call("workspace.launch", workspace=args.workspace, argv=argvv, cwd=args.cwd)
        show(res, f"launched app {res['app']} (pid {res['pid']})")
        return 0
    if cmd == "enter":
        if args.takeover:
            c.call("workspace.pause", workspace=args.workspace, reason="entered by user")
        info = c.call("workspace.get", workspace=args.workspace)
        if info["state"] != "running":
            print(f"workspace is {info['state']}", file=sys.stderr)
            return 5
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ)
        env["PYTHONPATH"] = root + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        cmdl = [sys.executable, "-m", "aw.viewer", info["name"]]
        if args.wait:
            return subprocess.call(cmdl, env=env)
        subprocess.Popen(cmdl, env=env, start_new_session=True, stdin=subprocess.DEVNULL)
        print(f"viewer opened for {info['name']}")
        return 0
    if cmd == "screenshot":
        kw = _caller_kw(args)
        if args.region:
            kw["region"] = [int(v) for v in args.region.split(",")]
        if args.max_width:
            kw["max_width"] = args.max_width
        res = c.call("screen.screenshot", workspace=args.workspace, cursor=args.cursor, inline=True, **kw)
        data = base64.b64decode(res["base64"])
        outp = args.output if args.output != "-" else f"workspace-{res['workspace']}.png"
        with open(outp, "wb") as f:
            f.write(data)
        show({k: v for k, v in res.items() if k != "base64"} | {"path": outp},
             f"{outp} ({res['width']}x{res['height']}, {res['bytes']} bytes, {res['capture_ms']} ms)")
        return 0
    kw = _caller_kw(args)
    if cmd == "windows":
        res = c.call("window.list", workspace=args.workspace, **kw)
        if out:
            _p(res)
        else:
            for w in res["windows"]:
                flags = "".join(f for f, on in (("F", w["focused"]), ("M", w["maximized"]),
                                                ("m", w["minimized"]), ("S", w["fullscreen"])) if on)
                print(f"{w['id']:>3} [{flags:<3}] {w['app_id']:<28} {w['title']}")
        return 0
    if cmd == "display-info":
        _p(c.call("screen.info", workspace=args.workspace, **kw))
        return 0
    if cmd == "window-action":
        show(c.call("window.action", workspace=args.workspace, window=args.window, action=args.action, **kw),
             "ok")
        return 0
    if cmd == "click":
        if (args.x is None) != (args.y is None):
            print("click needs both X and Y (or neither)", file=sys.stderr)
            return 2
        show(c.call("input.click", workspace=args.workspace, x=args.x, y=args.y, button=args.button,
                    count=args.count, **kw), "ok")
        return 0
    if cmd == "move":
        show(c.call("input.move", workspace=args.workspace, x=args.x, y=args.y, **kw), "ok")
        return 0
    if cmd == "drag":
        show(c.call("input.drag", workspace=args.workspace, x1=args.x1, y1=args.y1, x2=args.x2, y2=args.y2,
                    **kw), "ok")
        return 0
    if cmd == "type":
        show(c.call("input.type", workspace=args.workspace, text=args.text, **kw), "ok")
        return 0
    if cmd == "key":
        show(c.call("input.key", workspace=args.workspace, combo=args.combo, **kw), "ok")
        return 0
    if cmd == "scroll":
        x = y = None
        if args.at:
            x, y = [float(v) for v in args.at.split(",")]
        show(c.call("input.scroll", workspace=args.workspace, dy=args.dy, dx=args.dx, x=x, y=y, **kw), "ok")
        return 0
    if cmd == "clipboard":
        if args.op == "get":
            r = c.call("clipboard.get", workspace=args.workspace, **kw)
            if out:
                _p(r)
            else:
                sys.stdout.write(r["text"] or "")
        else:
            c.call("clipboard.set", workspace=args.workspace, text=args.text or "", **kw)
            print("ok")
        return 0
    if cmd == "events":
        try:
            for ev in c.events(workspaces=[args.workspace] if args.workspace else None):
                print(json.dumps(ev), flush=True)
        except KeyboardInterrupt:
            pass
        return 0
    if cmd == "approvals":
        if args.op == "list":
            _p(c.call("approval.list", pending=True))
        elif args.op in ("approve", "deny"):
            _p(c.call("approval.resolve", id=args.arg, decision="approved" if args.op == "approve" else "denied"))
        else:
            _p(c.call("approval.request", workspace=args.workspace, title=args.title or args.arg or "Needs input"))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
