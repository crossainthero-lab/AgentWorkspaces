#!/usr/bin/python3
"""Isolated-shell test of the REAL stack: real GNOME Shell 50 (headless, private buses/dirs), the AgentWorkspaces
extension, the real daemon, real workspaces (headless labwc) running a real GTK4 test app, the real viewer.
No stand-ins. Optionally with the Desktop Cube / Tiling Shell / Blur my Shell / Just Perfection / User Themes /
Ubuntu Dock extensions of the developer's own session (copied, settings read once and loaded into a PRIVATE
keyfile): `--realistic` (default); `--plain` runs with only this extension.

Pointer clicks are synthesised inside the private compositor and land on the viewer window like a person's would;
the checks look at the daemon's state and at the test app's own event log, not at pixels.

Usage: run_real.py [--plain] [--media DIR]     (media: where the screenshots go; default gnome-extension/media)
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(os.path.dirname(HERE))]
from aw.client import ApiError  # noqa: E402
from gnome_session import Session, REALISTIC_COPY, REALISTIC_SYSTEM, UUID  # noqa: E402
from live_env import Daemon, ScriptedAgent, PROBE_APP, ROOT, wait_for  # noqa: E402

CUBE = "desktop-cube@schneegans.github.com"
RESULTS = []
VIEWER = "dev.agentworkspaces.Viewer"

# Viewer controls at 1600x900 with the viewer maximized (positions measured from screenshots of this build)
UI = {"sidebar_toggle": (24, 54), "overview_toggle": (66, 54), "agent": (1197, 54), "paused": (1272, 54),
      "you": (1340, 54), "clipboard": (1393, 54), "menu": (1436, 54), "new_ws": (80, 842), "back": (90, 876),
      "row0": (120, 104), "row1": (120, 155), "row2": (120, 206)}


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f"   [{detail}]" if detail else ""), flush=True)


def log_events(path):
    out = []
    if os.path.exists(path):
        for line in open(path):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def main():
    plain = "--plain" in sys.argv
    media = sys.argv[sys.argv.index("--media") + 1] if "--media" in sys.argv else os.path.join(os.path.dirname(HERE), "media")
    os.makedirs(media, exist_ok=True)
    kw = {} if plain else dict(copy_extensions=REALISTIC_COPY, enable_extensions=REALISTIC_SYSTEM, dump_user_settings=True)
    s = Session(monitors=("1600x900",), pipewire=False, **kw)
    d = agent = None
    try:
        s.start()
        time.sleep(2.0)
        s.eval("Main.overview.hide(); return 1")
        time.sleep(1.0)
        shot = lambda name: s.shot(os.path.join(media, name))  # noqa: E731
        check("extension loads ACTIVE (state 1) in the isolated shell", s.ext_state(UUID) == 1)
        if not plain:
            check("Desktop Cube, Tiling Shell, Blur my Shell, Just Perfection, User Themes, Ubuntu Dock are ACTIVE too",
                  all(s.ext_state(u) == 1 for u in REALISTIC_COPY + REALISTIC_SYSTEM),
                  str({u.split('@')[0]: s.ext_state(u) for u in REALISTIC_COPY + REALISTIC_SYSTEM}))
        st = s.state()
        check("GNOME's Ctrl+Alt+Up/Down were removed while the extension is enabled; Left/Right untouched",
              st["keys"]["switch-to-workspace-up"] == [] and st["keys"]["switch-to-workspace-down"] == [] and
              "<Control><Alt>Left" in st["keys"]["switch-to-workspace-left"] and
              "<Control><Alt>Right" in st["keys"]["switch-to-workspace-right"])

        # ---- real daemon + two real workspaces, each with the GTK4 test app (its event log is the oracle)
        d = Daemon(s).start()
        c = d.client()
        logs = {}
        for name, kind, color in (("research", "claude-code", "#2b6cb0"), ("web", "codex", "#2f855a")):
            c.call("workspace.create", name=name, agent_kind=kind, width=1280, height=720)
            logs[name] = f"{s.base}/probe-{name}.log"
            open(logs[name], "w").close()
            c.call("workspace.launch", workspace=name, argv=[sys.executable, PROBE_APP, name.upper(), logs[name], color])
            assert wait_for(lambda: any(e.get("ev") == "ready" for e in log_events(logs[name])), 20), "probe not ready"
        time.sleep(1.5)
        s.launch(["gnome-calculator"], "calc.log")
        assert wait_for(lambda: s.state()["windows"], 25)
        time.sleep(1.5)

        # ---- enter
        s.chord("Control_L", "Alt_L", "Down")
        ok = wait_for(lambda: any(w["app_id"] == VIEWER for w in s.state()["windows"]), 20, 0.1)
        check("Ctrl+Alt+Down: the daemon starts the real viewer on its own GNOME workspace", bool(ok))
        time.sleep(2.5)
        st = s.state()
        vw = next(w for w in st["windows"] if w["app_id"] == VIEWER)
        check("viewer window is on the active (agent) workspace and maximized", vw["ws"] == st["active"] and vw["maximized"],
              json.dumps(vw))
        ws = {w["name"]: w for w in c.call("workspace.list")["workspaces"]}
        check("the viewer attached to exactly one workspace", sum(w["viewers"] for w in ws.values()) == 1,
              str({n: w["viewers"] for n, w in ws.items()}))
        sel = next(n for n, w in ws.items() if w["viewers"] == 1)
        other = "web" if sel == "research" else "research"
        shot("viewer-entered.png")

        # ---- overview and switching
        s.click(*UI["overview_toggle"])
        time.sleep(3.2)
        shot("viewer-overview.png")
        ws = {w["name"]: w for w in c.call("workspace.list")["workspaces"]}
        check("overview shows all workspaces; viewer stays attached", sum(w["viewers"] for w in ws.values()) == 1)
        s.click(*UI["row1"] if other == "web" else UI["row0"])        # sidebar row of the other workspace
        time.sleep(2.0)
        ws = {w["name"]: w for w in c.call("workspace.list")["workspaces"]}
        check("switching workspace moves the viewer: old one detached, new one attached",
              ws[other]["viewers"] == 1 and ws[sel]["viewers"] == 0, str({n: w["viewers"] for n, w in ws.items()}))
        check("the detached workspace keeps running", ws[sel]["state"] == "running")
        s.click(*UI["row0"] if sel == "research" else UI["row1"])
        time.sleep(2.0)
        ws = {w["name"]: w for w in c.call("workspace.list")["workspaces"]}
        check("switched back", ws[sel]["viewers"] == 1 and ws[other]["viewers"] == 0)
        cur = sel

        # ---- activity: an agent action shows up (in the sidebar row and header)
        c.call("input.click", workspace=cur, x=400, y=300)
        time.sleep(1.2)
        shot("viewer-activity.png")

        # ---- take control: agent is rejected, my keys reach the workspace
        s.click(*UI["you"])
        got = wait_for(lambda: c.call("workspace.get", workspace=cur)["input_mode"] == "manual", 5)
        check("'You' puts the workspace into manual takeover", bool(got))
        try:
            c.call("input.click", workspace=cur, x=100, y=100)
            check("agent input is rejected with E_PAUSED during takeover", False, "call succeeded")
        except ApiError as e:
            check("agent input is rejected with E_PAUSED during takeover", e.code == "E_PAUSED", e.code)
        n0 = len([e for e in log_events(logs[cur]) if e.get("ev") == "text"])
        s.type_text("hi")
        time.sleep(1.0)
        keys = [e for e in log_events(logs[cur]) if e.get("ev") == "text"]
        check("my keystrokes go through the viewer into the workspace's app (its entry now reads 'hi')",
              len(keys) > n0 and keys[-1]["text"].endswith("hi"), str([e.get("text") for e in keys[n0:]]))
        shot("viewer-takeover.png")
        # the agent client waits instead of fighting while I am in control
        agent = ScriptedAgent(cur, word_delay=0.1, line_pause=0.2)
        agent.start()
        time.sleep(2.0)
        t_before = agent.typed
        check("a scripted agent that hits E_PAUSED waits and types nothing", agent.paused_rejections > 0 and t_before == 0,
              f"rejections={agent.paused_rejections} typed={t_before}")
        s.click(*UI["agent"])
        got = wait_for(lambda: c.call("workspace.get", workspace=cur)["input_mode"] == "agent", 5)
        time.sleep(2.5)
        check("'Agent' releases control: the waiting agent resumes", bool(got) and agent.typed > 0, f"typed={agent.typed}")
        agent.stop_flag = True

        # ---- pause (view only)
        s.click(*UI["paused"])
        wait_for(lambda: c.call("workspace.get", workspace=cur)["input_mode"] == "manual", 5)
        n1 = len([e for e in log_events(logs[cur]) if e.get("ev") == "text"])
        s.type_text("zz")
        time.sleep(0.8)
        n2 = len([e for e in log_events(logs[cur]) if e.get("ev") == "text"])
        try:
            c.call("input.click", workspace=cur, x=100, y=100)
            rej = False
        except ApiError as e:
            rej = e.code == "E_PAUSED"
        check("'Paused': agent rejected (E_PAUSED) and my keys are NOT forwarded (view only)", rej and n2 == n1, f"{n1}->{n2}")
        shot("viewer-paused.png")
        s.click(*UI["agent"])
        wait_for(lambda: c.call("workspace.get", workspace=cur)["input_mode"] == "agent", 5)

        # ---- approvals bar (agent asks the human)
        a = c.call("approval.request", workspace=cur, title="Use the shared browser profile?",
                   detail="The agent wants to open a page that needs your signed-in session.", kind="input")
        time.sleep(1.8)
        shot("viewer-approval.png")
        ws = {w["name"]: w for w in c.call("workspace.list")["workspaces"]}
        check("a pending approval is counted in the workspace list", ws[cur]["pending_approvals"] == 1)
        s.eval("return 1")

        # ---- create from the dialog
        s.click(*UI["new_ws"])
        time.sleep(1.2)
        shot("viewer-create.png")
        s.type_text("notes")
        time.sleep(0.4)
        s.key("Return")
        got = wait_for(lambda: any(w["name"] == "notes" and w["state"] == "running" for w in c.call("workspace.list")["workspaces"]), 15)
        check("New workspace dialog creates a running workspace and the viewer selects it", bool(got))
        time.sleep(2.0)
        ws = {w["name"]: w for w in c.call("workspace.list")["workspaces"]}
        check("the new workspace is the one now attached", ws.get("notes", {}).get("viewers") == 1)
        shot("viewer-new-workspace.png")

        # ---- close workspace from the menu
        s.click(*UI["menu"])
        time.sleep(0.8)
        shot("viewer-menu.png")
        s.key("Escape")
        time.sleep(0.4)

        # ---- detach: closing the viewer window leaves everything running
        s.eval("""for (const a of global.get_window_actors()) { const w = a.meta_window;
                  if ((w.get_gtk_application_id?.() ?? w.get_wm_class()) === 'dev.agentworkspaces.Viewer') w.delete(global.get_current_time()); }
                  return 1""")
        gone = wait_for(lambda: not any(w["app_id"] == VIEWER for w in s.state()["windows"]), 10)
        time.sleep(1.0)
        ws = {w["name"]: w for w in c.call("workspace.list")["workspaces"]}
        check("closing the viewer detaches (viewers=0) and every workspace keeps running",
              bool(gone) and all(w["viewers"] == 0 and w["state"] == "running" for w in ws.values()),
              str({n: (w["viewers"], w["state"]) for n, w in ws.items()}))
        n3 = len([e for e in log_events(logs[cur]) if e.get("ev") in ("button", "motion")])
        c.call("input.click", workspace=cur, x=640, y=360)
        time.sleep(0.5)
        n4 = len([e for e in log_events(logs[cur]) if e.get("ev") in ("button", "motion")])
        check("the agent can still drive the workspace with no viewer attached", n4 > n3)

        # ---- re-enter: one viewer again, remembers what I was looking at
        s.chord("Control_L", "Alt_L", "Up")
        time.sleep(1.0)
        s.chord("Control_L", "Alt_L", "Down")
        ok = wait_for(lambda: any(w["app_id"] == VIEWER for w in s.state()["windows"]), 20, 0.1)
        time.sleep(2.5)
        ws = {w["name"]: w for w in c.call("workspace.list")["workspaces"]}
        check("re-entering opens the viewer again on the workspace I used last",
              bool(ok) and ws["notes"]["viewers"] == 1, str({n: w["viewers"] for n, w in ws.items()}))

        # ---- Back to desktop button (extension D-Bus Return)
        origin = s.state()["active"]
        shot("viewer-back-button.png")
        s.click(*UI["back"])
        time.sleep(1.6)
        check("the 'Back to desktop' button returns to the personal workspace", s.state()["active"] != origin,
              f"{origin} -> {s.state()['active']}")

        # ---- the Desktop Cube still works and was not left broken
        if not plain:
            # How Desktop Cube hooks in: it replaces WorkspaceAnimationController.prototype._prepareWorkspaceSwitch /
            # _finishWorkspaceSwitch (no keybindings of its own, no use of the vertical axis for key switches).
            # Count calls of the patched method: the cube must run for Left/Right, and must not be entered half-way
            # by the extension's own vertical slide.
            s.eval("""const m = await import('resource:///org/gnome/shell/ui/workspaceAnimation.js');
                      const P = m.WorkspaceAnimationController.prototype;
                      globalThis.__cubeCalls = 0; const o = P._prepareWorkspaceSwitch;
                      P._prepareWorkspaceSwitch = function (...a) { globalThis.__cubeCalls++; return o.apply(this, a); };
                      globalThis.__restoreCube = () => { P._prepareWorkspaceSwitch = o; };
                      return 1""")
            calls = lambda: s.eval("return globalThis.__cubeCalls")  # noqa: E731
            s.chord("Control_L", "Alt_L", "Up")
            time.sleep(1.5)
            s.chord("Control_L", "Alt_L", "Down")
            time.sleep(1.5)
            c_vertical = calls()
            s.chord("Control_L", "Alt_L", "Up")
            time.sleep(1.5)
            s.chord("Control_L", "Alt_L", "Right")
            time.sleep(1.8)
            c_right = calls()
            s.chord("Control_L", "Alt_L", "Left")
            time.sleep(1.8)
            check("Desktop Cube animates Ctrl+Alt+Left/Right and is not invoked by the extension's vertical enter/return",
                  c_vertical == 0 and c_right >= 1 and calls() >= 2, f"cube prepare calls: after enter+return={c_vertical}, "
                  f"after Right={c_right}, after Left={calls()}")
            s.eval("globalThis.__restoreCube?.(); return 1")
            info = s.eval(f"""
              const ctrl = Main.wm._workspaceAnimation; const wg = global.window_group;
              return {{switchData: !!ctrl?._switchData, wg: [wg.translation_y, wg.rotation_angle_y, wg.opacity],
                      n: global.workspace_manager.n_workspaces}};""")
            check("Desktop Cube: no switch left in flight, window group untransformed after enter/leave/cube turns",
                  not info["switchData"] and info["wg"] == [0, 0, 255], json.dumps(info))
        errs = s.shell_errors()
        check("no JS errors in the shell log", not errs, "; ".join(errs[:2]))

        # ---- disable restores GNOME's bindings exactly
        s.ext_call("DisableExtension", UUID)
        time.sleep(1.5)
        down = s.keyfile_get("org/gnome/desktop/wm/keybindings", "switch-to-workspace-down")
        up = s.keyfile_get("org/gnome/desktop/wm/keybindings", "switch-to-workspace-up")
        check("disable restores GNOME's Ctrl+Alt+Up/Down exactly", down == ["<Control><Alt>Down"] and up == ["<Control><Alt>Up"],
              f"up={up} down={down}")
    finally:
        if agent:
            agent.stop_flag = True
        if d:
            d.stop()
        s.stop()
        print("leftover processes carrying the private runtime dir:", s.mine() or "none")
    bad = [n for n, ok in RESULTS if not ok]
    print(f"{len(RESULTS) - len(bad)}/{len(RESULTS)} checks passed" + (f"; FAILED: {bad}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
