#!/usr/bin/python3 -I
"""Ctrl+Alt+Up ALWAYS goes up to the agent desktop, and it does so quietly.

With nothing running the extension asks the service for the default desktop (`workspace.ensure_default`: a workspace
called `agents`, no agent assigned, created or started on first use), starts the service first when it is down, and
slides up as soon as the viewer has a first frame. It never shows a notification, toast or dialog; when something is wrong
the viewer says so, once, inside its own window. Nothing is created at login or enable time.

Checked here, in an ISOLATED headless GNOME Shell with the developer's extension set (private keyfile, private buses; the
harness asserts the private bus before every start):
  - nothing at login/enable time (no workspace, no service, no viewer, no strip)
  - (A) cold start: service down, nothing running -> Up starts the service, creates `agents`, enters
  - (B) second Up/Down cycles reuse it (no second workspace, no second viewer)
  - (C) a stopped `agents` is started again by Up
  - (D) the Super overview lists it like any running workspace; a click and Ctrl+Alt+Up from the overview enter it
  - (E) service up, `agents` gone: Up creates it again, and the slide starts only when the viewer window has its first frame
  - (F) creation fails (service up): the viewer says so once, in its own window, with a button that works afterwards
  - (G) the service cannot be started at all: the viewer says so, once, in its own window
  - no notification, popup or leftover actor in any of it, and no JS error in the shell log
The "service" is the real daemon started BY THE EXTENSION through its `daemon-command` setting, in the private runtime
directory: `systemctl --user` cannot reach any real systemd from there.
"""
import json
import os
import signal
import stat
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(os.path.dirname(HERE))]
from aw.client import ApiError, Client  # noqa: E402
from gnome_session import Session, REALISTIC_COPY, REALISTIC_SYSTEM, UUID  # noqa: E402
from live_env import ROOT, wait_for  # noqa: E402

VIEWER = "dev.agentworkspaces.Viewer"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f"   [{detail}]" if detail else ""), flush=True)


def short(a):
    return json.dumps({k: v for k, v in a.items() if k != "uinames"})


def snap(s):
    return s.eval(f"""const e = Main.extensionManager.lookup('{UUID}').stateObj; const g = global.window_group;
      const c = Main.overview._overview.controls;
      const v = global.get_window_actors().map(a => a.meta_window).filter(w => (w.get_gtk_application_id?.() ?? w.get_wm_class()) === '{VIEWER}');
      return {{notes: Main.messageTray.getSources().map(x => x.title + ':' + (x.notifications?.length ?? 0)),
              actors: global.get_window_actors().length, uinames: Main.uiGroup.get_children().map(x => x.constructor.name + ':' + (x.style_class ?? '') + (x.visible ? '' : ':hidden')),
              sliding: !!e._slide, entering: !!e._entering, wg: [g.translation_y, g.rotation_angle_x, g.translation_z, g.opacity],
              active: global.workspace_manager.get_active_workspace_index(), n: global.workspace_manager.n_workspaces,
              overview: Main.overview.visible, strip: !!e._strip && e._strip._open,
              ctrl: [c.scale_x, c.translation_y], viewers: v.length, viewer_ws: v.map(w => w.get_workspace()?.index() ?? -1),
              agent: e._agentWs ? e._agentWs.index() : -1, samples: e._lastSamples ?? []}};""")


def newer(a, b):
    """Visible actors in the shell's UI layer that were not there in b (hidden ones, such as the dock's tooltip labels GNOME
    creates on demand, do not count)."""
    from collections import Counter
    vis = lambda x: Counter(n for n in x["uinames"] if not n.endswith(":hidden"))  # noqa: E731
    return dict(vis(a) - vis(b))


def daemon_pids(s):
    out = []
    for pid in s.mine():
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
        except OSError:
            continue
        if any(c == b"aw.daemon" for c in cmd) or (b"daemon" in cmd and any(c.endswith(b"agent-workspaces") or c.endswith(b"aw-wrapper") for c in cmd)):
            out.append(pid)
    return out


def workspaces(base):
    os.environ["AW_BASE_DIR"] = base
    try:
        c = Client(autostart=False, role="admin")
        try:
            return c.call("workspace.list")["workspaces"]
        finally:
            c.close()
    except (ApiError, OSError):
        return None


