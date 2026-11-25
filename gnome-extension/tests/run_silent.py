#!/usr/bin/python3 -I
"""With no agent workspace the extension must be SILENT: no viewer, no overlay, no notification, no animation, nothing in the
Super overview, nothing at login/enable time. And the service robustness: with the daemon down it is started (systemd unit
first, else the daemon itself, bounded), and a running workspace is found again after the daemon restarts.

ISOLATED headless GNOME Shell with the developer's extension set (private keyfile, private buses; the harness asserts the
private bus before every start). The "service" is the real daemon started BY THE EXTENSION through its `daemon-command`
setting, in the private runtime directory: `systemctl --user` cannot reach any real systemd from there.
"""
import json
import os
import signal
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(os.path.dirname(HERE))]
from aw.client import Client  # noqa: E402
from gnome_session import Session, REALISTIC_COPY, REALISTIC_SYSTEM, UUID  # noqa: E402
from live_env import PROBE_APP, ROOT, wait_for  # noqa: E402

VIEWER = "dev.agentworkspaces.Viewer"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f"   [{detail}]" if detail else ""), flush=True)


def snap(s):
    st = s.state()
    ext = s.eval(f"""const e = Main.extensionManager.lookup('{UUID}').stateObj; const g = global.window_group;
      const c = Main.overview._overview.controls;
      return {{notes: Main.messageTray.getSources().map(x => x.title + ':' + (x.notifications?.length ?? 0)),
              actors: global.get_window_actors().length, uivis: Main.uiGroup.get_children().filter(x => x.visible).length, uinames: Main.uiGroup.get_children().map(x => x.constructor.name + ':' + (x.style_class ?? '') + (x.visible ? '' : ':hidden')), sliding: !!e._slide,
              wg: [g.translation_y, g.rotation_angle_x, g.translation_z, g.opacity], active: global.workspace_manager.get_active_workspace_index(),
              n: global.workspace_manager.n_workspaces, overview: Main.overview.visible, strip: !!e._strip && e._strip.actor.visible,
              ctrl: [c.scale_x, c.translation_y], viewer: global.get_window_actors().some(a => (a.meta_window.get_gtk_application_id?.() ?? a.meta_window.get_wm_class()) === '{VIEWER}')}};""")
    return ext


def newer(a, b):
    from collections import Counter
    return dict(Counter(a["uinames"]) - Counter(b["uinames"]))


def daemon_pids(s):
    out = []
    for pid in s.mine():
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
        except OSError:
            continue
        if any(c == b"aw.daemon" for c in cmd) or (b"daemon" in cmd and any(c.endswith(b"agent-workspaces") for c in cmd)):
            out.append(pid)
    return out


