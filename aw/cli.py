"""agent-workspaces: command line interface."""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
from typing import List, Optional

from . import __version__, paths
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
    sub.add_parser("stop-daemon", help="stop the daemon process (workspaces keep running and are re-adopted)")
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
    c.add_argument("--audio", choices=["private", "off"], default="private",
                   help="private: PipeWire null sink per workspace (about 10 MiB, nothing reaches the host "
                        "speakers); off: no audio socket at all")
    c.add_argument("--no-start", action="store_true")
    c.add_argument("--sandbox", action="store_true",
                   help="run the workspace's apps in a bubblewrap sandbox: private HOME, read-only system, "
                        "private /tmp, nothing of your files unless mounted (see docs/isolation-model.md)")
    c.add_argument("--sandbox-no-network", action="store_true",
                   help="with --sandbox: also give the apps no network (and no host abstract sockets)")
    c.add_argument("--sandbox-mount", action="append", default=[], metavar="PATH[:ro|:rw]",
                   help="make a project directory visible inside the sandbox (default read-only); repeatable")

    r = sub.add_parser("rename"); ws_arg(r); r.add_argument("new_name")
    a = sub.add_parser("assign", help="assign an agent to a workspace"); ws_arg(a)
    a.add_argument("agent", help="claude-code|codex|antigravity|hermes|other|none")
    a.add_argument("--label", default="")
    cf = sub.add_parser("configure"); ws_arg(cf)
    cf.add_argument("--size"); cf.add_argument("--scale", type=float)
    cf.add_argument("--gpu", choices=["auto", "off"])
    cf.add_argument("--clipboard", choices=["isolated", "to-workspace", "from-workspace", "both"])
    cf.add_argument("--window-policy", choices=["maximized", "floating"])
    cf.add_argument("--audio", choices=["private", "off"], help="restarts the compositor (apps are closed)")
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
    wm_ = sub.add_parser("window-move", help="move a window (content rectangle origin, logical px)"); ws_arg(wm_)
    wm_.add_argument("window", type=int); wm_.add_argument("x", type=int); wm_.add_argument("y", type=int); caller(wm_)
    wr_ = sub.add_parser("window-resize", help="resize a window (content size, logical px)"); ws_arg(wr_)
    wr_.add_argument("window", type=int); wr_.add_argument("width", type=int)
    wr_.add_argument("height", type=int); caller(wr_)
    wg_ = sub.add_parser("window-geometry", help="move and resize a window in one step"); ws_arg(wg_)
    wg_.add_argument("window", type=int)
    for n in ("x", "y", "width", "height"):
        wg_.add_argument(n, type=int)
    caller(wg_)
    ai = sub.add_parser("audio-info", help="private audio state: sink, daemons, streams, route"); ws_arg(ai)
    ar = sub.add_parser("audio-record", help="record the workspace's audio (its null sink monitor) to a WAV")
    ws_arg(ar); ar.add_argument("seconds", type=float); ar.add_argument("-o", "--output", default=None)
    arr = sub.add_parser("audio-route", help="HUMAN ONLY: play the workspace's audio on the host (off by default)")
    ws_arg(arr); arr.add_argument("to", choices=["host", "none"]); arr.add_argument("--sink", help="host sink name")
    arr.add_argument("--yes", action="store_true", help="skip the confirmation (needs a terminal otherwise)")
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
    tk = sub.add_parser("token", help="workspace credentials (the token itself is never printed unless 'issue')")
    tk.add_argument("op", choices=["path", "list", "rotate", "issue", "revoke", "whoami"])
    tk.add_argument("workspace", nargs="?", metavar="NAME")
    tk.add_argument("--scopes", help="comma list (issue): read,screenshot,input,launch,windows,clipboard,approval")
    tk.add_argument("--ttl", type=float, default=900, help="seconds (issue), default 900, max 86400")
    tk.add_argument("--label", default="")
    tk.add_argument("--id", help="credential id (revoke); without it all non-default credentials of the workspace")
    tk.add_argument("--admin", action="store_true", help="rotate: the admin credential instead of a workspace's")
    gr = sub.add_parser("grants", help="human authorisation of sensitive operations (interactive terminal only)")
    gr.add_argument("op", choices=["list", "request", "approve", "deny", "revoke"])
    gr.add_argument("arg", nargs="?", help="grant id (approve/deny/revoke) or kind (request)")
    gr.add_argument("--workspace")
    gr.add_argument("--resource", default="")
    gr.add_argument("--detail", default="")
    gr.add_argument("--ttl", type=float, default=900, help="seconds the grant lasts (approve), default 900")
    au = sub.add_parser("audit", help="show the audit log (who did what; never contains secrets or contents)")
    au.add_argument("-n", type=int, default=30)
    from . import cli_browsers
    cli_browsers.add_parsers(sub)
    ig = sub.add_parser("integrate", help="opt-in agent integration (reversible)")
    ig.add_argument("agent", choices=["claude-code", "codex", "antigravity", "hermes"])
    ig.add_argument("action", choices=["status", "install", "uninstall", "show", "run"])
    ig.add_argument("prompt", nargs="?", help="with 'run': the one-off prompt")
    ig.add_argument("--no-guard", action="store_true", help="do not install the graphical-isolation guard (Claude Code, Hermes)")
    ig.add_argument("--dry-run", action="store_true", help="print what install/uninstall would do")
    ig.add_argument("--model", help="with 'run': model name (default: the cheapest suitable one)")
    ig.add_argument("--project", help="with 'run': project name used in the workspace name")
    ig.add_argument("--workspace", help="default workspace for this agent")
    ig.add_argument("--config", help="override the agent config path (testing)")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "stop-daemon":
        import signal
        pf = os.path.join(paths.base_dir(), "daemon.pid")
        try:
            pid = int(open(pf).read())
            if b"aw.daemon" not in open(f"/proc/{pid}/cmdline", "rb").read() and b"agent-workspaces" not in open(f"/proc/{pid}/cmdline", "rb").read():
                print("the recorded pid is not the daemon; nothing stopped", file=sys.stderr)
                return 1
            os.kill(pid, signal.SIGTERM)
            print(f"daemon (pid {pid}) told to stop; workspaces keep running")
            return 0
        except (OSError, ValueError):
            print("no daemon is running")
            return 0
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


