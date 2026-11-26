#!/usr/bin/python3 -I
"""The agent desktop as a real desktop, recorded and checked in an ISOLATED headless GNOME Shell.

What is shown and asserted (stock Ubuntu overview, or with Desktop Cube and the developer's other extensions with --cube):
  1. Super: the agent desktop is a thumbnail above the personal ones, the same look and size as GNOME's own thumbnails
  2. a click on it goes up to it; Ctrl+Shift+Alt+Up from workspace 2 goes up too (also with a window fullscreen)
  3. up there Ctrl+Alt+Left/Right/Up/Down, Super+Page Down and the cube keys do nothing visible; with a second agent
     workspace Left/Right move between the agent workspaces only
  4. Ctrl+Shift+Alt+Down returns to workspace 2 (and its window has the focus again); Super from the agent desktop and a
     click on one of your desktops leaves it cleanly

Usage: demo_real_desktop.py OUTDIR [--cube]
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(os.path.dirname(HERE))]
from aw.client import Client  # noqa: E402
from gnome_session import Session, REALISTIC_COPY, REALISTIC_SYSTEM, UUID  # noqa: E402
from live_env import Daemon, PROBE_APP, wait_for  # noqa: E402
from record import record, extract_frames  # noqa: E402

RES = {"checks": []}
UP = ("Control_L", "Shift_L", "Alt_L", "Up")
DOWN = ("Control_L", "Shift_L", "Alt_L", "Down")


def check(name, ok, detail=""):
    RES["checks"].append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f"   [{detail}]" if detail else ""), flush=True)


def ext(s, expr):
    return s.eval(f"const e = Main.extensionManager.lookup('{UUID}').stateObj; return {expr};")


def info(s):
    return s.eval(f"""const e = Main.extensionManager.lookup('{UUID}').stateObj;
      const v = e._viewerWindows().map(w => w.get_title());
      const sw = Main.wm._workspaceSwitcherPopup;
      return {{active: global.workspace_manager.get_active_workspace_index(), n: global.workspace_manager.n_workspaces,
              agent: e._agentWs ? e._agentWs.index() : -1, agentActive: e._agentActive(), sliding: !!e._slide, overview: Main.overview.visible,
              notes: Main.messageTray.getSources().map(x => x.title + ':' + (x.notifications?.length ?? 0)),
              popup: !!(sw && sw.visible), viewer: v, focus: global.display.focus_window?.get_title() ?? null,
              ctrl: [Main.overview._overview.controls.scale_x, Main.overview._overview.controls.translation_y],
              wg: [global.window_group.translation_y, global.window_group.rotation_angle_y]}};""")


def main():
    out = os.path.abspath(sys.argv[1])
    cube = "--cube" in sys.argv
    tag = "cube" if cube else "stock"
    os.makedirs(out, exist_ok=True)
    kw = dict(copy_extensions=REALISTIC_COPY, enable_extensions=REALISTIC_SYSTEM, dump_user_settings=True) if cube else {}
    s = Session(monitors=("1600x900",), pipewire=True, **kw)
    d = None
    try:
        s.start()
        time.sleep(2.0)
        s.eval("Main.overview.hide(); return 1")
        time.sleep(1.0)
        # ---- personal desktops: workspace 1 has an editor, workspace 2 a calculator and a probe window
        s.launch(["gnome-text-editor", "--new-window"], "ed.log")
        assert wait_for(lambda: s.state()["windows"], 25)
        time.sleep(1.5)
        s.launch(["gnome-calculator"], "calc.log")
        assert wait_for(lambda: len(s.state()["windows"]) >= 2, 25)
        time.sleep(1.0)
        s.eval("""for (const a of global.get_window_actors()) { const m = a.meta_window; const t = (m.get_title() ?? '') + (m.get_wm_class() ?? '');
            if (/alculator/.test(t)) { m.unmaximize?.(); m.move_resize_frame(true, 420, 140, 380, 520); m.change_workspace_by_index(1, false); } } return 1""")
        time.sleep(1.0)
        s.eval("global.workspace_manager.get_workspace_by_index(1).activate(global.get_current_time()); return 1")
        time.sleep(1.2)
        # ---- the service and one agent workspace (a real one, with a real app), through the real daemon
        d = Daemon(s).start()
        c = d.client()
        c.call("workspace.create", name="research", agent_kind="claude-code", width=1280, height=720, window_policy="floating")
        c.call("workspace.launch", workspace="research", argv=["gnome-calculator"])
        assert wait_for(lambda: c.call("window.list", workspace="research")["windows"], 25)
        time.sleep(1.5)
        # warm-up visit (starts the viewer process), not recorded
        s.chord(*UP)
        assert wait_for(lambda: info(s)["agentActive"], 30, 0.2)
        time.sleep(2.5)
        s.chord(*DOWN)
        time.sleep(1.5)
        i = info(s)
        check(f"[{tag}] back on workspace 2 (index 1) after the warm-up visit", i["active"] == 1 and not i["agentActive"], json.dumps(i))
        s.move(1585, 440)

        marks = {}
        vid = os.path.join(out, f"real-desktop-{tag}.mp4")
        with record(s, vid, fps=60) as rec:
            time.sleep(1.5)
            # ---- 1. Super: the agent desktop as one more thumbnail, first in GNOME's own row
            marks["super"] = time.monotonic() - rec._t0
            s.chord("Super_L")
            wait_for(lambda: info(s)["overview"], 5, 0.1)
            time.sleep(3.2)
            s.shot(os.path.join(out, f"1-overview-{tag}.png"))
            geo = s.eval("""const e = Main.extensionManager.lookup('%s').stateObj; const box = Main.overview._overview.controls._thumbnailsBox;
              const ts = box._thumbnails; const a = ts.find(t => t.metaWorkspace === e._agentWs);
              if (!a) return null;
              const R = t => ({pos: t.get_transformed_position(), size: t.get_transformed_size(), vis: t.visible, parent: t.get_parent() === a.get_parent(), cls: t.style_class,
                               radius: t.get_theme_node().get_border_radius(0)});
              return {n: ts.length, agentFirst: ts[0] === a, agentIndex: a.metaWorkspace.index(), rows: ts.map(R),
                      dnd: a.handleDragOverInternal(Main.xdndHandler, null, 0), accept: a.acceptDropInternal(Main.xdndHandler, null, 0),
                      personalDnd: ts.filter(t => t !== a).map(t => typeof t.handleDragOverInternal)};""" % UUID)
            rows = geo["rows"] if geo else []
            same = len(rows) >= 2 and all(r["vis"] and r["parent"] and r["cls"] == rows[0]["cls"] and r["radius"] == rows[0]["radius"] and
                                          abs(r["size"][0] - rows[0]["size"][0]) <= 1 and abs(r["size"][1] - rows[0]["size"][1]) <= 1 and
                                          abs(r["pos"][1] - rows[0]["pos"][1]) <= 1 for r in rows)
            check(f"[{tag}] the agent desktop is ONE MORE THUMBNAIL in GNOME's own row, in the FIRST position: same container, class, corner radius, size and height as "
                  f"its siblings, nothing of our own drawn", geo is not None and geo["agentFirst"] and geo["agentIndex"] == 0 and same, json.dumps(geo)[:400])
            gaps = [round(rows[i + 1]["pos"][0] - (rows[i]["pos"][0] + rows[i]["size"][0]), 1) for i in range(len(rows) - 1)]
            check(f"[{tag}] ... with the same spacing between it and the first personal thumbnail as between the personal ones", len(set(gaps)) == 1 or max(gaps) - min(gaps) <= 1.5, str(gaps))
            check(f"[{tag}] ... and a window dropped on it is refused (NO_DROP), while the personal ones take drops", geo is not None and geo["dnd"] == 0 and geo["accept"] is False
                  and all(t == "function" for t in geo["personalDnd"]), json.dumps([geo and geo["dnd"], geo and geo["accept"]]))
            s.chord("Escape")
            time.sleep(1.5)
            order = s.eval("""const e = Main.extensionManager.lookup('%s').stateObj; return [e._agentWs.index(), global.workspace_manager.n_workspaces];""" % UUID)
            check(f"[{tag}] ... and when the overview closes the agent workspace is back after your own desktops (not left in front)", order[0] == order[1] - 2 or order[0] == order[1] - 1, str(order))

            # ---- 2. click on the thumbnail selects that desktop (the big area shows it); activating it enters
            marks["click"] = time.monotonic() - rec._t0
            s.chord("Super_L")
            wait_for(lambda: info(s)["overview"], 5, 0.1)
            time.sleep(2.5)
            xy = s.eval("""const e = Main.extensionManager.lookup('%s').stateObj; const t = Main.overview._overview.controls._thumbnailsBox._thumbnails.find(x => x.metaWorkspace === e._agentWs);
                           const [x, y] = t.get_transformed_position(); return [x + t.get_transformed_size()[0] / 2, y + t.get_transformed_size()[1] / 2];""" % UUID)
            marks["clicked"] = time.monotonic() - rec._t0
            s.click(int(xy[0]), int(xy[1]))
            ok = wait_for(lambda: info(s)["agentActive"], 8, 0.1)
            time.sleep(1.8)
            s.shot(os.path.join(out, f"2a-agent-selected-in-overview-{tag}.png"))
            big = s.eval("""const e = Main.extensionManager.lookup('%s').stateObj; const c = Main.overview._overview.controls; let vis = null;
              for (const v of c._workspacesDisplay?._workspacesViews ?? []) for (const w of v._workspaces ?? []) if (w.metaWorkspace === e._agentWs) vis = [w.opacity, w.visible, w.mapped];
              const ind = c._thumbnailsBox._indicator; return {vis, ring: ind.visible, overview: Main.overview.visible};""" % UUID)
            check(f"[{tag}] a click on the agent thumbnail selects that desktop like any other (the overview stays open, GNOME's own ring on it, the big area shows the agent desktop)",
                  bool(ok) and big["overview"] and big["ring"] and big["vis"] is not None and big["vis"][0] == 255, json.dumps(big))
            marks["activated"] = time.monotonic() - rec._t0
            s.click(800, 520)                                   # the big preview of the agent desktop: activating it enters
            ok = wait_for(lambda: not info(s)["overview"], 8, 0.1)
            time.sleep(2.0)
            i = info(s)
            check(f"[{tag}] activating it (a click on the big preview) closes the overview on the agent desktop with the viewer focused",
                  bool(ok) and i["agentActive"] and i["focus"] is not None and i["focus"].startswith("AgentWorkspaces") and not i["sliding"], json.dumps(i))
            s.shot(os.path.join(out, f"2-agent-desktop-{tag}.png"))
            s.chord(*DOWN)
            ok = wait_for(lambda: not info(s)["agentActive"], 6, 0.1)
            time.sleep(1.8)
            i = info(s)
            check(f"[{tag}] ... and Ctrl+Shift+Alt+Down from there returns to workspace 2 (where the overview was opened)", bool(ok) and i["active"] == 1 and "alculator" in (i["focus"] or ""), json.dumps(i))
            s.chord(*UP)
            wait_for(lambda: info(s)["agentActive"], 6, 0.1)
            time.sleep(2.0)

            # ---- 3. the keys that must do nothing up here
            marks["keys"] = time.monotonic() - rec._t0
            before = info(s)
            for chord in (("Control_L", "Alt_L", "Left"), ("Control_L", "Alt_L", "Right"), ("Control_L", "Alt_L", "Up"),
                          ("Control_L", "Alt_L", "Down"), ("Super_L", "Next"), ("Super_L", "Prior"), ("Super_L", "Alt_L", "Left"),
                          ("Super_L", "Alt_L", "Right"), ("Control_L", "Shift_L", "Alt_L", "Left"), ("Control_L", "Shift_L", "Alt_L", "Right")):
                s.chord(*chord)
                time.sleep(0.5)
            time.sleep(1.2)
            after = info(s)
            s.shot(os.path.join(out, f"3-keys-ignored-{tag}.png"))
            check(f"[{tag}] up there Ctrl+Alt+Left/Right/Up/Down, Super+Page keys, Super+Alt+Left/Right and the move keys do nothing visible "
                  f"(same workspace, no switcher popup, no notification, nothing sliding, window group untouched)",
                  after["active"] == before["active"] and after["agentActive"] and not after["popup"] and after["notes"] == before["notes"] and
                  not after["sliding"] and after["wg"] == [0, 0] and after["viewer"] == before["viewer"], json.dumps(after))
            time.sleep(1.0)

            # ---- 4. Ctrl+Shift+Alt+Down: back to workspace 2, its window focused
            marks["down"] = time.monotonic() - rec._t0
            s.chord(*DOWN)
            ok = wait_for(lambda: not info(s)["agentActive"], 6, 0.1)
            time.sleep(1.8)
            i = info(s)
            check(f"[{tag}] Ctrl+Shift+Alt+Down returns to workspace 2 (the one we came from) and its window has the focus again",
                  bool(ok) and i["active"] == 1 and i["focus"] is not None and "alculator" in i["focus"], json.dumps(i))
            s.shot(os.path.join(out, f"4-back-on-workspace-2-{tag}.png"))
            time.sleep(0.8)

            # ---- 5. the shortcut from workspace 2, with a window fullscreen
            s.eval("""for (const a of global.get_window_actors()) { const m = a.meta_window;
                if (/alculator/.test((m.get_title() ?? '') + (m.get_wm_class() ?? ''))) m.make_fullscreen(); } return 1""")
            time.sleep(1.5)
            s.shot(os.path.join(out, f"5-fullscreen-before-{tag}.png"))
            marks["enter"] = time.monotonic() - rec._t0
            s.chord(*UP)
            ok = wait_for(lambda: info(s)["agentActive"], 6, 0.1)
            time.sleep(2.2)
            i = info(s)
            check(f"[{tag}] Ctrl+Shift+Alt+Up from workspace 2 with a window fullscreen goes up to the agent desktop", bool(ok), json.dumps(i))
            s.chord(*DOWN)
            time.sleep(1.8)
            s.eval("""for (const a of global.get_window_actors()) { const m = a.meta_window;
                if (/alculator/.test((m.get_title() ?? '') + (m.get_wm_class() ?? ''))) m.unmake_fullscreen(); } return 1""")
            time.sleep(1.0)
            check(f"[{tag}] ... and Down comes back to the fullscreen workspace", info(s)["active"] == 1 and not info(s)["agentActive"])

            # ---- 6. a second agent workspace: Left/Right between agent workspaces only
            c.call("workspace.create", name="web", agent_kind="codex", width=1280, height=720)
            lg = f"{s.base}/web.log"
            open(lg, "w").close()
            c.call("workspace.launch", workspace="web", argv=[sys.executable, PROBE_APP, "WEB", lg, "#2f855a"])
            wait_for(lambda: os.path.getsize(lg) > 0, 20)
            time.sleep(1.0)
            s.chord(*UP)
            wait_for(lambda: info(s)["agentActive"], 6, 0.1)
            time.sleep(2.0)
            t0 = info(s)["viewer"]
            s.chord("Control_L", "Alt_L", "Right")
            time.sleep(1.5)
            t1 = info(s)
            s.shot(os.path.join(out, f"6a-second-agent-workspace-{tag}.png"))
            s.chord("Control_L", "Alt_L", "Left")
            time.sleep(1.5)
            t2 = info(s)
            check(f"[{tag}] with two agent workspaces Ctrl+Alt+Right / Left move between them, and only between them (the personal workspace did not change)",
                  t1["viewer"] != t0 and t2["viewer"] == t0 and t1["agentActive"] and t2["agentActive"] and t1["active"] == t2["active"], json.dumps([t0, t1["viewer"], t2["viewer"]]))
            s.shot(os.path.join(out, f"6-two-agents-{tag}.png"))

            # ---- 7. Super from the agent desktop; pick one of your desktops: leaves cleanly
            marks["leave"] = time.monotonic() - rec._t0
            s.chord("Super_L")
            wait_for(lambda: info(s)["overview"], 5, 0.1)
            time.sleep(3.0)
            s.shot(os.path.join(out, f"7-overview-from-agent-{tag}.png"))
            geo = s.eval("""const e = Main.extensionManager.lookup('%s').stateObj; const c = Main.overview._overview.controls;
              const ts = (c._thumbnailsBox._thumbnails ?? []).filter(x => x.visible && x.metaWorkspace !== e._agentWs && x.metaWorkspace.index() === 1);
              if (!ts.length) return null; const [x, y] = ts[0].get_transformed_position(); return [x + ts[0].get_transformed_size()[0] / 2, y + ts[0].get_transformed_size()[1] / 2];""" % UUID)
            check(f"[{tag}] Super works from the agent desktop and GNOME's own thumbnails of your desktops are there to pick (the agent one first, ringed)", geo is not None, json.dumps(geo))
            if geo:
                s.click(int(geo[0]), int(geo[1]))
                ok = wait_for(lambda: info(s)["active"] == 1 and not info(s)["agentActive"], 6, 0.1)
                time.sleep(2.0)
                i = info(s)
                ring = s.eval("""const c = Main.overview._overview.controls; const ind = c._thumbnailsBox._indicator; return [ind.visible, global.workspace_manager.get_active_workspace_index()];""")
                s.shot(os.path.join(out, f"8-after-picking-workspace-1-{tag}.png"))
                check(f"[{tag}] picking workspace 1 in the overview leaves the agent desktop cleanly (now on workspace 1 like any desktop switch in the overview, "
                      f"nothing sliding or half done, window group untouched, GNOME's own ring on the picked thumbnail)",
                      bool(ok) and i["active"] == 1 and not i["agentActive"] and not i["sliding"] and i["wg"] == [0, 0] and ring[0], json.dumps([i, ring]))
                s.chord("Escape")
                time.sleep(1.5)
                i = info(s)
                check(f"[{tag}] ... and closing the overview leaves everything as it was (controls back to normal, panel and dock visible)",
                      not i["overview"] and i["ctrl"] == [1, 0] and s.eval("return [Main.panel.get_parent().opacity, Main.panel.get_parent().visible]") == [255, True], json.dumps(i))
                s.shot(os.path.join(out, f"9-closed-{tag}.png"))
            time.sleep(1.0)
        print("video:", json.dumps(rec.info))
        print("marks:", json.dumps(marks))
        off = 0.4

        def strip(name, t0, step, count, cols=4):
            extract_frames(vid, os.path.join(out, "frames", name), [round(t0 + step * i, 3) for i in range(count)])
            rows = -(-count // cols)
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-pattern_type", "glob", "-i", os.path.join(out, "frames", name, "frame-*.png"),
                            "-vf", f"scale=400:-1,tile={cols}x{rows}", "-frames:v", "1", os.path.join(out, f"strip-{name}-{tag}.png")], check=True)
        strip("click-enter", marks["activated"] + off, 0.08, 20, cols=5)
        strip("shortcut-enter", marks["enter"] + off - 0.02, 0.04, 20, cols=5)
        strip("down-back", marks["down"] + off - 0.02, 0.04, 20, cols=5)
        errs = s.shell_errors()
        check(f"[{tag}] no JS errors in the shell log", not errs, "; ".join(errs[:2]))
    finally:
        if d:
            d.stop()
        s.stop()
        print("leftover processes with the private runtime dir:", s.mine() or "none")
    bad = [n for n, ok in RES["checks"] if not ok]
    print(f"{len(RES['checks']) - len(bad)}/{len(RES['checks'])} checks passed", ("; FAILED: " + str(bad)) if bad else "")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
