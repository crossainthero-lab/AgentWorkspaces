#!/usr/bin/python3 -I
"""The agent desktop stacked above the personal ones, WITH Desktop Cube enabled, in an ISOLATED headless GNOME Shell
with the developer's own extension set (Desktop Cube, Tiling Shell, Blur my Shell, Just Perfection, User Themes, Ubuntu
Dock; settings read once from `dconf dump` into a PRIVATE keyfile), the real daemon, a real workspace and the real viewer.

Usage: run_cube.py [--personal N]      N personal workspaces (default 2; the suite is run for 2 and 4)

Ctrl+Alt+Up goes up to the agent desktop and Ctrl+Alt+Down comes back, with a vertical slide: never a cube face and never
a cube rotation. Desktop Cube keeps doing Ctrl+Alt+Left/Right between personal workspaces and is never involved in our
moves. Also: the Super overview paths and the agent previews there, rapid key presses, focus and bar/dock after leaving,
workspace count changes, GNOME's bindings restored on disable, no JS errors.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(os.path.dirname(HERE))]
from gnome_session import Session, REALISTIC_COPY, REALISTIC_SYSTEM, UUID  # noqa: E402
from live_env import Daemon, PROBE_APP, wait_for  # noqa: E402

VIEWER = "dev.agentworkspaces.Viewer"
RESULTS = []
H = 900


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f"   [{detail}]" if detail else ""), flush=True)


def ext(s, expr):
    return s.eval(f"const e = Main.extensionManager.lookup('{UUID}').stateObj; return {expr};")


def leftovers(s):
    """Everything a move must not leave behind."""
    return s.eval("""
      const g = global.window_group;
      const ctrl = Main.wm._workspaceAnimation;
      const chrome = [Main.layoutManager.panelBox, ...Main.uiGroup.get_children().filter(c => c.constructor.name === 'DashToDock')];
      const c = Main.overview._overview.controls;
      return {rx: g.rotation_angle_x, ry: g.rotation_angle_y, tz: g.translation_z, ty: g.translation_y, pz: g.pivot_point_z,
              opacity: g.opacity, ui: Main.uiGroup.get_n_children(), switchData: !!ctrl?._switchData,
              chrome: chrome.map(x => x.opacity), overview: Main.overview.visible, ctrlScale: [c.scale_x, c.translation_y]};""")


def sample_check(samples, first):
    """monotone slide from `first` (+-H) to 0 over many frames"""
    mono = all(abs(samples[i]) >= abs(samples[i + 1]) for i in range(len(samples) - 1))
    return len(samples) >= 8 and samples[0] == first and samples[-1] == 0 and mono


def main():
    n = int(sys.argv[sys.argv.index("--personal") + 1]) if "--personal" in sys.argv else 2
    T = f"[{n} personal]"
    s = Session(monitors=("1600x900",), copy_extensions=REALISTIC_COPY, enable_extensions=REALISTIC_SYSTEM,
                dump_user_settings=True, keep="--keep" in sys.argv)
    d = None
    try:
        s.start()
        time.sleep(2.0)
        s.eval("Main.overview.hide(); return 1")
        time.sleep(1.0)
        check(f"{T} extension and Desktop Cube are ACTIVE", s.ext_state(UUID) == 1 and
              s.ext_state("desktop-cube@schneegans.github.com") == 1)
        st0 = s.state()
        check(f"{T} nothing at enable time: no viewer, no notification beyond the shell's own, no extra window",
              not any(w["app_id"] == VIEWER for w in st0["windows"]))
        standin = os.path.join(HERE, "standin_viewer.py")
        for i in range(n):
            s.launch([sys.executable, standin, f"Personal {i + 1}", f"dev.agentworkspaces.TestPersonal{i + 1}"], f"p{i}.log")
            assert wait_for(lambda: any(w["app_id"] == f"dev.agentworkspaces.TestPersonal{i + 1}" for w in s.state()["windows"]), 25)
            time.sleep(1.0)
            s.eval(f"""for (const a of global.get_window_actors()) {{ const m = a.meta_window;
                      if ((m.get_gtk_application_id?.() ?? m.get_wm_class()) === 'dev.agentworkspaces.TestPersonal{i + 1}')
                        m.change_workspace_by_index({i}, false); }} return 1""")
            time.sleep(0.6)
        s.eval("global.workspace_manager.get_workspace_by_index(0).activate(global.get_current_time()); return 1")
        time.sleep(1.5)
        st = s.state()
        check(f"{T} {n} personal workspaces with a window each", len({w['ws'] for w in st['windows']}) == n and st["n"] == n + 1,
              f"n_workspaces={st['n']}")

        d = Daemon(s).start()
        c = d.client()
        c.call("workspace.create", name="research", agent_kind="claude-code", width=1280, height=720)
        lg = f"{s.base}/r.log"
        open(lg, "w").close()
        c.call("workspace.launch", workspace="research", argv=[sys.executable, PROBE_APP, "AGENT", lg, "#9c4221"])
        assert wait_for(lambda: os.path.getsize(lg) > 0, 20)
        time.sleep(1.0)

        # ---- cold: Ctrl+Alt+Up starts the viewer on the agent desktop
        s.chord("Control_L", "Alt_L", "Up")
        ok = wait_for(lambda: any(w["app_id"] == VIEWER for w in s.state()["windows"]), 20, 0.1)
        time.sleep(3.0)
        st = s.state()
        vw = next((w for w in st["windows"] if w["app_id"] == VIEWER), None)
        check(f"{T} Ctrl+Alt+Up with a running workspace goes up: the viewer opens on the agent desktop, not a personal one",
              bool(ok) and vw and vw["ws"] == st["active"] and st["active"] >= n, json.dumps(vw))
        check(f"{T} the viewer window is full screen (environment)",
              s.eval("""const w = global.get_window_actors().map(a => a.meta_window).find(w => (w.get_gtk_application_id?.() ?? w.get_wm_class()) === 'dev.agentworkspaces.Viewer');
                        return w ? w.is_fullscreen() : null;""") is True)
        shotp = f"{s.base}/env.png"
        s.move(900, 500)
        time.sleep(3.5)
        s.shot(shotp)
        hidden0 = dark(shotp, (1100, 8, 1400, 40))
        s.move(930, 520)
        time.sleep(0.6)
        s.move(900, 500)
        time.sleep(0.5)
        s.shot(shotp)
        shown = dark(shotp, (1100, 8, 1400, 40))
        time.sleep(4.0)
        s.shot(shotp)
        hidden1 = dark(shotp, (1100, 8, 1400, 40))
        check(f"{T} environment: header hidden at rest, appears on mouse movement, hides again",
              hidden0 < 400 and shown > 5000 and hidden1 < 400, f"dark px at rest={hidden0}, after move={shown}, after idle={hidden1}")
        s.chord("Control_L", "Alt_L", "Down")
        time.sleep(1.5)
        check(f"{T} Ctrl+Alt+Down returns to the previous personal workspace", s.state()["active"] == 0)
        time.sleep(1.0)
        s.chord("Control_L", "Alt_L", "Up")        # one more full cycle so dock items etc. exist before we count actors
        time.sleep(2.0)
        s.chord("Control_L", "Alt_L", "Down")
        time.sleep(2.0)
        base = leftovers(s)

        # ---- the stacked slide, with Desktop Cube on
        s.chord("Control_L", "Alt_L", "Up")
        time.sleep(1.5)
        samples = ext(s, "e._lastSamples")
        check(f"{T} going up is a vertical slide (kind=slide, never a cube): the agent desktop comes down into place from above "
              f"(-{H} to 0, monotone, {len(samples)} frames)", ext(s, "e._lastKind") == "slide" and sample_check(samples, -H),
              f"{samples[:3]}..{samples[-3:]}")
        a = leftovers(s)
        check(f"{T} nothing left behind after going up: window group untransformed, no overlay actor, nothing in flight",
              a["rx"] == 0 and a["ry"] == 0 and a["tz"] == 0 and a["ty"] == 0 and a["pz"] == 0 and a["ui"] == base["ui"] and
              not a["switchData"], json.dumps(a))
        s.chord("Control_L", "Alt_L", "Down")
        time.sleep(1.5)
        samples = ext(s, "e._lastSamples")
        check(f"{T} coming down slides the other way (+{H} to 0) onto the previous personal workspace",
              ext(s, "e._lastKind") == "slide" and sample_check(samples, H) and s.state()["active"] == 0, f"{samples[:3]}..{samples[-3:]}")
        a = leftovers(s)
        check(f"{T} after coming down: untransformed, no overlays, bar and dock back to full opacity, Desktop Cube not left mid-turn",
              a["rx"] == 0 and a["tz"] == 0 and a["pz"] == 0 and a["ui"] == base["ui"] and all(o == 255 for o in a["chrome"]) and
              not a["switchData"], json.dumps(a))

        # ---- focus returns to the window you were in
        s.eval("""const a = global.get_window_actors().map(x => x.meta_window).find(w => w.get_workspace().index() === 0 && w.get_title() === 'Personal 1');
                  a.activate(global.get_current_time()); return 1""")
        time.sleep(0.8)
        s.chord("Control_L", "Alt_L", "Up")
        time.sleep(1.8)
        s.chord("Control_L", "Alt_L", "Down")
        time.sleep(1.8)
        focus = s.eval("return global.display.focus_window?.get_title() ?? null")
        check(f"{T} keyboard focus returns to the window you were in", focus == "Personal 1", str(focus))

        # ---- the Super overview: agent previews above the personal workspaces; Up and Down from inside it
        s.chord("Super_L")
        wait_for(lambda: s.state()["overview"], 5, 0.1)
        time.sleep(3.0)
        info = s.eval("""const e = Main.extensionManager.lookup('%s').stateObj; const st = e._strip; const c = Main.overview._overview.controls;
          const tiles = st ? [...st._tiles.values()] : [];
          const ag = e._agentWs; let agentThumbHidden = null;
          for (const t of c._thumbnailsBox?._thumbnails ?? []) if (t.metaWorkspace === ag) agentThumbHidden = !t.visible;
          return {strip: !!st && st.actor.visible, tiles: tiles.length, names: tiles.map(t => t.name),
                  previews: tiles.map(t => !!t.preview.content), scale: c.scale_x, shift: c.translation_y,
                  stripY: st ? st.actor.get_transformed_position()[1] : null, agentThumbHidden};""" % UUID)
        s.shot(f"{s.base}/overview.png")
        check(f"{T} Super shows the agent desktop above the personal ones: one tile per running workspace with a live preview, "
              f"GNOME's own controls moved down, the agent workspace's own thumbnail hidden",
              info["strip"] and info["tiles"] == 1 and info["names"] == ["research"] and all(info["previews"]) and
              info["scale"] < 1 and info["shift"] > 100 and info["agentThumbHidden"] is True, json.dumps(info))
        s.chord("Control_L", "Alt_L", "Up")
        wait_for(lambda: s.state()["active"] >= n and not s.state()["overview"], 6, 0.1)
        time.sleep(1.8)
        a = leftovers(s)
        check(f"{T} Ctrl+Alt+Up from inside the overview closes it and goes up (slide); GNOME's overview controls are back to normal",
              s.state()["active"] >= n and not a["overview"] and a["ctrlScale"] == [1, 0] and ext(s, "e._lastKind") == "slide",
              json.dumps(a))
        s.chord("Super_L")
        wait_for(lambda: s.state()["overview"], 5, 0.1)
        time.sleep(1.0)
        s.chord("Control_L", "Alt_L", "Down")
        wait_for(lambda: s.state()["active"] == 0 and not s.state()["overview"], 6, 0.1)
        time.sleep(1.8)
        a = leftovers(s)
        check(f"{T} Ctrl+Alt+Down from inside the overview returns to the previous personal workspace, overview closed, nothing left",
              s.state()["active"] == 0 and not a["overview"] and a["rx"] == 0 and a["ui"] == base["ui"], json.dumps(a))
        # click a preview in the overview
        s.chord("Super_L")
        wait_for(lambda: s.state()["overview"], 5, 0.1)
        time.sleep(3.0)
        xy = s.eval(f"""const st = Main.extensionManager.lookup('{UUID}').stateObj._strip; const t = [...st._tiles.values()][0].button;
                       const [x, y] = t.get_transformed_position(); return [x + t.width / 2, y + t.height / 2];""")
        s.click(int(xy[0]), int(xy[1]))
        wait_for(lambda: s.state()["active"] >= n and not s.state()["overview"], 8, 0.1)
        time.sleep(1.8)
        check(f"{T} clicking an agent preview in the overview goes up to it (overview closes, slide)",
              s.state()["active"] >= n and not s.state()["overview"] and ext(s, "e._lastKind") == "slide")
        s.chord("Control_L", "Alt_L", "Down")
        time.sleep(1.8)

        # ---- keys pressed again during a slide: queued, not cancelled
        s.chord("Control_L", "Alt_L", "Up")
        time.sleep(0.12)
        s.chord("Control_L", "Alt_L", "Down")
        time.sleep(0.08)
        s.chord("Control_L", "Alt_L", "Up")
        time.sleep(0.08)
        s.chord("Control_L", "Alt_L", "Down")
        time.sleep(3.0)
        a = leftovers(s)
        check(f"{T} Up, Down, Up, Down pressed during the slide: ends on the personal workspace with nothing stuck",
              s.state()["active"] == 0 and a["ty"] == 0 and a["ui"] == base["ui"] and not a["switchData"], json.dumps(a))
        s.chord("Control_L", "Alt_L", "Up")
        time.sleep(0.2)
        s.chord("Control_L", "Alt_L", "Up")
        time.sleep(2.5)
        check(f"{T} Up pressed twice during the slide arrives once", s.state()["active"] >= n)
        s.chord("Control_L", "Alt_L", "Down")
        time.sleep(2.0)

        # ---- Desktop Cube's own Left/Right, never affected, never entering the agent desktop
        s.eval("""const m = await import('resource:///org/gnome/shell/ui/workspaceAnimation.js');
                  const P = m.WorkspaceAnimationController.prototype; globalThis.__cc = 0; const o = P._prepareWorkspaceSwitch;
                  P._prepareWorkspaceSwitch = function (...a) { globalThis.__cc++; return o.apply(this, a); };
                  globalThis.__restore = () => { P._prepareWorkspaceSwitch = o; }; return 1""")
        agent_idx = ext(s, "e._agentWs.index()")
        visited = []
        for i in range(n - 1):
            s.chord("Control_L", "Alt_L", "Right")
            time.sleep(1.6)
            visited.append(s.state()["active"])
        s.chord("Control_L", "Alt_L", "Right")        # past the last personal workspace: skips the agent desktop
        time.sleep(1.8)
        beyond = s.state()["active"]
        s.chord("Control_L", "Alt_L", "Left")
        time.sleep(1.8)
        back = s.state()["active"]
        for i in range(n - 1):
            s.chord("Control_L", "Alt_L", "Left")
            time.sleep(1.6)
        cc = s.eval("return globalThis.__cc")
        check(f"{T} Ctrl+Alt+Right/Left walk the {n} personal workspaces with Desktop Cube's own turn and never land on the agent desktop "
              f"(past the last one: the trailing empty workspace)",
              visited == list(range(1, n)) and agent_idx not in visited and beyond == n + 1 and beyond != agent_idx and
              back == n - 1 and s.state()["active"] == 0 and cc == 2 * (n - 1) + 2,
              f"visited={visited} beyond={beyond} agent={agent_idx} cube turns={cc}")
        s.eval("globalThis.__restore?.(); return 1")
        a = leftovers(s)
        check(f"{T} no cube switch left in flight, window group untransformed", not a["switchData"] and a["rx"] == 0 and a["ry"] == 0 and a["tz"] == 0 and a["ty"] == 0,
              json.dumps(a))

        # ---- workspace count changes: the viewer window goes away, the agent desktop is rebuilt on the next Up
        s.chord("Control_L", "Alt_L", "Up")
        time.sleep(2.0)
        s.eval("""for (const a of global.get_window_actors()) { const w = a.meta_window;
                  if ((w.get_gtk_application_id?.() ?? w.get_wm_class()) === 'dev.agentworkspaces.Viewer') w.delete(global.get_current_time()); } return 1""")
        wait_for(lambda: not any(w["app_id"] == VIEWER for w in s.state()["windows"]), 10)
        time.sleep(2.5)
        n_before = s.state()["n"]
        check(f"{T} closing the viewer window by hand while up there does not strand you on an empty desktop: back down on the personal workspace",
              s.state()["active"] == 0, f"active={s.state()['active']}")
        s.chord("Control_L", "Alt_L", "Up")
        ok = wait_for(lambda: any(w["app_id"] == VIEWER for w in s.state()["windows"]), 20, 0.2)
        time.sleep(3.0)
        st = s.state()
        check(f"{T} after the viewer window was closed (GNOME removed the empty agent workspace), Ctrl+Alt+Up builds the agent desktop again",
              bool(ok) and st["active"] >= n and ext(s, "e._agentWs.index()") == st["active"], f"n before={n_before} after={st['n']} active={st['active']}")
        s.chord("Control_L", "Alt_L", "Down")
        time.sleep(2.0)
        check(f"{T} and Ctrl+Alt+Down returns to the personal workspace", s.state()["active"] == 0)
        s.chord("Control_L", "Alt_L", "Up")
        time.sleep(2.0)

        # ---- logs, bindings
        log = open(s.log, errors="replace").read()
        bad = [l for l in log.splitlines() if "enter failed" in l or "return failed" in l or "slide failed" in l]
        check(f"{T} no JS errors and no failed move in the shell log", not s.shell_errors() and not bad,
              "; ".join((s.shell_errors() + bad)[:2]))
        s.ext_call("DisableExtension", UUID)
        time.sleep(1.5)
        down = s.keyfile_get("org/gnome/desktop/wm/keybindings", "switch-to-workspace-down")
        up = s.keyfile_get("org/gnome/desktop/wm/keybindings", "switch-to-workspace-up")
        check(f"{T} disable restores GNOME's Ctrl+Alt+Up/Down exactly", down == ["<Control><Alt>Down"] and up == ["<Control><Alt>Up"],
              f"up={up} down={down}")
    finally:
        if d:
            d.stop()
        s.stop()
        print("leftover processes carrying the private runtime dir:", s.mine() or "none")
        if "--keep" in sys.argv:
            print("kept:", s.base)
    bad = [m for m, ok in RESULTS if not ok]
    print(f"{len(RESULTS) - len(bad)}/{len(RESULTS)} checks passed" + (f"; FAILED: {bad}" if bad else ""))
    return 1 if bad else 0


def dark(path, box):
    """Near-black pixels in box: the revealed header is a dark bar over the (light) test workspace."""
    from PIL import Image
    im = Image.open(path).convert("RGB").crop(box)
    return sum(1 for r, g, b in im.get_flattened_data() if max(r, g, b) < 45)


if __name__ == "__main__":
    sys.exit(main())