def _token(c: Client, args) -> int:
    if args.op == "whoami":
        _p(c.call("auth.whoami"))
        return 0
    if args.op == "rotate" and args.admin:
        r = c.call("credential.rotate", admin=True)
        print(f"admin credential rotated; new token in {r['token_file']}")
        return 0
    if not args.workspace:
        print("token: a workspace name is required", file=sys.stderr)
        return 2
    if args.op == "path":
        _p({"token_file": c.call("credential.list", workspace=args.workspace)["token_file"]})
    elif args.op == "list":
        _p(c.call("credential.list", workspace=args.workspace))
    elif args.op == "rotate":
        r = c.call("credential.rotate", workspace=args.workspace)
        print(f"rotated; the old token no longer works. New token file: {r['token_file']}")
    elif args.op == "revoke":
        r = c.call("credential.revoke", workspace=args.workspace, **({"id": args.id} if args.id else {}))
        print(f"revoked {r['revoked']} credential(s)")
    elif args.op == "issue":
        kw = {"scopes": args.scopes.split(",")} if args.scopes else {}
        r = c.call("credential.issue", workspace=args.workspace, ttl_seconds=args.ttl, label=args.label, **kw)
        # the one place a token is printed: stdout, on request. Put it in AW_TOKEN of the session that needs it.
        print(r["token"])
        print(f"# credential {r['credential']}, scopes {','.join(r['scopes'])}, expires_at {r['expires_at']}",
              file=sys.stderr)
    return 0


