"""Copy selected acceptance runs into docs/evidence/agents/ in a sanitised form:
final workspace screenshot, short transcript (images shown as <image>, home paths as ~), reduced result JSON.

  python3 -m tests.acceptance.collect_evidence NAME=RUN_DIR_BASENAME [NAME=RUN_DIR_BASENAME ...]
"""
import json
import os
import shutil
import sys

from tests.acceptance import transcript

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "docs", "evidence", "agents")


HOME = os.path.expanduser("~")


def scrub(text):
    import re
    text = re.sub(re.escape(HOME) + r"/\.cache/claude-tmp/[A-Za-z0-9_\-]+", "<tmp>", text)
    text = re.sub(r"/run/user/\d+/awt-[A-Za-z0-9\-]+", "<tmp>", text)
    return text.replace(HOME, "~")


def mask_title_path(src, dst):
    """The editor's header shows the document's folder; white it out when that folder is a private path."""
    try:
        from PIL import Image, ImageDraw
        im = Image.open(src).convert("RGB")
        ImageDraw.Draw(im).rectangle([300, 30, 980, 44], fill=(255, 255, 255))
        im.save(dst)
    except ImportError:
        os.remove(dst)


def main(pairs):
    for pair in pairs:
        name, base = pair.split("=", 1)
        d = os.path.join(ROOT, ".scratch", "runs", base)
        r = json.load(open(os.path.join(d, "result.json")))
        dst = os.path.join(OUT, name)
        os.makedirs(dst, exist_ok=True)
        shutil.copyfile(os.path.join(d, "final.png"), os.path.join(dst, "final-screenshot.png"))
        if any("/" in (t or "") and "Text Editor" in (t or "") for t in (r.get("workspace_state") or {}).get("windows", [])):
            mask_title_path(os.path.join(d, "final.png"), os.path.join(dst, "final-screenshot.png"))
        lines = open(os.path.join(d, "agent.stdout"), errors="replace").read().splitlines()
        kind = "claude" if r["agent"] == "claude-code" else "codex"
        out, usage = (transcript.claude if kind == "claude" else transcript.codex)(lines)
        with open(os.path.join(dst, "transcript.txt"), "w") as f:
            f.write(f"prompt: {r['prompt']}\n\n" + "\n".join(out) + "\n\nUSAGE " + json.dumps(usage) + "\n")
        ws = r.get("workspace_state") or {}
        u = ws.get("usage") or {}
        red = {"agent": r["agent"], "model": r["model"], "prompt": r["prompt"], "exit_code": r["exit_code"],
               "seconds": r["seconds"], "workspace": r["workspace"], "run_alongside": r.get("parallel_with"),
               "workspace_viewers_attached": ws.get("viewers"), "workspace_windows": ws.get("windows"),
               "files_in_workspace_home": r.get("home_files"), "file_previews": r.get("home_previews"),
               "host_display_variables_seen_in_agent_process_group": r.get("agent_env_host_display_vars"),
               "host_wayland_sockets_before_after": [r.get("host_before"), r.get("host_after")],
               "agent_peak_pss_mib_incl_mcp_server": r.get("agent_peak_pss_mib_incl_mcp_server"),
               "agent_cpu_seconds": r.get("agent_cpu_seconds_children"),
               "workspace_memory_mib": round(u.get("memory_bytes", 0) / 2 ** 20) if u else None,
               "workspace_cpu_seconds": round(u.get("cpu_ns", 0) / 1e9, 1) if u else None, "usage": usage}
        with open(os.path.join(dst, "result.json"), "w") as f:
            f.write(scrub(json.dumps(red, indent=2, default=str)))
        tp = os.path.join(dst, "transcript.txt")
        with open(tp) as f:
            clean = scrub(f.read())
        with open(tp, "w") as f:
            f.write(clean)
        print("wrote", dst)


if __name__ == "__main__":
    main(sys.argv[1:])
