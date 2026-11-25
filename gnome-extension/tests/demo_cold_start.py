#!/usr/bin/python3 -I
"""Record the cold start: nothing is running, you press Ctrl+Alt+Up.

ISOLATED headless GNOME Shell with the Desktop Cube extension and the developer's other extensions (private keyfile, private
buses). No service, no workspace: the extension starts the service and creates the default `agents` desktop, the viewer
opens, the slide runs on its first frame. Then a person opens two apps from the launcher in the agent taskbar (real clicks,
real typing through the isolated shell's virtual devices), a scripted AgentWorkspaces client (the same API an MCP agent uses,
with the workspace's own credential; NOT a model) moves its orange pointer and types, and Ctrl+Alt+Down comes back.

Usage: demo_cold_start.py OUT.mp4 [--frames DIR] [--strips DIR] [--launcher X,Y]
"""
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(os.path.dirname(HERE))]
from aw.client import ApiError, Client  # noqa: E402
from gnome_session import Session, REALISTIC_COPY, REALISTIC_SYSTEM, UUID  # noqa: E402
from live_env import PROBE_APP, ROOT, wait_for  # noqa: E402
from record import record, extract_frames  # noqa: E402

RES = {"checks": []}


def check(name, ok, detail=""):
    RES["checks"].append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f"   [{detail}]" if detail else ""), flush=True)


class Pointer(threading.Thread):
    """A scripted client (not a model): glides the agent pointer across the workspace in small real input.move steps,
    clicks into the editor and types two lines."""

    def __init__(self, ws):
        super().__init__(daemon=True)
        self.ws = ws
        self.done = False
        self.errors = []

    def run(self):
        c = Client(autostart=False, role="agent", workspace=self.ws)
        try:
            pts = [(300, 200), (700, 260), (900, 420), (640, 380)]
            x0, y0 = 640.0, 360.0
            for (x, y) in pts:
                for i in range(1, 21):
                    t = i / 20
                    t = t * t * (3 - 2 * t)
                    c.call("input.move", workspace=self.ws, x=x0 + (x - x0) * t, y=y0 + (y - y0) * t)
                    time.sleep(0.035)
                x0, y0 = x, y
                time.sleep(0.25)
            c.call("input.click", workspace=self.ws, x=640, y=380)
            time.sleep(0.4)
            for text in ("Notes from the agent desktop\n", "opened from the launcher by a person,\n", "written into by a scripted client.\n"):
                for word in text.split(" "):
                    c.call("input.type", workspace=self.ws, text=word + ("" if word.endswith("\n") else " "))
                    time.sleep(0.16)
        except (ApiError, OSError) as e:
            self.errors.append(str(e))
        finally:
            c.close()
            self.done = True


