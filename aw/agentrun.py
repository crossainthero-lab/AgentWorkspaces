"""Run ONE agent invocation with the AgentWorkspaces MCP server attached, without installing anything.

`agent-workspaces integrate <agent> run [--model M] PROMPT` is the non-persistent counterpart of
`integrate <agent> install`: it uses each agent's own per-invocation mechanisms (command-line flags,
`-c` overrides, a throw-away config home) so the user's real agent configuration is never edited.

Safety properties (covered by tests/unit/test_agentrun.py):
  * the agent process gets an environment built from scratch: no DISPLAY, WAYLAND_DISPLAY, WAYLAND_SOCKET,
    XAUTHORITY or DBUS_SESSION_BUS_ADDRESS, a private empty XDG_RUNTIME_DIR (so a default `wayland-0` /
    `bus` lookup finds nothing) and BROWSER=/bin/true;
  * the agent's own shell tool is removed where the agent allows it, so graphical interaction can only go
    through the MCP tools;
  * credentials are never copied or printed: the agent finds its own login through the real HOME, exactly
    as it does when you start it yourself.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import tempfile
from typing import Dict, List, Optional, Tuple

from . import integrations as ig

PASS_THROUGH = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TZ")
HOST_DISPLAY_VARS = ("DISPLAY", "WAYLAND_DISPLAY", "WAYLAND_SOCKET", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS",
                     "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP", "GNOME_SETUP_DISPLAY", "SESSION_MANAGER")

# Cheapest suitable model per agent (names as accepted by that agent's own --model flag).
CHEAP_MODEL = {"claude-code": "haiku", "codex": "gpt-6-luna", "antigravity": "gemini-3.8-flash-low",
               "hermes": None}


class Plan:
    def __init__(self, argv: List[str], env: Dict[str, str], cwd: str, cleanup: List[str], notes: List[str]):
        self.argv, self.env, self.cwd, self.cleanup, self.notes = argv, env, cwd, cleanup, notes

    def close(self):
        for p in self.cleanup:
            shutil.rmtree(p, ignore_errors=True)


def clean_env(rt_dir: str, extra: Optional[Dict[str, str]] = None, host: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    host = os.environ if host is None else host
    env = {k: host[k] for k in PASS_THROUGH if k in host}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.setdefault("LANG", "C.UTF-8")
    env.update({"XDG_RUNTIME_DIR": rt_dir, "BROWSER": "/bin/true", "TERM": "dumb", "NO_COLOR": "1",
                "GIT_TERMINAL_PROMPT": "0"})
    for k, v in (extra or {}).items():
        if k in HOST_DISPLAY_VARS:
            raise ValueError(f"refusing to pass {k} to an agent")
        env[k] = v
    return env


def mcp_env(kind: str, base_dir: Optional[str], extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    # No AW_PROJECT: the MCP server names the workspace after the directory the agent started it in, exactly
    # as it does after `integrate install`.
    e = {"AW_AGENT_KIND": kind}
    if base_dir:
        e["AW_BASE_DIR"] = base_dir
    for k in ("AW_APP_DENY", "AW_VISION_URL", "AW_VISION_MODEL", "AW_VISION_KEY_ENV"):
        if os.environ.get(k):
            e[k] = os.environ[k]
    e.update(extra or {})
    return e


def plan(agent: str, prompt: str, model: Optional[str] = None, project: str = "demo", base_dir: Optional[str] = None,
         cwd: Optional[str] = None, max_turns: int = 12, extra_env: Optional[Dict[str, str]] = None,
         instructions: bool = True) -> Plan:
    kind = ig.AGENTS[agent]["kind"]
    tmp = tempfile.mkdtemp(prefix="aw-run-")
    rt = os.path.join(tmp, "rt")
    os.makedirs(rt, mode=0o700)
    cwd = cwd or os.path.join(tmp, project)
    os.makedirs(cwd, exist_ok=True)
    server = ig.server_command()
    menv = mcp_env(kind, base_dir, extra_env)
    env = clean_env(rt)
    model = model or CHEAP_MODEL.get(agent)
    notes: List[str] = []
    if agent == "claude-code":
        cfg = os.path.join(tmp, "mcp.json")
        with open(cfg, "w") as f:
            json.dump({"mcpServers": {ig.NAME: {"command": server[0], "args": server[1:], "env": menv}}}, f)
        argv = ["claude", "-p", prompt, "--mcp-config", cfg, "--strict-mcp-config", "--tools", "",
                "--allowedTools", f"mcp__{ig.NAME}", "--permission-mode", "dontAsk",
                "--setting-sources", "", "--disable-slash-commands", "--output-format", "stream-json", "--verbose",
                "--no-session-persistence"]
        if model:
            argv += ["--model", model]
        notes.append("built-in tools removed (--tools \"\"): only the MCP tools exist")
    elif agent == "codex":
        def toml(v):
            return json.dumps(v)
        argv = ["codex", "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config",
                "--ignore-rules", "--json", "-s", "read-only",
                "-c", 'approval_policy="never"',
                "-c", f"mcp_servers.{ig.NAME}.command={toml(server[0])}",
                "-c", f"mcp_servers.{ig.NAME}.args={toml(server[1:])}",
                "-c", f"mcp_servers.{ig.NAME}.env={{{', '.join(f'{k}={toml(v)}' for k, v in menv.items())}}}",
                "-c", f"mcp_servers.{ig.NAME}.default_tools_approval_mode=\"approve\"",
                *(["-c", f"developer_instructions={toml(ig.INSTRUCTIONS)}"] if instructions else []),
                "-C", cwd]
        if model:
            argv += ["-m", model, "-c", 'model_reasoning_effort="low"']
        argv.append(prompt)
    else:
        raise NotImplementedError(agent)
    return Plan(argv, env, cwd, [tmp], notes)


def run(args) -> int:
    import subprocess
    p = plan(args.agent, args.prompt, model=args.model, project=args.project or "demo",
             base_dir=os.environ.get("AW_BASE_DIR"), cwd=None if args.project else os.getcwd())
    try:
        print("+", " ".join(shlex.quote(a) for a in p.argv[:4]), "...", file=sys.stderr)
        return subprocess.run(p.argv, env=p.env, cwd=p.cwd, stdin=subprocess.DEVNULL).returncode
    finally:
        p.close()