def main():
    s = Session(monitors=("1600x900",), copy_extensions=REALISTIC_COPY, enable_extensions=REALISTIC_SYSTEM, dump_user_settings=True)
    base = os.path.join(s.rt, "agent-workspaces")
    try:
        s.start()
        time.sleep(2.0)
        s.eval("Main.overview.hide(); return 1")
        time.sleep(1.0)
        # nothing at login/enable time
        a0 = snap(s)
        check("nothing at login/enable time: no viewer window, no overlay, no strip, nothing sliding, window group untouched",
              not a0["viewer"] and not a0["sliding"] and not a0["strip"] and a0["wg"] == [0, 0, 0, 255] and a0["ctrl"] == [1, 0],
              json.dumps(a0))
        s.launch([sys.executable, os.path.join(HERE, "standin_viewer.py"), "Personal", "dev.agentworkspaces.TestPersonal"], "p.log")
        assert wait_for(lambda: s.state()["windows"], 25)
        time.sleep(2.0)
        s.eval("Main.overview.hide(); return 1")
        time.sleep(1.0)
        a1 = snap(s)
        ext_cmd = lambda v: s.eval(f"Main.extensionManager.lookup('{UUID}').stateObj._settings.set_string('daemon-command', {json.dumps(v)}); return 1")  # noqa: E731

        # ---- (b) daemon down, no workspaces, nothing that can start it: silent
        ext_cmd("/nonexistent/agent-workspaces")
        s.chord("Control_L", "Alt_L", "Up")
        time.sleep(3.0)
        a = snap(s)
        same = {k: a[k] for k in ("notes", "actors", "uivis", "sliding", "wg", "active", "n", "overview", "viewer")} == \
               {k: a1[k] for k in ("notes", "actors", "uivis", "sliding", "wg", "active", "n", "overview", "viewer")}
        check("(b) daemon down and no workspaces (and the service cannot be started): Ctrl+Alt+Up does nothing visible - no window, "
              "overlay, animation or notification, window group untouched", same and not os.path.exists(f"{base}/api.sock"), json.dumps(a) + " baseline " + json.dumps(a1))
        time.sleep(5.0)       # the extension waits up to 6 s for a service that cannot start before giving up
        log = open(s.log, errors="replace").read()
        check("(b) ... the reason is only in the shell log", "enter ignored: the service is not running" in log)
        a = snap(s)
        check("(b) ... and nothing appeared when it gave up (no notification, window or overlay)",
              {k: a[k] for k in ("notes", "actors", "sliding", "wg", "active", "n", "viewer")} == {k: a1[k] for k in ("notes", "actors", "sliding", "wg", "active", "n", "viewer")},
              json.dumps(a))

        # ---- the service is started on demand: systemd unit first (unreachable here), then the daemon itself
        ext_cmd(os.path.join(ROOT, "bin", "agent-workspaces"))
        s.chord("Control_L", "Alt_L", "Up")
        up = wait_for(lambda: os.path.exists(f"{base}/api.sock"), 15, 0.2)
        time.sleep(2.0)
        a = snap(s)
        same = {k: a[k] for k in ("notes", "actors", "uivis", "sliding", "wg", "active", "n", "overview", "viewer")} == \
               {k: a1[k] for k in ("notes", "actors", "uivis", "sliding", "wg", "active", "n", "overview", "viewer")}
        check("daemon down: the extension starts the service (unit not available here, so the daemon itself) and, with still no "
              "workspace, stays completely silent", bool(up) and same, f"up={up} new actors: " + json.dumps(newer(a, a1)))

        # ---- (a) daemon up, no workspaces
        s.chord("Control_L", "Alt_L", "Up")
        time.sleep(2.0)
        a = snap(s)
        check("(a) daemon up, no workspaces: Ctrl+Alt+Up does nothing visible (no window actor, no overlay, no slide, no notification, "
              "window group untouched)", {k: a[k] for k in ("notes", "actors", "uivis", "sliding", "wg", "active", "n", "viewer")} ==
              {k: a1[k] for k in ("notes", "actors", "uivis", "sliding", "wg", "active", "n", "viewer")}, json.dumps(a) + " new actors: " + json.dumps(newer(a, a1)))
        # ---- (d) the overview shows nothing extra
        s.chord("Super_L")
        wait_for(lambda: s.state()["overview"], 5, 0.1)
        time.sleep(3.0)
        a = snap(s)
        check("(d) Super with no workspaces: nothing extra in the overview (no strip, GNOME's controls where they were)",
              a["overview"] and not a["strip"] and a["ctrl"] == [1, 0], json.dumps(a))
        s.shot(f"{s.base}/overview-empty.png")
        s.chord("Control_L", "Alt_L", "Up")              # from the overview: still nothing
        time.sleep(2.0)
        a = snap(s)
        check("(d) Ctrl+Alt+Up from the overview with no workspaces: nothing happens (the overview just closes or stays; no viewer)",
              not a["viewer"] and a["active"] == a1["active"] and not a["sliding"] and a["notes"] == a1["notes"], json.dumps(a))
        s.eval("Main.overview.hide(); return 1")
        time.sleep(1.0)

        # ---- (c) one running workspace: enters normally
        os.environ["AW_BASE_DIR"] = base
        c = Client(autostart=False, role="admin")
        c.call("workspace.create", name="research", agent_kind="claude-code", width=1280, height=720)
        lg = f"{s.base}/r.log"
        open(lg, "w").close()
        c.call("workspace.launch", workspace="research", argv=[sys.executable, PROBE_APP, "AGENT", lg, "#9c4221"])
        assert wait_for(lambda: os.path.getsize(lg) > 0, 20)
        time.sleep(1.0)
        s.chord("Control_L", "Alt_L", "Up")
        ok = wait_for(lambda: snap(s)["viewer"], 20, 0.2)
        time.sleep(2.5)
        a = snap(s)
        check("(c) with one running workspace Ctrl+Alt+Up enters: the viewer opens on the agent desktop (slide)",
              bool(ok) and a["active"] >= 1 and a["active"] != a1["active"], json.dumps(a))
        s.chord("Control_L", "Alt_L", "Down")
        time.sleep(2.0)
        check("(c) and Ctrl+Alt+Down comes back", snap(s)["active"] == a1["active"])

        # ---- the daemon dies (workspaces keep running): the next Up restarts the service and finds the workspace again
        pids = daemon_pids(s)
        for pid in pids:
            os.kill(pid, signal.SIGTERM)
        gone = wait_for(lambda: not os.path.exists(f"{base}/api.sock") and not daemon_pids(s), 10, 0.2)
        # close the viewer first so that entering has to start it again
        s.eval("""for (const a of global.get_window_actors()) { const w = a.meta_window;
                  if ((w.get_gtk_application_id?.() ?? w.get_wm_class()) === 'dev.agentworkspaces.Viewer') w.delete(global.get_current_time()); } return 1""")
        time.sleep(2.0)
        s.chord("Control_L", "Alt_L", "Up")
        ok = wait_for(lambda: snap(s)["viewer"], 25, 0.2)
        time.sleep(2.5)
        a = snap(s)
        check("daemon stopped (exact pids, workspaces still running): Ctrl+Alt+Up restarts the service, it re-adopts the workspace and "
              "the agent desktop opens", bool(gone) and bool(ok) and a["active"] != a1["active"], f"stopped={pids} {json.dumps(a)}")
        s.chord("Control_L", "Alt_L", "Down")
        time.sleep(2.0)

        log = open(s.log, errors="replace").read()
        check("no JS errors in the shell log", not s.shell_errors(), "; ".join(s.shell_errors()[:2]))
    finally:
        try:
            Client(autostart=False, role="admin")
            for w in Client(autostart=False, role="admin").call("workspace.list")["workspaces"]:
                Client(autostart=False, role="admin").call("workspace.destroy", workspace=w["id"])
        except Exception:
            pass
        s.stop()
        print("leftover processes carrying the private runtime dir:", s.mine() or "none")
    bad = [m for m, ok in RESULTS if not ok]
    print(f"{len(RESULTS) - len(bad)}/{len(RESULTS)} checks passed" + (f"; FAILED: {bad}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