def main():
    out = os.path.abspath(sys.argv[1])
    frames_dir = sys.argv[sys.argv.index("--frames") + 1] if "--frames" in sys.argv else None
    strips = sys.argv[sys.argv.index("--strips") + 1] if "--strips" in sys.argv else None
    lx, ly = (int(v) for v in (sys.argv[sys.argv.index("--launcher") + 1] if "--launcher" in sys.argv else "114,880").split(","))
    s = Session(monitors=("1600x900",), copy_extensions=REALISTIC_COPY, enable_extensions=REALISTIC_SYSTEM, dump_user_settings=True, pipewire=True)
    base = os.path.join(s.rt, "agent-workspaces")
    os.environ["AW_BASE_DIR"] = base
    agent = None
    try:
        s.start()
        time.sleep(2.0)
        s.eval("Main.overview.hide(); return 1")
        time.sleep(1.0)
        s.eval(f"Main.extensionManager.lookup('{UUID}').stateObj._settings.set_string('daemon-command', {json.dumps(os.path.join(ROOT, 'bin', 'agent-workspaces'))}); return 1")
        # a personal desktop with ordinary windows
        s.launch(["gnome-text-editor", "--new-window"], "ed.log")
        assert wait_for(lambda: s.state()["windows"], 25)
        time.sleep(1.5)
        s.launch(["gnome-calculator"], "calc.log")
        assert wait_for(lambda: len(s.state()["windows"]) >= 2, 25)
        time.sleep(1.0)
        s.eval("""for (const a of global.get_window_actors()) { const m = a.meta_window; const t = (m.get_title() ?? '') + (m.get_wm_class() ?? '');
            if (/alculator/.test(t)) { m.unmaximize?.(); m.move_resize_frame(true, 1040, 120, 380, 520); }
            else if (/ditor/.test(t)) { m.unmaximize?.(); m.move_resize_frame(true, 90, 80, 900, 600); } } return 1""")
        time.sleep(1.0)
        s.move(1585, 440)
        check("nothing is running before the key press: no service, no workspace", not os.path.exists(f"{base}/api.sock"))

        marks = {}
        with record(s, out, fps=60) as rec:
            time.sleep(2.0)
            marks["up"] = time.monotonic() - rec._t0
            s.chord("Control_L", "Alt_L", "Up")
            ok = wait_for(lambda: s.state()["active"] != 0, 20, 0.05)
            check("Ctrl+Alt+Up with nothing running goes up to the agent desktop", bool(ok))
            time.sleep(3.5)                                    # the empty agent desktop: taskbar and launcher
            marks["empty"] = time.monotonic() - rec._t0
            time.sleep(1.0)
            marks["launcher"] = time.monotonic() - rec._t0
            s.click(lx, ly)                                    # the launcher in the agent taskbar
            time.sleep(1.2)
            marks["menu"] = time.monotonic() - rec._t0
            s.type_text("text editor")
            time.sleep(1.0)
            marks["search"] = time.monotonic() - rec._t0
            s.key("Return")
            wl = Client(autostart=False, role="admin").call("workspace.list")["workspaces"]
            ws = next(w for w in wl if w["name"] == "agents")
            adm = Client(autostart=False, role="admin")
            got = wait_for(lambda: adm.call("window.list", workspace=ws["id"])["windows"], 25, 0.2)
            check("an app opened from the launcher appears in the `agents` workspace", bool(got), json.dumps([w["title"] for w in got or []]))
            time.sleep(2.5)
            marks["app"] = time.monotonic() - rec._t0
            agent = Pointer("agents")
            agent.start()
            time.sleep(1.0)
            marks["agent"] = time.monotonic() - rec._t0
            wait_for(lambda: agent.done, 25, 0.2)
            check("the scripted client's pointer and typing ran without errors", agent.done and not agent.errors, "; ".join(agent.errors))
            time.sleep(1.5)
            marks["down"] = time.monotonic() - rec._t0
            s.chord("Control_L", "Alt_L", "Down")
            check("Ctrl+Alt+Down comes back", bool(wait_for(lambda: s.state()["active"] == 0, 6, 0.1)))
            time.sleep(1.8)
        print("video:", json.dumps(rec.info))
        print("marks (s after recording start; the video starts ~0.4 s earlier):", json.dumps(marks))
        RES["video"] = rec.info
        if strips:
            off = 0.4

            def strip(name, t0, step, count, cols=4):
                extract_frames(out, os.path.join(strips, name), [round(t0 + step * i, 3) for i in range(count)])
                rows = -(-count // cols)
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-pattern_type", "glob", "-i", os.path.join(strips, name, "frame-*.png"),
                                "-vf", f"scale=400:-1,tile={cols}x{rows}", "-frames:v", "1", os.path.join(strips, f"strip-{name}.png")], check=True)
            strip("cold-up", marks["up"] + off - 0.02, 0.08, 20, cols=5)
            for k in ("empty", "menu", "search", "app", "agent", "down"):
                extract_frames(out, os.path.join(strips, k), [round(marks[k] + off, 2)])
        errs = s.shell_errors()
        check("no JS errors in the shell log", not errs, "; ".join(errs[:2]))
        notes = s.eval("return Main.messageTray.getSources().map(x => x.title + ':' + (x.notifications?.length ?? 0))")
        check("no notification during the whole cold start", notes == [], json.dumps(notes))
        if frames_dir:
            dur = rec.info["duration"]
            extract_frames(out, frames_dir, [round(dur * i / 24, 2) for i in range(0, 24)])
    finally:
        try:
            for w in Client(autostart=False, role="admin").call("workspace.list")["workspaces"]:
                Client(autostart=False, role="admin").call("workspace.destroy", workspace=w["id"])
        except Exception:
            pass
        s.stop()
        print("leftover processes with the private runtime dir:", s.mine() or "none")
    bad = [n for n, ok in RES["checks"] if not ok]
    print(f"{len(RES['checks']) - len(bad)}/{len(RES['checks'])} checks passed", ("; FAILED: " + str(bad)) if bad else "")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
