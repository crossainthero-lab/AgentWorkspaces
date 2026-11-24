"""Opt-in, reversible per-agent integration.

Each agent is registered through ITS OWN official `mcp add` / `mcp remove` command, never by editing its
configuration files by hand, and never globally: one agent at a time, only when asked.

  agent-workspaces integrate claude-code install --workspace research
  agent-workspaces integrate claude-code status
  agent-workspaces integrate claude-code uninstall

Nothing here touches screenshot tools, DISPLAY variables or any other computer-use integration.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import List, Optional

NAME = "agent-workspaces"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

INSTRUCTIONS = """\
## Graphical computer use: AgentWorkspaces

When you need to look at or operate a graphical application, use the `agent-workspaces` MCP tools
(list_workspaces, screenshot, mouse_click, keyboard_type, launch_app, ...). They act on an AgentWorkspace
that the user assigned to you: a separate headless desktop. They never touch the user's personal desktop,
mouse, keyboard focus or windows.

- Graphical interaction targets your ASSIGNED AgentWorkspace, never the personal desktop.
- Do not use xdotool, ydotool, wtype, grim, gnome-screenshot or any other computer-use tool against the host
  session, and do not change DISPLAY / WAYLAND_DISPLAY.
- Start with list_workspaces. If none is assigned to you, ask the user to create or assign one.
- Take a screenshot before clicking; coordinates are pixels of that workspace's screen.
- If a tool returns E_PAUSED the user has taken the workspace over: stop sending input and wait.
"""

SKILL_MD = """---
name: agent-workspaces
description: Use when you need graphical computer use (screenshots, clicking, typing, launching GUI apps). Operates your assigned AgentWorkspace, never the user's personal desktop.
---

""" + INSTRUCTIONS

AGENTS = {
    "claude-code": {"bin": "claude", "kind": "claude-code"},
    "codex": {"bin": "codex", "kind": "codex"},
    "antigravity": {"bin": "agy", "kind": "antigravity"},
    "hermes": {"bin": "hermes", "kind": "hermes"},
}


def server_command() -> List[str]:
    return [sys.executable, os.path.join(ROOT, "bin", "agent-workspaces"), "mcp"]


def add_command(agent: str, workspace: Optional[str]) -> List[str]:
    kind = AGENTS[agent]["kind"]
    envs = [f"AW_AGENT_KIND={kind}"] + ([f"AW_WORKSPACE={workspace}"] if workspace else [])
    exe = AGENTS[agent]["bin"]
    cmd = server_command()
    if agent == "claude-code":
        return [exe, "mcp", "add", "--scope", "user", NAME, *sum((["-e", e] for e in envs), []), "--", *cmd]
    if agent == "codex":
        return [exe, "mcp", "add", *sum((["--env", e] for e in envs), []), NAME, "--", *cmd]
    if agent == "antigravity":
        return [exe, "mcp", "add", *sum((["--env", e] for e in envs), []), NAME, "--", *cmd]
    if agent == "hermes":
        return [exe, "mcp", "add", NAME, "--command", cmd[0], "--env", *envs, "--args", *cmd[1:]]
    raise KeyError(agent)


def remove_command(agent: str) -> List[str]:
    exe = AGENTS[agent]["bin"]
    if agent == "claude-code":
        return [exe, "mcp", "remove", "--scope", "user", NAME]
    return [exe, "mcp", "remove", NAME]


def list_command(agent: str) -> List[str]:
    return [AGENTS[agent]["bin"], "mcp", "list"]


def skill_path(home: Optional[str] = None) -> str:
    return os.path.join(home or os.path.expanduser("~"), ".claude", "skills", NAME, "SKILL.md")


def run(args) -> int:
    agent, action = args.agent, args.action
    exe = AGENTS[agent]["bin"]
    if action == "show":
        print(INSTRUCTIONS)
        print("MCP server command:\n  " + " ".join(add_command(agent, args.workspace)))
        return 0
    if shutil.which(exe) is None:
        print(f"{exe} is not installed; nothing to do", file=sys.stderr)
        return 1
    if action == "status":
        r = subprocess.run(list_command(agent), capture_output=True, text=True, timeout=60, input="")
        listed = NAME in (r.stdout + r.stderr)
        print(f"{agent}: agent-workspaces MCP server is {'registered' if listed else 'not registered'}")
        return 0
    if action == "install":
        cmd = add_command(agent, args.workspace)
        # hermes asks "Enable all tools? [Y/n/select]" after probing the server: answer yes for all of them
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90,
                           input="y\n" if agent == "hermes" else "", )
        if r.returncode != 0:
            print((r.stderr or r.stdout).strip(), file=sys.stderr)
            return r.returncode
        print(f"{agent}: registered MCP server '{NAME}' (AW_AGENT_KIND={AGENTS[agent]['kind']})")
        if agent == "claude-code":
            sp = skill_path()
            if not os.path.exists(sp):
                os.makedirs(os.path.dirname(sp), exist_ok=True)
                with open(sp, "w") as f:
                    f.write(SKILL_MD)
                print(f"wrote {sp} (the instruction text; removed again by 'uninstall')")
        else:
            print("Instruction text is also sent by the MCP server itself; to put it in your agent's "
                  "memory file run: agent-workspaces integrate", agent, "show")
        print("Create and assign a workspace:  agent-workspaces create --name NAME --agent", AGENTS[agent]["kind"])
        return 0
    if action == "uninstall":
        r = subprocess.run(remove_command(agent), capture_output=True, text=True, timeout=60,
                           input="y\n" if agent == "hermes" else "")
        if r.returncode != 0:
            print((r.stderr or r.stdout).strip(), file=sys.stderr)
        if agent == "claude-code":
            sp = skill_path()
            if os.path.exists(sp) and open(sp).read() == SKILL_MD:   # only remove what we wrote
                os.remove(sp)
                try:
                    os.rmdir(os.path.dirname(sp))
                except OSError:
                    pass
        print(f"{agent}: removed MCP server '{NAME}'")
        return 0
    return 2
