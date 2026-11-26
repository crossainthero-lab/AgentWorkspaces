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


def orange(path, box):
    """Pixels close to the agent marker's colour (#ff7a00) inside box=(x0, y0, x1, y1). The calculator's own orange '='
    key (#ed5b00) is deliberately outside the match."""
    from PIL import Image
    im = Image.open(path).convert("RGB").crop(box)
    return sum(1 for r, g, b in im.get_flattened_data() if r >= 250 and 108 <= g <= 138 and b <= 12)


def marker_bbox(path, box):
    """Centre of the arrow tip: the top-left-most extent of the marker colour inside box."""
    from PIL import Image
    im = Image.open(path).convert("RGB")
    x0, y0, x1, y1 = box
    pts = [(x + x0, y + y0) for y in range(y1 - y0) for x in range(x1 - x0)
           if (lambda p: p[0] >= 250 and 108 <= p[1] <= 138 and p[2] <= 12)(im.getpixel((x + x0, y + y0)))]
    if not pts:
        return None
    return (min(p[0] for p in pts), min(p[1] for p in pts))


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
    # this suite drives the viewer's ordinary window (title bar, sidebar); run_cube.py covers the full-screen environment
    s = Session(monitors=("1600x900",), pipewire=False,
                own_settings="[org/gnome/shell/extensions/agent-workspaces]\nenvironment-mode=false\n", **kw)
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
        check("GNOME's own Ctrl+Alt+Up/Down (switch workspace) and Left/Right are untouched; only its Ctrl+Shift+Alt+Up/Down (move window) are taken over",
              st["keys"]["switch-to-workspace-up"] == ["<Control><Alt>Up"] and st["keys"]["switch-to-workspace-down"] == ["<Control><Alt>Down"] and
              st["keys"]["move-to-workspace-up"] == [] and st["keys"]["move-to-workspace-down"] == [] and
              "<Control><Alt>Left" in st["keys"]["switch-to-workspace-left"] and
              "<Control><Alt>Right" in st["keys"]["switch-to-workspace-right"])

        conf = s.eval("return Main.extensionManager.lookup('%s').stateObj._settings.get_strv('conflicts')" % UUID)
        print("keybinding conflicts logged by the extension (realistic session):", json.dumps(conf))
        check("with the default Ctrl+Shift+Alt+Up/Down the only accelerator collision is GNOME's own move-window-up/down, taken over exactly and "
              "restored on disable; no other GNOME key, Desktop Cube, Tiling Shell, Just Perfection, Blur my Shell or Dash to Dock binding uses them",
              all("move-to-workspace-" in c and "taken over" in c for c in conf) and len(conf) <= 2, json.dumps(conf))

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

        # ---- enter: first through GNOME's Activities overview (Super), to prove the key works from inside it
        personal_ws = s.state()["active"]
        s.chord("Super_L")
        shown = wait_for(lambda: s.state()["overview"], 5, 0.1)
        check("Super opens GNOME's Activities overview (real key event)", bool(shown))
        time.sleep(1.2)
        s.chord("Control_L", "Shift_L", "Alt_L", "Up")
        ok = wait_for(lambda: any(w["app_id"] == VIEWER for w in s.state()["windows"]), 20, 0.1)
        check("Ctrl+Alt+Up from inside the overview: the daemon starts the real viewer on its own GNOME workspace", bool(ok))
        time.sleep(2.5)
        st = s.state()
        check("the overview was closed on the way (not left open on the agent workspace)", st["overview"] is False and
              st["active"] != personal_ws)
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
        d.agent_client(cur).call("input.click", workspace=cur, x=400, y=300)
        time.sleep(1.2)
        shot("viewer-activity.png")

        # ---- take control: agent is rejected, my keys reach the workspace
        s.click(*UI["you"])
        got = wait_for(lambda: c.call("workspace.get", workspace=cur)["input_mode"] == "manual", 5)
        check("'You' puts the workspace into manual takeover", bool(got))
        try:
            d.agent_client(cur).call("input.click", workspace=cur, x=100, y=100)
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
            d.agent_client(cur).call("input.click", workspace=cur, x=100, y=100)
            rej = False
        except ApiError as e:
            rej = e.code == "E_PAUSED"
        check("'Paused': agent rejected (E_PAUSED) and my keys are NOT forwarded (view only)", rej and n2 == n1, f"{n1}->{n2}")
        shot("viewer-paused.png")
        s.click(*UI["agent"])
        wait_for(lambda: c.call("workspace.get", workspace=cur)["input_mode"] == "agent", 5)

        # ---- approvals bar (agent asks the human)
        a = d.agent_client(cur).call("approval.request", workspace=cur, title="Use the shared browser profile?",
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

        # ---- sidebar collapses to an icon rail and back
        s.click(*UI["sidebar_toggle"])
        time.sleep(0.8)
        shot("viewer-collapsed.png")
        s.click(*UI["sidebar_toggle"])
        time.sleep(0.6)

        # ---- the approval bar: Deny and Approve do what they say
        s.click(1544, 104)
        got = wait_for(lambda: c.call("approval.get", id=a["id"])["status"] == "approved", 5)
        check("Approve in the viewer resolves the agent's request as approved", bool(got))
        a2 = d.agent_client(cur).call("approval.request", workspace=cur, title="Copy from the host clipboard?", kind="input")
        time.sleep(1.5)
        s.click(1455, 104)
        got = wait_for(lambda: c.call("approval.get", id=a2["id"])["status"] == "denied", 5)
        check("Deny in the viewer resolves it as denied", bool(got))
        time.sleep(1.0)
        check("the bar and the badge go away when nothing is pending",
              c.call("approval.list", pending=True)["approvals"] == [] and
              all(w["pending_approvals"] == 0 for w in c.call("workspace.list")["workspaces"]))

        # human-only grants (SA3's `grant.*`): an agent can only ask, the viewer is a human channel that can decide
        g = d.agent_client(cur).call("grant.request", workspace=cur, kind="browser_profile", resource="Work profile")
        time.sleep(1.8)
        shot("viewer-grant.png")
        check("grant.request from the workspace credential is pending until a person decides",
              c.call("grant.check", workspace=cur, kind="browser_profile", resource="Work profile")["granted"] is False)
        s.click(1500, 104)                       # 'Allow for 15 minutes'
        got = wait_for(lambda: c.call("grant.check", workspace=cur, kind="browser_profile", resource="Work profile")["granted"], 5)
        check("'Allow for 15 minutes' in the viewer grants it (viewer is accepted as a human channel)", bool(got))
        time.sleep(1.0)

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
        d.agent_client(cur).call("input.click", workspace=cur, x=640, y=360)
        time.sleep(0.5)
        n4 = len([e for e in log_events(logs[cur]) if e.get("ev") in ("button", "motion")])
        check("the agent can still drive the workspace with no viewer attached", n4 > n3)

        # ---- re-enter: one viewer again, remembers what I was looking at
        s.chord("Control_L", "Shift_L", "Alt_L", "Down")
        time.sleep(1.0)
        s.chord("Control_L", "Shift_L", "Alt_L", "Up")
        ok = wait_for(lambda: any(w["app_id"] == VIEWER for w in s.state()["windows"]), 20, 0.1)
        time.sleep(2.5)
        ws = {w["name"]: w for w in c.call("workspace.list")["workspaces"]}
        check("re-entering opens the viewer again on the workspace I used last",
              bool(ok) and ws["notes"]["viewers"] == 1, str({n: w["viewers"] for n, w in ws.items()}))

        # ---- stop / start / close from the menu (the viewer is on 'notes')
        s.click(*UI["menu"])
        time.sleep(0.6)
        s.click(1437, 207)                       # Stop workspace
        got = wait_for(lambda: next(w for w in c.call("workspace.list")["workspaces"] if w["name"] == "notes")["state"] == "stopped", 10)
        check("Stop workspace from the menu stops it", bool(got))
        time.sleep(1.2)
        shot("viewer-stopped.png")
        s.click(*UI["menu"])
        time.sleep(0.6)
        s.click(1437, 177)                       # Start workspace
        got = wait_for(lambda: next(w for w in c.call("workspace.list")["workspaces"] if w["name"] == "notes")["state"] == "running", 15)
        check("Start workspace from the menu starts it again", bool(got))
        time.sleep(2.0)
        s.click(*UI["menu"])
        time.sleep(0.6)
        s.click(1444, 237)                       # Close workspace...
        time.sleep(1.0)
        shot("viewer-close-dialog.png")
        s.key("Escape")
        time.sleep(0.6)
        check("Cancel keeps the workspace", any(w["name"] == "notes" for w in c.call("workspace.list")["workspaces"]))
        s.click(*UI["menu"])
        time.sleep(0.6)
        s.click(1444, 237)
        time.sleep(1.0)
        s.key("Tab")
        time.sleep(0.2)
        s.key("Return")                          # the dialog's 'Close workspace' button
        got = wait_for(lambda: not any(w["name"] == "notes" for w in c.call("workspace.list")["workspaces"]), 10)
        check("Close workspace (confirmed) destroys it", bool(got))
        time.sleep(1.5)

        # ---- Super + Ctrl+Alt+Down from the agent workspace: back to the previous personal workspace, overview closed
        s.chord("Super_L")
        wait_for(lambda: s.state()["overview"], 5, 0.1)
        time.sleep(1.0)
        s.chord("Control_L", "Shift_L", "Alt_L", "Down")
        wait_for(lambda: s.state()["overview"] is False and s.state()["active"] == personal_ws, 6, 0.1)
        time.sleep(1.2)
        st = s.state()
        check("Ctrl+Alt+Down from inside the overview returns to the previous personal workspace with the overview closed",
              st["active"] == personal_ws and st["overview"] is False, f"active={st['active']} overview={st['overview']}")
        s.chord("Control_L", "Shift_L", "Alt_L", "Up")
        time.sleep(2.0)
        check("Ctrl+Alt+Up from the normal desktop enters again", s.state()["active"] != personal_ws)

        # ---- the agent's world has its own taskbar and its own pointer marker
        c.call("workspace.create", name="desk", agent_kind="claude-code", width=1280, height=720, window_policy="floating")
        c.call("workspace.launch", workspace="desk", argv=["gnome-calculator"])
        assert wait_for(lambda: c.call("window.list", workspace="desk")["windows"], 25)
        time.sleep(1.5)
        c.call("workspace.launch", workspace="desk", argv=["gnome-text-editor"])
        assert wait_for(lambda: len(c.call("window.list", workspace="desk")["windows"]) >= 2, 25)
        time.sleep(1.5)
        calc = next(w for w in c.call("window.list", workspace="desk")["windows"] if "alculator" in w["title"])
        edit = next(w for w in c.call("window.list", workspace="desk")["windows"] if "Editor" in w["title"])
        c.call("window.set_geometry", workspace="desk", window=calc["id"], x=60, y=70, width=380, height=560, agent_kind="human")
        time.sleep(1.0)
        time.sleep(1.5)
        s.click(120, 206)                              # the 'desk' row (third in the sidebar)
        got = wait_for(lambda: c.call("workspace.get", workspace="desk")["viewers"] == 1, 8)
        time.sleep(2.0)
        check("the new workspace can be opened from its sidebar row", bool(got))
        ag = d.agent_client("desk")
        shot("viewer-agent-taskbar.png")
        p = os.path.join(media, "viewer-agent-taskbar.png")
        check("the agent taskbar shows its orange accent line", orange(p, (250, 850, 1600, 866)) > 800,
              f"{orange(p, (250, 850, 1600, 866))} orange px in the taskbar's top line")
        shell_titles = [w["title"] for w in s.state()["windows"]]
        check("your own desktop lists none of the agent workspace's windows (only the personal calculator and the viewer)",
              not any("Text Editor" in (t or "") for t in shell_titles) and sum("Calculator" in (t or "") for t in shell_titles) == 1,
              str(shell_titles))
        wl = c.call("window.list", workspace="desk")["windows"]
        check("the workspace's window list holds exactly its own two windows", sorted(w["title"].split(" ")[0] for w in wl) ==
              ["Calculator", "New"], str([w["title"] for w in wl]))
        # clicking a taskbar entry focuses that window (as the human, through the window API)
        s.click(627, 879)                              # the editor's entry
        got = wait_for(lambda: next(w for w in c.call("window.list", workspace="desk")["windows"] if w["id"] == edit["id"])["focused"], 5)
        check("clicking the editor's taskbar entry focuses it in the workspace", bool(got))
        s.click(512, 879)                              # the calculator's entry (the launcher is the first entry)
        got = wait_for(lambda: next(w for w in c.call("window.list", workspace="desk")["windows"] if w["id"] == calc["id"])["focused"], 5)
        check("clicking the calculator's taskbar entry focuses it", bool(got))
        # the agent focuses a window through the API: the taskbar follows (event driven)
        ag.call("window.action", workspace="desk", window=edit["id"], action="focus")
        time.sleep(1.2)
        s.shot(os.path.join(media, "_tb.png"))
        tb = os.path.join(media, "_tb.png")
        a, b = orange(tb, (572, 886, 790, 896)), orange(tb, (461, 886, 566, 896))
        check("agent focus via the API moves the taskbar highlight to the editor", a > 80 and b < 20, f"editor underline={a}, calculator={b}")
        ag.call("window.action", workspace="desk", window=calc["id"], action="focus")
        time.sleep(1.2)
        s.shot(tb)
        a, b = orange(tb, (572, 886, 790, 896)), orange(tb, (461, 886, 566, 896))
        check("and back to the calculator", b > 60 and a < 20, f"editor={a}, calculator underline={b}")
        os.unlink(tb)
        # the agent's pointer: a real position from the daemon, drawn by the viewer in orange
        px, py = 104, 481                              # the '7' key
        for i in range(1, 11):
            ag.call("input.move", workspace="desk", x=300 + (px - 300) * i / 10, y=300 + (py - 300) * i / 10)
            time.sleep(0.03)
        ag.call("input.click", workspace="desk", x=px, y=py)
        time.sleep(0.25)
        s.shot(os.path.join(media, "viewer-agent-cursor.png"))
        pc = os.path.join(media, "viewer-agent-cursor.png")
        n_marker = orange(pc, (250, 80, 1600, 850))
        check("the orange agent pointer is drawn in the viewer at the agent's real position", n_marker > 40, f"{n_marker} orange px")
        mid = marker_bbox(pc, (250, 80, 1600, 850))
        # where it should be: workspace (104,481) through the viewer's fit mapping
        ww, wh = 1352, 776                             # the picture area: window minus sidebar, header, taskbar, 2 px frame
        scale = min(ww / 1280, wh / 720)
        exp_x = 246 + (ww - 1280 * scale) / 2 + 104 * scale
        exp_y = 81 + (wh - 720 * scale) / 2 + 481 * scale
        check("... at the right place (within 18 px of the mapped workspace position)",
              mid and abs(mid[0] - exp_x) < 18 and abs(mid[1] - exp_y) < 18, f"found {mid}, expected about ({exp_x:.0f}, {exp_y:.0f})")
        r = c.call("screen.screenshot", workspace="desk", inline=True)
        import base64 as _b64
        wsp = os.path.join(media, "_ws.png")
        open(wsp, "wb").write(_b64.b64decode(r["base64"]))
        check("the marker is not in the agent's own screenshot of the workspace", orange(wsp, (0, 0, 1280, 720)) == 0,
              f"{orange(wsp, (0, 0, 1280, 720))} orange px")
        os.unlink(wsp)
        time.sleep(5.5)
        s.shot(pc + ".idle.png")
        n_idle = orange(pc + ".idle.png", (250, 80, 1600, 850))
        os.unlink(pc + ".idle.png")
        check("it fades away when the agent has not moved the pointer for a few seconds", n_idle == 0, f"{n_idle} orange px")
        ag.call("input.move", workspace="desk", x=500, y=400)
        time.sleep(0.3)
        s.click(*UI["paused"])
        time.sleep(0.8)
        s.shot(pc + ".paused.png")
        n_p = orange(pc + ".paused.png", (250, 80, 1600, 850))
        os.unlink(pc + ".paused.png")
        check("and is hidden as soon as the human pauses or takes over", n_p == 0, f"{n_p} orange px")
        s.click(*UI["agent"])
        time.sleep(0.6)

        # ---- Back to desktop button (extension D-Bus Return)
        origin = s.state()["active"]
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
            s.chord("Control_L", "Shift_L", "Alt_L", "Down")
            time.sleep(1.5)
            s.chord("Control_L", "Shift_L", "Alt_L", "Up")
            time.sleep(1.5)
            c_vertical = calls()
            s.chord("Control_L", "Shift_L", "Alt_L", "Down")
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
        mup = s.keyfile_get("org/gnome/desktop/wm/keybindings", "move-to-workspace-up")
        mdown = s.keyfile_get("org/gnome/desktop/wm/keybindings", "move-to-workspace-down")
        check("disable leaves GNOME's bindings exactly as they were (Ctrl+Alt+Up/Down and Ctrl+Shift+Alt+Up/Down)",
              down == ["<Control><Alt>Down"] and up == ["<Control><Alt>Up"] and mup == ["<Control><Shift><Alt>Up"] and mdown == ["<Control><Shift><Alt>Down"],
              f"up={up} down={down} move-up={mup} move-down={mdown}")
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
