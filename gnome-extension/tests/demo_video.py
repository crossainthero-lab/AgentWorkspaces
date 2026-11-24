#!/usr/bin/python3
"""Record the demo video: enter and leave AgentWorkspaces in an ISOLATED headless GNOME Shell that also runs the
Desktop Cube extension (and Tiling Shell, Blur my Shell, Just Perfection, User Themes, Ubuntu Dock: copies of the
extensions installed for the developer, their settings dumped read-only and loaded into a PRIVATE keyfile).

What you see: a personal desktop with ordinary windows (real GNOME apps) -> Ctrl+Alt+Right/Left (cube) ->
Ctrl+Alt+Down (real key events synthesised inside the private compositor) -> vertical slide into the AgentWorkspaces
viewer, where a *scripted* AgentWorkspaces client (not a model) types into a real text editor that lives in a real
workspace -> Ctrl+Alt+Up back -> a second visit shows the workspace kept working -> cube still works.

Usage: demo_video.py OUT.mp4 [--frames DIR] [--strips DIR]
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from gnome_session import Session, REALISTIC_COPY, REALISTIC_SYSTEM, UUID  # noqa: E402
from live_env import Daemon, DemoAgent, PROBE_APP, wait_for  # noqa: E402
from record import record, extract_frames  # noqa: E402

CUBE = "desktop-cube@schneegans.github.com"
RES = {"checks": []}


def check(name, ok, detail=""):
    RES["checks"].append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f"   [{detail}]" if detail else ""), flush=True)


def place(s, title_part, x, y, w, h, ws=None):
    s.eval(f"""
      for (const a of global.get_window_actors()) {{
        const m = a.meta_window;
        if ((m.get_title() ?? '').includes({json.dumps(title_part)}) || (m.get_wm_class() ?? '').includes({json.dumps(title_part)})) {{
          m.unmaximize?.(); m.move_resize_frame(true, {x}, {y}, {w}, {h});
          {f'm.change_workspace_by_index({ws}, false);' if ws is not None else ''}
        }}
      }}
      return 1;""")


def windows(s):
    return s.state()["windows"]


def main():
    out = os.path.abspath(sys.argv[1])
    frames_dir = sys.argv[sys.argv.index("--frames") + 1] if "--frames" in sys.argv else None
    strips = sys.argv[sys.argv.index("--strips") + 1] if "--strips" in sys.argv else None
    s = Session(monitors=("1600x900",), copy_extensions=REALISTIC_COPY, enable_extensions=REALISTIC_SYSTEM,
                dump_user_settings=True, pipewire=True)
    d = None
    agent = None
    try:
        s.start()
        time.sleep(2.0)
        s.eval("Main.overview.hide(); return 1")
        time.sleep(1.0)
        check("Desktop Cube, Tiling Shell and AgentWorkspaces are all ENABLED",
              all(s.ext_state(u) == 1 for u in (UUID, CUBE, "tilingshell@ferrarodomenico.com")))
        original = ["<Control><Alt>Down"]       # GNOME's value, as written by the harness (the extension already ran)
        check("GNOME's Ctrl+Alt+Down/Up were taken over while enabled", s.state()["keys"]["switch-to-workspace-down"] == [])

        # ---- a real daemon, a real workspace with a real app, a scripted client
        d = Daemon(s).start()
        c = d.client()
        c.call("workspace.create", name="research", agent_kind="claude-code", width=1280, height=720, window_policy="floating")
        c.call("workspace.launch", workspace="research", argv=["gnome-calculator"])
        assert wait_for(lambda: c.call("window.list", workspace="research")["windows"], 25), "calculator did not appear"
        time.sleep(1.5)
        c.call("workspace.launch", workspace="research", argv=["gnome-text-editor"])
        assert wait_for(lambda: len(c.call("window.list", workspace="research")["windows"]) >= 2, 25), "editor did not appear"
        time.sleep(1.5)
        calc = next(w for w in c.call("window.list", workspace="research")["windows"] if "alculator" in w["title"])
        c.call("window.set_geometry", workspace="research", window=calc["id"], x=60, y=70, width=380, height=560, agent_kind="human")
        time.sleep(1.0)

        # ---- the personal desktop: ordinary windows
        s.launch(["gnome-text-editor", "--new-window"], "ed.log")
        assert wait_for(lambda: windows(s), 25), "personal editor did not appear"
        time.sleep(1.5)
        s.launch(["gnome-calculator"], "calc.log")
        assert wait_for(lambda: len(windows(s)) >= 2, 25), "calculator did not appear"
        time.sleep(1.0)
        s.launch([sys.executable, PROBE_APP, "Second workspace", f"{s.base}/probe.log", "#4a5568"], "probe.log")
        assert wait_for(lambda: len(windows(s)) >= 3, 25), "probe did not appear"
        time.sleep(1.0)
        w = windows(s)
        ed = next(x for x in w if "Text Editor" in (x["title"] or "") or "editor" in (x["app_id"] or "").lower())
        place(s, ed["app_id"] or "editor", 90, 80, 900, 600)
        place(s, "alculator", 1040, 120, 380, 520)
        place(s, "probe", 400, 200, 700, 420, ws=1)
        time.sleep(1.0)
        s.eval("global.workspace_manager.get_workspace_by_index(0).activate(global.get_current_time()); return 1")
        time.sleep(1.2)
        st = s.state()
        check("personal desktop has ordinary windows on workspace 0 and 1",
              sum(1 for x in st["windows"] if x["ws"] == 0) >= 2 and any(x["ws"] == 1 for x in st["windows"]), json.dumps(
                  [(x["title"], x["ws"]) for x in st["windows"]]))

        # ---- warm-up visit (not recorded): the first Ctrl+Alt+Down starts the viewer process
        t0 = time.monotonic()
        s.chord("Control_L", "Alt_L", "Down")
        viewer_ready = wait_for(lambda: any(x["app_id"] == "dev.agentworkspaces.Viewer" for x in windows(s)), 30, 0.1)
        cold = time.monotonic() - t0
        check("first Ctrl+Alt+Down asks the daemon and the viewer window appears on the agent workspace",
              bool(viewer_ready) and s.state()["active"] == next(x["ws"] for x in windows(s) if x["app_id"] == "dev.agentworkspaces.Viewer"),
              f"{cold:.2f} s from key press to viewer window (cold start incl. python/GTK)")
        RES["cold_start_s"] = round(cold, 2)
        time.sleep(3.0)
        s.chord("Control_L", "Alt_L", "Up")
        time.sleep(1.2)
        check("Ctrl+Alt+Up returned to workspace 0", s.state()["active"] == 0)

        # ---- the recording
        marks = {}
        s.move(1585, 440)                                     # park your own pointer at the right edge, out of the way
        agent = DemoAgent("research")
        with record(s, out, fps=30) as rec:
            time.sleep(2.0)                                   # personal desktop, ordinary windows
            marks["cube"] = time.monotonic() - rec._t0
            s.chord("Control_L", "Alt_L", "Right")            # cube
            time.sleep(1.8)
            check("Ctrl+Alt+Right moved to workspace 1 (cube)", s.state()["active"] == 1)
            s.chord("Control_L", "Alt_L", "Left")
            time.sleep(1.8)
            check("Ctrl+Alt+Left moved back to workspace 0 (cube)", s.state()["active"] == 0)
            agent.start()
            time.sleep(0.8)
            marks["super"] = time.monotonic() - rec._t0
            s.chord("Super_L")                                # GNOME's Activities overview animates in
            check("Super opened the Activities overview", bool(wait_for(lambda: s.state()["overview"], 5, 0.1)))
            time.sleep(2.2)
            marks["down"] = time.monotonic() - rec._t0
            s.chord("Control_L", "Alt_L", "Down")             # from inside the overview to the agent workspace
            check("Ctrl+Alt+Down from the overview reached the viewer with the overview closed",
                  bool(wait_for(lambda: s.state()["active"] != 0 and not s.state()["overview"], 5, 0.1)))
            time.sleep(9.5)                                   # the user watches the agent work
            n_in = (agent.clicks, agent.focus_calls)
            marks["up"] = time.monotonic() - rec._t0
            s.chord("Control_L", "Alt_L", "Up")               # back
            time.sleep(2.5)
            check("Ctrl+Alt+Up returned to workspace 0 with the overview closed", s.state()["active"] == 0 and not s.state()["overview"])
            time.sleep(2.5)                                   # the agent keeps working while not visible
            marks["down2"] = time.monotonic() - rec._t0
            s.chord("Control_L", "Alt_L", "Down")             # second visit, from the normal desktop
            time.sleep(6.5)
            check("the agent kept working while the workspace was not visible", agent.clicks > n_in[0],
                  f"clicks {n_in[0]} -> {agent.clicks}, focus calls {n_in[1]} -> {agent.focus_calls}")
            s.chord("Control_L", "Alt_L", "Up")
            time.sleep(2.0)
            marks["cube2"] = time.monotonic() - rec._t0
            s.chord("Control_L", "Alt_L", "Right")            # the cube still works afterwards
            time.sleep(1.8)
            s.chord("Control_L", "Alt_L", "Left")
            time.sleep(2.0)
        print("video:", json.dumps(rec.info))
        RES["video"] = rec.info
        print("marks (s after recording start; the video starts ~0.4 s earlier):", json.dumps(marks))
        if strips:
            def strip(name, t0, step, count, cols=4):
                extract_frames(out, os.path.join(strips, name), [round(t0 + step * i, 3) for i in range(count)])
                rows = -(-count // cols)
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-pattern_type", "glob", "-i", os.path.join(strips, name, "frame-*.png"),
                                "-vf", f"scale=400:-1,tile={cols}x{rows}", "-frames:v", "1", os.path.join(strips, f"strip-{name}.png")], check=True)
            off = 0.4                                        # the video starts ~0.4 s before the marks
            strip("cube", marks["cube"] + off, 0.06, 8)
            strip("super", marks["super"] + off, 0.1, 8)
            strip("down", marks["down"] + off - 0.05, 1 / 30, 20, cols=5)
            strip("glide", marks["down"] + off + 2.0, 0.12, 8)
            strip("up", marks["up"] + off - 0.05, 1 / 30, 20, cols=5)
        # ---- cube state is intact
        info = s.eval(f"""
          const ext = Main.extensionManager.lookup('{CUBE}');
          const sky = ext?.stateObj?._skybox;
          const ctrl = Main.wm._workspaceAnimation;
          const wg = global.window_group;
          return {{skyboxParentIsStage: sky ? sky.get_parent() === global.stage : null, switchData: !!ctrl?._switchData,
                  wg: [wg.translation_y, wg.rotation_angle_y, wg.opacity], cubeState: ext?.state}};""")
        check("cube not left broken: skybox attached to the stage, no switch in flight, window group untransformed",
              info["skyboxParentIsStage"] in (True, None) and not info["switchData"] and info["wg"] == [0, 0, 255], json.dumps(info))
        errs = s.shell_errors()
        check("no JS errors in the shell log", not errs, "; ".join(errs[:2]))
        agent.stop_flag = True
        if frames_dir:
            dur = rec.info["duration"]
            extract_frames(out, frames_dir, [round(dur * i / 24, 2) for i in range(0, 24)])
        # ---- disable restores the bindings exactly
        s.ext_call("DisableExtension", UUID)
        time.sleep(1.5)
        down = s.keyfile_get("org/gnome/desktop/wm/keybindings", "switch-to-workspace-down")
        up = s.keyfile_get("org/gnome/desktop/wm/keybindings", "switch-to-workspace-up")
        check("disable restored GNOME's Ctrl+Alt+Up/Down exactly", down == original and up == ["<Control><Alt>Up"], f"up={up} down={down}")
    finally:
        if agent:
            agent.stop_flag = True
        if d:
            d.stop()
        s.stop()
        print("leftover processes with the private runtime dir:", s.mine() or "none")
    bad = [n for n, ok in RES["checks"] if not ok]
    print(f"{len(RES['checks']) - len(bad)}/{len(RES['checks'])} checks passed", ("; FAILED: " + str(bad)) if bad else "")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