def call(base, method, **p):
    os.environ["AW_BASE_DIR"] = base
    c = Client(autostart=False, role="admin")
    try:
        return c.call(method, **p)
    finally:
        c.close()


def wrapper(path, daemon_line=":", viewer_line=":"):
    """A stand-in `agent-workspaces` command: the real one, but `daemon` / `viewer` can be made to misbehave."""
    with open(path, "w") as f:
        f.write(f"""#!/bin/sh
if [ "$1" = daemon ]; then
{daemon_line}
fi
if [ "$1" = viewer ]; then
{viewer_line}
fi
exec /usr/bin/python3 -I {ROOT}/bin/agent-workspaces "$@" >> {path}.log 2>&1
""")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return path


def kill_daemon(s, base):
    pids = daemon_pids(s)
    for pid in pids:
        os.kill(pid, signal.SIGTERM)
    wait_for(lambda: not os.path.exists(f"{base}/api.sock") and not daemon_pids(s), 10, 0.2)
    return pids


def main():
    s = Session(monitors=("1600x900",), copy_extensions=REALISTIC_COPY, enable_extensions=REALISTIC_SYSTEM, dump_user_settings=True)
    base = os.path.join(s.rt, "agent-workspaces")
    quiet = ("notes", "sliding", "wg")
    try:
        s.start()
        time.sleep(2.0)
        s.eval("Main.overview.hide(); return 1")
        time.sleep(1.0)
        a0 = snap(s)
        check("nothing at login/enable time: no service, no workspace, no viewer window, no overlay, no strip, nothing sliding, "
              "window group untouched", a0["viewers"] == 0 and not a0["sliding"] and not a0["strip"] and a0["wg"] == [0, 0, 0, 255]
              and a0["ctrl"] == [1, 0] and not os.path.exists(f"{base}/api.sock"), short(a0))
        s.launch([sys.executable, os.path.join(HERE, "standin_viewer.py"), "Personal", "dev.agentworkspaces.TestPersonal"], "p.log")
        assert wait_for(lambda: s.state()["windows"], 25)
        time.sleep(2.0)
        s.eval("Main.overview.hide(); return 1")
        time.sleep(1.0)
        a1 = snap(s)
        ext_cmd = lambda v: s.eval(f"Main.extensionManager.lookup('{UUID}').stateObj._settings.set_string('daemon-command', {json.dumps(v)}); return 1")  # noqa: E731
        real = os.path.join(ROOT, "bin", "agent-workspaces")

        # ---- (A) cold start: the service is down and nothing runs
        ext_cmd(real)
        t0 = time.monotonic()
        s.chord("Control_L", "Shift_L", "Alt_L", "Up")
        ok = wait_for(lambda: snap(s)["viewers"] > 0 and snap(s)["active"] != a1["active"], 40, 0.2)
        took = time.monotonic() - t0
        time.sleep(3.0)
        a = snap(s)
        wl = workspaces(base) or []
        agents = [w for w in wl if w["name"] == "agents"]
        s.shot(f"{s.base}/cold-start.png")
        check("(A) cold start, service down and nothing running: Ctrl+Alt+Up starts the service, creates `agents` (agent kind none, "
              "running) and enters it", bool(ok) and len(wl) == 1 and len(agents) == 1 and agents[0]["state"] == "running"
              and agents[0]["agent"]["kind"] == "none", f"{took:.1f}s workspaces={[(w['name'], w['state'], w['agent']['kind']) for w in wl]}")
        check("(A) ... the viewer is on the agent desktop, which is a separate workspace above where you were",
              a["viewers"] == 1 and a["viewer_ws"] == [a["active"]] and a["active"] == a["agent"] and a["active"] != a1["active"], short(a))
        check("(A) ... without any notification, toast, dialog or leftover animation actor",
              {k: a[k] for k in ("notes", "sliding", "wg")} == {k: a1[k] for k in ("notes", "sliding", "wg")} and not newer(a, a1), json.dumps(newer(a, a1)))
        aid = agents[0]["id"] if agents else None
        s.chord("Control_L", "Shift_L", "Alt_L", "Down")
        time.sleep(2.0)
        check("(A) Ctrl+Alt+Down comes back to the personal desktop you left", snap(s)["active"] == a1["active"])

        # ---- (B) cycles reuse the workspace and the viewer
        ok_cycles = True
        for i in range(3):
            s.chord("Control_L", "Shift_L", "Alt_L", "Up")
            time.sleep(1.2)
            a = snap(s)
            up = a["active"] == a["agent"] and a["active"] != a1["active"] and a["viewers"] == 1
            s.chord("Control_L", "Shift_L", "Alt_L", "Down")
            time.sleep(1.2)
            b = snap(s)
            back = b["active"] == a1["active"] and b["viewers"] == 1
            ok_cycles = ok_cycles and up and back
            if not (up and back):
                print("cycle", i, short(a), json.dumps(b))
        wl = workspaces(base) or []
        check("(B) three more Up/Down cycles: always enters and returns, one workspace (the same `agents`), one viewer window",
              ok_cycles and len(wl) == 1 and wl[0]["id"] == aid)
        a = snap(s)
        check("(B) ... still no notification or popup", {k: a[k] for k in quiet} == {k: a1[k] for k in quiet} and not newer(a, a1), json.dumps(newer(a, a1)))

        # ---- (C) stopped: Up starts it again
        call(base, "workspace.stop", workspace=aid)
        time.sleep(1.5)
        s.chord("Control_L", "Shift_L", "Alt_L", "Up")
        ok = wait_for(lambda: (workspaces(base) or [{}])[0].get("state") == "running", 20, 0.3)
        time.sleep(2.0)
        a = snap(s)
        check("(C) `agents` stopped: Ctrl+Alt+Up goes up and starts it again (same workspace, running)",
              bool(ok) and a["active"] == a["agent"] and (workspaces(base) or [{}])[0].get("id") == aid, short(a))
        s.chord("Control_L", "Shift_L", "Alt_L", "Down")
        time.sleep(1.8)

        # ---- (D) the overview lists it; click and Ctrl+Alt+Up from the overview enter it
        s.chord("Super_L")
        wait_for(lambda: s.state()["overview"], 5, 0.1)
        time.sleep(3.0)
        thumb = """const e = Main.extensionManager.lookup('%s').stateObj; const box = Main.overview._overview.controls._thumbnailsBox;
          const ts = box._thumbnails; const t = ts.find(x => x.metaWorkspace === e._agentWs); if (!t) return null;
          const [x, y] = t.get_transformed_position(); const [w, h] = t.get_transformed_size();
          return [x + w / 2, y + h / 2, ts[0] === t, t.metaWorkspace.index(), ts.length];""" % UUID
        tile = s.eval(thumb)
        s.shot(f"{s.base}/overview-agents.png")
        check("(D) the overview shows the `agents` desktop as the FIRST thumbnail of GNOME's own row, like any other desktop", bool(tile) and tile[2] and tile[3] == 0, json.dumps(tile))
        if tile:
            s.click(int(tile[0]), int(tile[1]))
            ok = wait_for(lambda: snap(s)["active"] == snap(s)["agent"], 8, 0.2)
            time.sleep(1.5)
            check("(D) a click on its thumbnail selects it like any desktop (overview stays open, the big area shows it)", bool(ok) and snap(s)["overview"], short(snap(s)))
            s.click(800, 520)
            ok = wait_for(lambda: not snap(s)["overview"], 8, 0.2)
            time.sleep(1.5)
            a = snap(s)
            check("(D) activating it closes the overview on the agent desktop (the agent workspace is back after your own desktops, not left first)",
                  bool(ok) and a["active"] == a["agent"] and a["agent"] > 0, short(a))
            s.chord("Control_L", "Shift_L", "Alt_L", "Down")
            time.sleep(1.8)
        s.chord("Super_L")
        wait_for(lambda: s.state()["overview"], 5, 0.1)
        time.sleep(2.0)
        s.chord("Control_L", "Shift_L", "Alt_L", "Up")
        ok = wait_for(lambda: snap(s)["active"] == snap(s)["agent"] and not snap(s)["overview"], 8, 0.2)
        time.sleep(1.5)
        check("(D) Ctrl+Alt+Up from the overview closes it and enters the agent desktop", bool(ok), short(snap(s)))
        s.chord("Control_L", "Shift_L", "Alt_L", "Down")
        time.sleep(1.8)

        # ---- (E) service up, `agents` gone: Up creates it again; the slide starts at the viewer's first frame
        call(base, "workspace.destroy", workspace=aid)
        s.eval("""const e = Main.extensionManager.lookup('%s').stateObj; for (const w of e._viewerWindows()) w.delete(global.get_current_time()); return 1""" % UUID)
        time.sleep(2.0)
        assert workspaces(base) == [], workspaces(base)
        s.eval("""const e = Main.extensionManager.lookup('%s').stateObj; globalThis.__awt = {t0: 0, map: 0, first: 0, slide: 0};
          const g = global.window_group; g.connect('notify::translation-y', () => { if (!globalThis.__awt.slide && g.translation_y !== 0) globalThis.__awt.slide = GLib.get_monotonic_time(); });
          globalThis.__awt.mapId = global.window_manager.connect('map', (wm, actor) => { if (!globalThis.__awt.map) globalThis.__awt.map = GLib.get_monotonic_time(); });
          return 1""" % UUID)
        s.eval("globalThis.__awt.t0 = GLib.get_monotonic_time(); return 1")
        s.chord("Control_L", "Shift_L", "Alt_L", "Up")
        ok = wait_for(lambda: snap(s)["viewers"] > 0 and snap(s)["active"] != a1["active"], 30, 0.1)
        time.sleep(2.5)
        t = s.eval("const t = globalThis.__awt; global.window_manager.disconnect(t.mapId); return [(t.map - t.t0) / 1000, (t.slide - t.t0) / 1000];")
        a = snap(s)
        wl = workspaces(base) or []
        check("(E) service up and no workspace: Up creates `agents` again and enters it", bool(ok) and len(wl) == 1 and wl[0]["name"] == "agents"
              and wl[0]["state"] == "running" and a["active"] == a["agent"], f"viewer mapped at {t[0]:.0f} ms, slide started at {t[1]:.0f} ms")
        check("(E) the slide starts when the viewer window has mapped (not before: no empty desktop slides in), within 1.2 s + the slide itself",
              t[1] > 0 and t[1] >= t[0] - 5 and t[1] - t[0] < 400, f"map {t[0]:.0f} ms, slide {t[1]:.0f} ms")
        a = snap(s)
        check("(E) no notification or popup", {k: a[k] for k in quiet} == {k: a1[k] for k in quiet} and not newer(a, a1), json.dumps(newer(a, a1)))
        # the launcher in the agent taskbar: a person opens an app INSIDE this workspace
        s.click(114, 880)
        time.sleep(1.2)
        s.shot(f"{s.base}/launcher-open.png")
        s.type_text("calculator")
        time.sleep(0.6)
        s.key("Return")
        got = wait_for(lambda: any("alculator" in (w["title"] or "") for w in call(base, "window.list", workspace="agents")["windows"]), 25, 0.3)
        time.sleep(1.0)
        info = call(base, "workspace.get", workspace="agents")
        personal = [w["title"] for w in s.state()["windows"] if "alculator" in (w["title"] or "")]
        s.shot(f"{s.base}/launcher-app.png")
        check("(E) the launcher in the agent taskbar opens an app inside the `agents` workspace, as you (activity 'You'), and nothing on your own desktop",
              bool(got) and not personal and (info.get("activity") or {}).get("last_action") == "human:launch",
              f"windows={[w['title'] for w in info.get('windows', [])]} activity={(info.get('activity') or {}).get('last_action')} personal={personal}")
        s.chord("Control_L", "Shift_L", "Alt_L", "Down")
        time.sleep(1.8)

        # ---- (F) creation fails while the service is up: the viewer says so, once, in its own window
        call(base, "workspace.destroy", workspace=(workspaces(base) or [{"id": "x"}])[0]["id"])
        kill_daemon(s, base)
        s.eval("""const e = Main.extensionManager.lookup('%s').stateObj; for (const w of e._viewerWindows()) w.delete(global.get_current_time()); return 1""" % UUID)
        time.sleep(2.0)
        nolabwc = wrapper(f"{s.base}/aw-wrapper-nolabwc", "export PATH=/nonexistent")
        ext_cmd(nolabwc)
        s.chord("Control_L", "Shift_L", "Alt_L", "Up")
        ok = wait_for(lambda: snap(s)["viewers"] > 0 and snap(s)["active"] != a1["active"], 40, 0.2)
        time.sleep(2.0)
        s.shot(f"{s.base}/fail-starting.png")
        time.sleep(8.0)                               # the viewer's grace period for the first workspace is 8 s
        a = snap(s)
        s.shot(f"{s.base}/fail-message.png")
        check("(F) the desktop could not be created: Ctrl+Alt+Up still goes up (the viewer window is there), no popup or notification",
              bool(ok) and a["active"] == a["agent"] and {k: a[k] for k in quiet} == {k: a1[k] for k in quiet} and not newer(a, a1), short(a))
        log = open(s.log, errors="replace").read()
        if not (ok and a["viewers"]):
            for name in ("viewer.log", "daemon.log"):
                try:
                    print(f"--- {name}:\n" + open(f"{base}/{name}", errors="replace").read()[-1500:])
                except OSError as e:
                    print(f"--- {name}: {e}")
            print("--- wrapper log:\n" + (open(f"{s.base}/aw-wrapper-nolabwc.log", errors="replace").read()[-1500:] if os.path.exists(f"{s.base}/aw-wrapper-nolabwc.log") else "(none)"))
            print("--- shell log:\n" + "\n".join(l for l in log.splitlines() if "agent-workspaces" in l)[-1500:])
        check("(F) ... the reason is in the shell log", "no workspace to go up to" in log)
        s.chord("Control_L", "Shift_L", "Alt_L", "Down")
        time.sleep(1.8)

        # ---- (G) the service cannot be started at all
        kill_daemon(s, base)
        s.eval("""const e = Main.extensionManager.lookup('%s').stateObj; for (const w of e._viewerWindows()) w.delete(global.get_current_time()); return 1""" % UUID)
        time.sleep(2.0)
        dead = wrapper(f"{s.base}/aw-wrapper-dead", "exit 1", "export AW_NO_AUTOSTART=1")
        ext_cmd(dead)
        t0 = time.monotonic()
        s.chord("Control_L", "Shift_L", "Alt_L", "Up")
        ok = wait_for(lambda: snap(s)["viewers"] > 0 and snap(s)["active"] != a1["active"], 40, 0.2)
        took = time.monotonic() - t0
        time.sleep(3.0)
        s.shot(f"{s.base}/dead-message.png")
        a = snap(s)
        check("(G) the service cannot be started: Ctrl+Alt+Up still goes up and the viewer is there to say so (no popup, no notification)",
              bool(ok) and a["active"] == a["agent"] and {k: a[k] for k in quiet} == {k: a1[k] for k in quiet} and not newer(a, a1),
              f"{took:.1f}s {short(a)}")
        s.chord("Control_L", "Shift_L", "Alt_L", "Down")
        time.sleep(1.8)

        # ---- the fault goes away: the same Up now works again from where it was
        s.eval("""const e = Main.extensionManager.lookup('%s').stateObj; for (const w of e._viewerWindows()) w.delete(global.get_current_time()); return 1""" % UUID)
        time.sleep(1.5)
        ext_cmd(real)
        s.chord("Control_L", "Shift_L", "Alt_L", "Up")
        ok = wait_for(lambda: snap(s)["viewers"] > 0 and snap(s)["active"] != a1["active"] and (workspaces(base) or []), 40, 0.2)
        time.sleep(3.0)
        wl = workspaces(base) or []
        check("after the fault is gone Ctrl+Alt+Up works again: `agents` exists and runs", bool(ok) and len(wl) == 1 and wl[0]["state"] == "running",
              json.dumps([(w["name"], w["state"]) for w in wl]))
        s.chord("Control_L", "Shift_L", "Alt_L", "Down")
        time.sleep(1.8)

        check("no JS errors in the shell log", not s.shell_errors(), "; ".join(s.shell_errors()[:2]))
    finally:
        try:
            for w in workspaces(base) or []:
                call(base, "workspace.destroy", workspace=w["id"])
        except Exception:
            pass
        keep = os.environ.get("AW_SILENT_SHOTS")          # keep the screenshots for looking at them
        if keep:
            import glob
            import shutil
            os.makedirs(keep, exist_ok=True)
            for f in glob.glob(f"{s.base}/*.png"):
                shutil.copy(f, keep)
        s.stop()
        print("leftover processes carrying the private runtime dir:", s.mine() or "none")
    bad = [m for m, ok in RESULTS if not ok]
    print(f"{len(RESULTS) - len(bad)}/{len(RESULTS)} checks passed" + (f"; FAILED: {bad}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
