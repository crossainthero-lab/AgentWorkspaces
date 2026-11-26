"""Record one real agent working in its own workspace: workspace screenshots every ~0.7 s -> a short video.
Fully private environment (see privenv.py); nothing is shown on any real screen. Output: OUT_DIR/release-demo-*.mp4 + frames.

  python3 -m tests.acceptance.demo_recording OUT_DIR
"""
import glob
import os
import shutil
import subprocess
import sys
import time

from tests.acceptance import harness


def main(out):
    os.makedirs(out, exist_ok=True)
    common, cli = harness.start_daemon("demo")
    try:
        prompt = ("Open my browser and go to agdc.dev, then open a text editor and write one sentence about what the page's "
                  "headline says. Save it as headline.txt.")
        run_dir = os.path.join(common.BASE, "run")
        r = harness.run_agent("claude-code", prompt, common.BASE, "demo", run_dir, timeout=240, shots_every=0.7,
                              extra_env={"AW_APP_DENY": "agentdock,waydroid,steam"})
        frames = sorted(glob.glob(os.path.join(run_dir, "shot-*.png")))
        print("frames", len(frames), "seconds", r["seconds"], "exit", r["exit_code"], "windows", (r.get("workspace_state") or {}).get("windows"),
              "home files", harness.home_files(r["home"]) if r.get("home") and os.path.exists(r["home"]) else None)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        fdir = os.path.join(out, f"frames-{stamp}")
        os.makedirs(fdir, exist_ok=True)
        for i, f in enumerate(frames):
            shutil.copyfile(f, os.path.join(fdir, f"f{i:03d}.png"))
        shutil.copyfile(os.path.join(run_dir, "final.png"), os.path.join(out, f"final-{stamp}.png"))
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", "3", "-i", os.path.join(fdir, "f%03d.png"), "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-vf", "scale=1280:-2", os.path.join(out, f"release-demo-{stamp}.mp4")], check=False)
        print("wrote", out)
    finally:
        common.stop_daemon(cli)


if __name__ == "__main__":
    main(sys.argv[1])