def _grants(c: Client, args) -> int:
    if args.op == "list":
        _p(c.call("grant.list", **({"workspace": args.workspace} if args.workspace else {})))
        return 0
    if args.op == "request":
        if not (args.workspace and args.arg):
            print("grants request --workspace NAME KIND [--resource R]", file=sys.stderr)
            return 2
        _p(c.call("grant.request", workspace=args.workspace, kind=args.arg, resource=args.resource,
                  detail=args.detail))
        return 0
    # approve / deny / revoke: a person at an interactive terminal, never a script or an agent
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("grants approve|deny|revoke need an interactive terminal: this decision is the user's.",
              file=sys.stderr)
        return 7
    if not args.arg:
        print("grant id required (see: agent-workspaces grants list)", file=sys.stderr)
        return 2
    g = next((x for x in c.call("grant.list")["grants"] if x["id"] == args.arg), None)
    if g is None:
        print(f"no grant {args.arg}", file=sys.stderr)
        return 4
    print(f"Workspace {g['workspace']} asks to: {g['description']}\n  kind: {g['kind']}"
          f"{'  resource: ' + g['resource'] if g['resource'] else ''}\n  detail: {g['detail'] or '-'}")
    word = {"approve": "yes", "deny": "no", "revoke": "revoke"}[args.op]
    if input(f"Type '{word}' to {args.op} (anything else cancels): ").strip().lower() != word:
        print("cancelled")
        return 1
    if args.op == "revoke":
        _p(c.call("grant.revoke", id=args.arg))
    else:
        _p(c.call("grant.resolve", id=args.arg, decision="granted" if args.op == "approve" else "denied",
                  ttl_seconds=args.ttl))
    return 0


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
                     start=not args.no_start, xwayland=not args.no_xwayland, sandbox=bool(args.sandbox),
                     sandbox_mounts=args.sandbox_mount, sandbox_net=not args.sandbox_no_network, audio=args.audio)
        show(res, f"created workspace {res['name']} ({res['id']}) state={res['state']}\n"
                  f"credential file for the agent: {res['token_file']}")
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
        for k in ("scale", "gpu", "clipboard", "window_policy", "audio"):
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
                g = w.get("geometry") or {}
                geo = (f"{g['width']}x{g['height']}+{g['x']}+{g['y']}" if g.get("width") is not None
                       else "?")
                print(f"{w['id']:>3} {w.get('kind', 'wayland'):<7} [{flags:<4}] {geo:<16} "
                      f"{w['app_id']:<24} {w['title']}")
        return 0
    if cmd == "display-info":
        _p(c.call("screen.info", workspace=args.workspace, **kw))
        return 0
    if cmd == "window-action":
        show(c.call("window.action", workspace=args.workspace, window=args.window, action=args.action, **kw),
             "ok")
        return 0
    if cmd in ("browsers", "launch-browser", "profile"):
        from . import cli_browsers
        return cli_browsers.run(args, c, show, out)
    if cmd == "audio-info":
        _p(c.call("audio.info", workspace=args.workspace, **kw))
        return 0
    if cmd == "audio-record":
        outp = args.output or f"workspace-{args.workspace}.wav"
        res = c.call("audio.record", workspace=args.workspace, seconds=args.seconds,
                     path=os.path.abspath(outp), **kw)
        show(res, f"{res['path']}: {res['seconds']} s, peak {res['peak']}, rms {res['rms_dbfs']} dBFS"
             + (f", tone {res['tone_hz']} Hz" if res.get("tone_hz") else ""))
        return 0
    if cmd == "audio-route":
        if args.to == "host" and not args.yes:
            if not sys.stdin.isatty():
                print("refusing: routing to the host speakers needs a terminal (or --yes)", file=sys.stderr)
                return 2
            if input(f"Play the audio of workspace {args.workspace!r} on your host speakers? [y/N] ").strip().lower() != "y":
                print("not changed")
                return 1
        res = c.call("audio.route", workspace=args.workspace, to=args.to, sink=args.sink)
        show(res, f"audio route: {res['to']}")
        return 0
    if cmd in ("window-move", "window-resize", "window-geometry"):
        method = {"window-move": "window.move", "window-resize": "window.resize",
                  "window-geometry": "window.set_geometry"}[cmd]
        g = {k: getattr(args, k) for k in ("x", "y", "width", "height") if hasattr(args, k)}
        res = c.call(method, workspace=args.workspace, window=args.window, **g, **kw)
        win = res.get("window") or {}
        geo = win.get("geometry") or {}
        show(res, "ok " + (f"{geo.get('width')}x{geo.get('height')}+{geo.get('x')}+{geo.get('y')} "
                           f"({win.get('geometry_source')})" if geo else ""))
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
    if cmd == "token":
        return _token(c, args)
    if cmd == "grants":
        return _grants(c, args)
    if cmd == "audit":
        for ev in c.call("audit.tail", n=args.n)["events"]:
            print(json.dumps(ev))
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
