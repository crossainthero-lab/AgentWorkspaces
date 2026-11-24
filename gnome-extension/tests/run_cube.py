#!/usr/bin/python3 -I
"""The cube-native enter/leave transition, in an ISOLATED headless GNOME Shell with the developer's own extension set
(Desktop Cube, Tiling Shell, Blur my Shell, Just Perfection, User Themes, Ubuntu Dock; settings read once from
`dconf dump` into a PRIVATE keyfile), the real daemon, a real workspace and the real viewer.

Usage: run_cube.py [--personal N]      N personal workspaces (default 2; the suite is run for 2 and 4)

Checks the turn itself (the window group's rotation about the horizontal axis runs from +-90 degrees to 0 over many
frames, monotone), what is left afterwards (no overlay actors, window group untransformed, nothing in flight, the shell's
bar and dock back), the Super overview path, pressing the key again half way (queued, not cancelled), the cube's own
horizontal turns still working, GNOME's bindings restored on disable and no JS errors.
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


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f"   [{detail}]" if detail else ""), flush=True)


def ext(s, expr):
    return s.eval(f"const e = Main.extensionManager.lookup('{UUID}').stateObj; return {expr};")


def leftovers(s):
    """Everything a turn must not leave behind."""
    return s.eval("""
      const g = global.window_group;
      const ctrl = Main.wm._workspaceAnimation;
      const chrome = [Main.layoutManager.panelBox, ...Main.uiGroup.get_children().filter(c => c.constructor.name === 'DashToDock')];
      return {rx: g.rotation_angle_x, ry: g.rotation_angle_y, tz: g.translation_z, ty: g.translation_y, pz: g.pivot_point_z,
              opacity: g.opacity, ui: Main.uiGroup.get_n_children(), switchData: !!ctrl?._switchData,
              chrome: chrome.map(c => c.opacity), overview: Main.overview.visible};""")


def bright(path, box):
    """Near-black pixels in box: the revealed header is a dark bar over the (light) test workspace."""
    from PIL import Image
    im = Image.open(path).convert("RGB").crop(box)
    return sum(1 for r, g, b in im.get_flattened_data() if max(r, g, b) < 45)


def main():
    n = int(sys.argv[sys.argv.index("--personal") + 1]) if "--personal" in sys.argv else 2
    s = Session(monitors=("1600x900",), copy_extensions=REALISTIC_COPY, enable_extensions=REALISTIC_SYSTEM,
                dump_user_settings=True)
    d = None
    try:
        s.start()
        time.sleep(2.0)
        s.eval("Main.overview.hide(); return 1")
        time.sleep(1.0)
        check(f"[{n} personal] extension and Desktop Cube are ACTIVE", s.ext_state(UUID) == 1 and
              s.ext_state("desktop-cube@schneegans.github.com") == 1)
        check(f"[{n} personal] extension reports the cube is enabled (so the cube-native turn is used)",
              ext(s, "e._cubeEnabled()") is True)
        # N personal workspaces, each with its own window (stand-in GTK windows with distinct app ids)
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
        check(f"[{n} personal] {n} personal workspaces with a window each", len({w['ws'] for w in st['windows']}) == n and st["n"] == n + 1,
              str([(w['title'], w['ws']) for w in st['windows']]) + f" n_workspaces={st['n']}")

        d = Daemon(s).start()
        c = d.client()
        c.call("workspace.create", name="research", agent_kind="claude-code", width=1280, height=720)
        lg = f"{s.base}/r.log"
        open(lg, "w").close()
        c.call("workspace.launch", workspace="research", argv=[sys.executable, PROBE_APP, "AGENT", lg, "#9c4221"])
        assert wait_for(lambda: os.path.getsize(lg) > 0, 20)
        time.sleep(1.0)

        # ---- cold enter: viewer starts, then warm it
        s.chord("Control_L", "Alt_L", "Down")
        ok = wait_for(lambda: any(w["app_id"] == VIEWER for w in s.state()["windows"]), 20, 0.1)
        time.sleep(3.0)
        st = s.state()
        vw = next((w for w in st["windows"] if w["app_id"] == VIEWER), None)
        check(f"[{n} personal] first Ctrl+Alt+Down starts the viewer on the agent workspace, which is not a personal one",
              bool(ok) and vw and vw["ws"] == st["active"] and st["active"] >= n, json.dumps(vw))
        check(f"[{n} personal] the viewer window is full screen (environment, no window chrome)",
              s.eval("""const w = global.get_window_actors().map(a => a.meta_window).find(w => (w.get_gtk_application_id?.() ?? w.get_wm_class()) === 'dev.agentworkspaces.Viewer');
                        return w ? w.is_fullscreen() : null;""") is True)
        # environment chrome: hidden until the mouse moves, then it appears, then it goes away again
        shotp = f"{s.base}/env.png"
        s.move(900, 500)
        time.sleep(3.5)
        s.shot(shotp)
        hidden0 = bright(shotp, (1100, 8, 1400, 40))
        s.move(930, 520)
        time.sleep(0.6)
        s.move(900, 500)
        time.sleep(0.5)
        s.shot(shotp)
        shown = bright(shotp, (1100, 8, 1400, 40))
        time.sleep(4.0)
        s.shot(shotp)
        hidden1 = bright(shotp, (1100, 8, 1400, 40))
        check(f"[{n} personal] environment: header hidden at rest, appears on mouse movement, hides again",
              hidden0 < 400 and shown > 5000 and hidden1 < 400, f"dark px at rest={hidden0}, after move={shown}, after idle={hidden1}")
        s.chord("Control_L", "Alt_L", "Up")
        time.sleep(1.5)
        check(f"[{n} personal] Ctrl+Alt+Up returns to the previous personal workspace", s.state()["active"] == 0)
        time.sleep(1.0)
        s.chord("Control_L", "Alt_L", "Down")      # one more full cycle, so dock items etc. exist before we count actors
        time.sleep(2.0)
        s.chord("Control_L", "Alt_L", "Up")
        time.sleep(2.0)
        base = leftovers(s)

        # ---- the turn down
        s.chord("Control_L", "Alt_L", "Down")
        time.sleep(1.5)
        kind = ext(s, "e._lastKind")
        samples = ext(s, "e._lastSamples")
        mono = all(abs(samples[i]) >= abs(samples[i + 1]) for i in range(len(samples) - 1))
        check(f"[{n} personal] Ctrl+Alt+Down turns the cube (kind=cube) from 90 degrees to 0 over many frames, monotone",
              kind == "cube" and len(samples) >= 8 and abs(samples[0]) == 90 and samples[-1] == 0 and mono,
              f"{len(samples)} frames: {samples[:3]}..{samples[-3:]}")
        down_first = samples[0]
        a = leftovers(s)
        check(f"[{n} personal] nothing is left behind after the turn down: window group untransformed, no overlay actors, nothing in flight",
              a["rx"] == 0 and a["ry"] == 0 and a["tz"] == 0 and a["ty"] == 0 and a["pz"] == 0 and a["ui"] == base["ui"] and
              not a["switchData"], json.dumps(a))
        # ---- the turn up
        s.chord("Control_L", "Alt_L", "Up")
        time.sleep(1.5)
        samples = ext(s, "e._lastSamples")
        mono = all(abs(samples[i]) >= abs(samples[i + 1]) for i in range(len(samples) - 1))
        check(f"[{n} personal] Ctrl+Alt+Up turns the cube back the other way to the previous personal workspace",
              ext(s, "e._lastKind") == "cube" and len(samples) >= 8 and samples[0] == -down_first and samples[-1] == 0 and mono and
              s.state()["active"] == 0, f"{samples[:3]}..{samples[-3:]}")
        a = leftovers(s)
        check(f"[{n} personal] after the turn up: untransformed, no overlays, bar and dock back to full opacity",
              a["rx"] == 0 and a["tz"] == 0 and a["pz"] == 0 and a["ui"] == base["ui"] and all(o == 255 for o in a["chrome"]),
              json.dumps(a))

        # ---- through the Super overview
        s.chord("Super_L")
        wait_for(lambda: s.state()["overview"], 5, 0.1)
        time.sleep(1.0)
        s.chord("Control_L", "Alt_L", "Down")
        wait_for(lambda: s.state()["active"] >= n and not s.state()["overview"], 6, 0.1)
        time.sleep(1.8)
        check(f"[{n} personal] Ctrl+Alt+Down from the Super overview: overview closes, then the cube turns",
              s.state()["active"] >= n and not s.state()["overview"] and ext(s, "e._lastKind") == "cube")
        s.chord("Super_L")
        wait_for(lambda: s.state()["overview"], 5, 0.1)
        time.sleep(1.0)
        s.chord("Control_L", "Alt_L", "Up")
        wait_for(lambda: s.state()["active"] == 0 and not s.state()["overview"], 6, 0.1)
        time.sleep(1.8)
        a = leftovers(s)
        check(f"[{n} personal] Ctrl+Alt+Up from the overview returns to the previous personal workspace, overview closed, nothing left",
              s.state()["active"] == 0 and not a["overview"] and a["rx"] == 0 and a["ui"] == base["ui"], json.dumps(a))

        # ---- key pressed again half way: queued, not cancelled
        s.chord("Control_L", "Alt_L", "Down")
        time.sleep(0.18)
        s.chord("Control_L", "Alt_L", "Up")               # while the cube is still turning down
        time.sleep(0.1)
        s.chord("Control_L", "Alt_L", "Down")             # and again: only the last request counts
        time.sleep(0.1)
        s.chord("Control_L", "Alt_L", "Up")
        time.sleep(3.0)
        a = leftovers(s)
        check(f"[{n} personal] Down, Up, Down, Up pressed during the turn: ends on the personal workspace, nothing stuck",
              s.state()["active"] == 0 and a["rx"] == 0 and a["tz"] == 0 and a["ui"] == base["ui"] and not a["switchData"],
              json.dumps(a))
        s.chord("Control_L", "Alt_L", "Down")
        time.sleep(0.2)
        s.chord("Control_L", "Alt_L", "Down")             # repeated Down during the turn is harmless
        time.sleep(2.5)
        check(f"[{n} personal] Down pressed twice during the turn: on the agent workspace once", s.state()["active"] >= n)
        s.chord("Control_L", "Alt_L", "Up")
        time.sleep(2.0)

        # ---- the cube's own horizontal turns still work, and are the cube's (not ours)
        s.eval("""const m = await import('resource:///org/gnome/shell/ui/workspaceAnimation.js');
                  const P = m.WorkspaceAnimationController.prototype; globalThis.__cc = 0; const o = P._prepareWorkspaceSwitch;
                  P._prepareWorkspaceSwitch = function (...a) { globalThis.__cc++; return o.apply(this, a); };
                  globalThis.__restore = () => { P._prepareWorkspaceSwitch = o; }; return 1""")
        for i in range(n - 1):
            s.chord("Control_L", "Alt_L", "Right")
            time.sleep(1.6)
        at_last = s.state()["active"]
        for i in range(n - 1):
            s.chord("Control_L", "Alt_L", "Left")
            time.sleep(1.6)
        cc = s.eval("return globalThis.__cc")
        check(f"[{n} personal] Ctrl+Alt+Right/Left walk through all {n} personal workspaces with the cube's own turn",
              at_last == n - 1 and s.state()["active"] == 0 and cc == 2 * (n - 1), f"cube turns: {cc}, last={at_last}")
        s.eval("globalThis.__restore?.(); return 1")
        a = leftovers(s)
        check(f"[{n} personal] no cube switch left in flight, window group untransformed", not a["switchData"] and a["rx"] == 0 and
              a["ry"] == 0 and a["tz"] == 0, json.dumps(a))

        # ---- slide fallback when the setting is off
        ext(s, "e._settings.set_boolean('use-cube-transition', false)")
        time.sleep(0.5)
        s.chord("Control_L", "Alt_L", "Down")
        time.sleep(1.5)
        check(f"[{n} personal] with `use-cube-transition` off the vertical slide is used", ext(s, "e._lastKind") == "slide")
        s.chord("Control_L", "Alt_L", "Up")
        time.sleep(1.5)
        ext(s, "e._settings.set_boolean('use-cube-transition', true)")

        # ---- logs, bindings
        log = open(s.log, errors="replace").read()
        bad = [l for l in log.splitlines() if "enter failed" in l or "return failed" in l or "cube turn failed" in l]
        check(f"[{n} personal] no JS errors and no failed turn in the shell log", not s.shell_errors() and not bad,
              "; ".join((s.shell_errors() + bad)[:2]))
        names0 = s.eval("return Main.uiGroup.get_children().map(c => c.constructor.name + ':' + (c.style_class ?? ''))")
        s.ext_call("DisableExtension", UUID)
        time.sleep(1.5)
        names1 = s.eval("return Main.uiGroup.get_children().map(c => c.constructor.name + ':' + (c.style_class ?? ''))")
        from collections import Counter
        added = Counter(names1) - Counter(names0)   # a popup menu (and Blur my Shell's blur widget for it) is built lazily by the shell
        down = s.keyfile_get("org/gnome/desktop/wm/keybindings", "switch-to-workspace-down")
        up = s.keyfile_get("org/gnome/desktop/wm/keybindings", "switch-to-workspace-up")
        check(f"[{n} personal] disable restores GNOME's Ctrl+Alt+Up/Down exactly", down == ["<Control><Alt>Down"] and up == ["<Control><Alt>Up"],
              f"up={up} down={down}")
        a = leftovers(s)
        check(f"[{n} personal] after disable: nothing transformed, no actor of ours left (only shell popups appeared lazily)",
              a["rx"] == 0 and a["tz"] == 0 and all("popup" in k for k in added), json.dumps(dict(added)))
    finally:
        if d:
            d.stop()
        s.stop()
        print("leftover processes carrying the private runtime dir:", s.mine() or "none")
    bad = [m for m, ok in RESULTS if not ok]
    print(f"{len(RESULTS) - len(bad)}/{len(RESULTS)} checks passed" + (f"; FAILED: {bad}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
