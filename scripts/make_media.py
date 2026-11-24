#!/usr/bin/env python3
"""Produce the real screenshots and a short recording in docs/media.

Everything is captured from real workspaces / the HostSim stand-in desktop; nothing is mocked up. The apps
run with a private HOME, so no personal data can appear. Needs gnome-text-editor, gnome-calculator,
google-chrome (optional) and ffmpeg (optional, for the recording).
"""
import base64
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests"), os.path.join(ROOT, "tests", "integration")]
import common as K  # noqa: E402
from harness import HostSim  # noqa: E402

OUT = os.path.join(ROOT, "docs", "media")


def save(c, ws, name, **kw):
    r = c.call("screen.screenshot", workspace=ws, inline=True, **kw)
    with open(os.path.join(OUT, name), "wb") as f:
        f.write(base64.b64decode(r["base64"]))
    print("wrote", name, r["width"], r["height"])


def main():
    import signal
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(1))
    os.makedirs(OUT, exist_ok=True)
    c = K.start_daemon("media")
    host = None
    try:
        c.call("workspace.create", name="research", agent_kind="claude-code", width=1280, height=720, window_policy="floating")
        c.call("workspace.create", name="web", agent_kind="codex", width=1280, height=720)
        c.call("workspace.launch", workspace="research", argv=["gnome-calculator"])
        K.wait_for(lambda: c.call("window.list", workspace="research")["windows"], 20)
        time.sleep(2)
        c.call("workspace.launch", workspace="research", argv=["gnome-text-editor"])
        K.wait_for(lambda: len(c.call("window.list", workspace="research")["windows"]) >= 2, 20)
        time.sleep(2)
        wins = c.call("window.list", workspace="research")["windows"]
        ed = [w for w in wins if "editor" in w["app_id"].lower()][0]
        c.call("window.action", workspace="research", window=ed["id"], action="focus")
        time.sleep(0.5)
        c.call("input.click", workspace="research", x=300, y=300)
        c.call("input.type", workspace="research", text="Notes written by an agent inside its own AgentWorkspace.\n"
               "The personal desktop never saw this window, this pointer or these keystrokes.\n")
        time.sleep(1)
        save(c, "research", "workspace-research.png")
        if shutil.which("google-chrome"):
            page = ("data:text/html,<body style='margin:0;background:%231a202c;color:white;font-family:sans-serif'>"
                    "<h1 style='font-size:56px;margin:60px'>A different workspace</h1>"
                    "<p style='font-size:28px;margin:60px'>Own pointer, own focus, own clipboard.</p>")
            c.call("workspace.launch", workspace="web", argv=["google-chrome", "--ozone-platform=wayland", "--no-first-run",
                   "--no-default-browser-check", "--password-store=basic", "--simulate-outdated-no-au=Tue, 31 Dec 2099 23:59:59 GMT",
                   "--app=" + page])
            K.wait_for(lambda: c.call("window.list", workspace="web")["windows"], 25)
            time.sleep(3)
            save(c, "web", "workspace-web.png")
        # the viewer window, shown in a stand-in desktop (never the real one)
        host = HostSim(K.BASE, size=(1600, 900))
        host.spawn([sys.executable, "-m", "aw.viewer", "research", "--takeover"], {"AW_BASE_DIR": K.BASE, "PYTHONPATH": K.ROOT})
        K.wait_for(lambda: c.call("workspace.get", workspace="research")["viewers"] == 1, 15)
        time.sleep(2.5)
        with open(os.path.join(OUT, "viewer-takeover.png"), "wb") as f:
            f.write(host.shot())
        print("wrote viewer-takeover.png")
        host.close(); host = None
        c.call("workspace.resume", workspace="research")
        # recording: capture frames while the agent drives the calculator
        if shutil.which("ffmpeg"):
            frames = os.path.join(K.BASE, "frames")
            os.makedirs(frames, exist_ok=True)
            wins = c.call("window.list", workspace="research")["windows"]
            calc = [w for w in wins if "calc" in (w["app_id"] + w["title"]).lower()]
            calc_pos = None
            if calc:
                c.call("window.action", workspace="research", window=calc[0]["id"], action="focus")
            n = 0

            def grab(k=1):
                nonlocal n
                for _ in range(k):
                    r = c.call("screen.screenshot", workspace="research", inline=True, max_width=960)
                    with open(os.path.join(frames, f"f{n:04d}.png"), "wb") as f:
                        f.write(base64.b64decode(r["base64"]))
                    n += 1
                    time.sleep(0.08)
            grab(6)
            for key in ("1", "2", "plus", "3", "4", "Return"):
                c.call("input.key", workspace="research", combo=key)
                grab(4)
            c.call("input.type", workspace="research", text="7*6\n")
            grab(8)
            out = os.path.join(OUT, "agent-driving-calculator.webm")
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", "8", "-i", os.path.join(frames, "f%04d.png"),
                            "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "36", out], check=False)
            if os.path.exists(out):
                print("wrote", os.path.basename(out), os.path.getsize(out), "bytes,", n, "frames")
    finally:
        if host:
            host.close()
        K.stop_daemon(c)


if __name__ == "__main__":
    main()
