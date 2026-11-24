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

from . import guard_install, paths

NAME = "agent-workspaces"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from .mcp import INSTRUCTIONS as _MCP_INSTRUCTIONS  # noqa: E402  (single source of the rule text)

INSTRUCTIONS = "## Graphical computer use: AgentWorkspaces\n\n" + _MCP_INSTRUCTIONS + "\n"

SKILL_MD = """---
name: agent-workspaces
description: Use whenever the user asks to open or use a graphical app, a website or browser, a text editor, the file manager, to take or inspect a screenshot, or to click/type in any window. Operates your own AgentWorkspace (a separate headless desktop), never the user's personal desktop.
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
    # AW_BASE_DIR is explicit so that the MCP server finds the daemon even when the agent's shell guard points
    # XDG_RUNTIME_DIR at <runtime dir>/aw-guard (aw/guard.py)
    envs = [f"AW_AGENT_KIND={kind}", f"AW_BASE_DIR={paths.base_dir()}"] + ([f"AW_WORKSPACE={workspace}"] if workspace else [])
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


# Where each agent keeps user-level skills (SKILL.md folders). Antigravity and Hermes are not listed: which of their
# several skill folders the agent reads was not verified, so only their MCP server is registered.
SKILL_ROOT = {"claude-code": ".claude/skills", "codex": ".codex/skills"}


BLOCK_BEGIN = "<!-- agent-workspaces:begin (managed by `agent-workspaces integrate`; removed by uninstall) -->"
BLOCK_END = "<!-- agent-workspaces:end -->"
# Global memory/rules files, for agents that read one (Antigravity: ~/.gemini/GEMINI.md, see its changelog).
RULES_FILE = {"antigravity": ".gemini/GEMINI.md", "hermes": "SOUL.md"}   # hermes: relative to $HERMES_HOME (default ~/.hermes)


def managed_block(text: str) -> str:
    return f"{BLOCK_BEGIN}\n{text.strip()}\n{BLOCK_END}\n"


def rules_path(agent: str, home: Optional[str] = None) -> Optional[str]:
    rel = RULES_FILE.get(agent)
    if not rel:
        return None
    if agent == "hermes":
        return os.path.join(home or os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes"), rel)
    return os.path.join(home or os.path.expanduser("~"), rel)


def _install_rules(agent: str, dry: bool) -> Optional[str]:
    rp = rules_path(agent)
    if not rp:
        return None
    cur = open(rp).read() if os.path.exists(rp) else ""
    if BLOCK_BEGIN in cur:
        return f"{rp} already has the managed block; left untouched"
    if dry:
        return f"would append a managed block to {rp}"
    os.makedirs(os.path.dirname(rp), exist_ok=True)
    with open(rp, "w") as f:
        f.write(cur + ("\n" if cur and not cur.endswith("\n") else "") + managed_block(INSTRUCTIONS))
    return f"appended a managed block to {rp} (removed again by 'uninstall'; the rest of the file is untouched)"


def _remove_rules(agent: str) -> None:
    rp = rules_path(agent)
    if not rp or not os.path.exists(rp):
        return
    cur = open(rp).read()
    a, b = cur.find(BLOCK_BEGIN), cur.find(BLOCK_END)
    if a < 0 or b < 0:
        return
    rest = (cur[:a] + cur[b + len(BLOCK_END):].lstrip("\n")).rstrip("\n")
    if rest.strip():
        open(rp, "w").write(rest + "\n")
    else:
        os.remove(rp)


def skill_path(agent: str = "claude-code", home: Optional[str] = None) -> Optional[str]:
    rel = SKILL_ROOT.get(agent)
    return os.path.join(home or os.path.expanduser("~"), rel, NAME, "SKILL.md") if rel else None


def _write_skill(agent: str, dry: bool) -> Optional[str]:
    sp = skill_path(agent)
    if not sp:
        return None
    if os.path.exists(sp):
        return f"{sp} already exists; left untouched"
    if dry:
        return f"would write {sp}"
    os.makedirs(os.path.dirname(sp), exist_ok=True)
    with open(sp, "w") as f:
        f.write(SKILL_MD)
    return f"wrote {sp} (removed again by 'uninstall' if unchanged)"


def run(args) -> int:
    agent, action = args.agent, args.action
    exe = AGENTS[agent]["bin"]
    dry = getattr(args, "dry_run", False)
    if action == "show":
        print(INSTRUCTIONS)
        print("MCP server command:\n  " + " ".join(add_command(agent, args.workspace)))
        return 0
    if action == "run":
        from . import agentrun
        if not args.prompt:
            print("give a prompt: integrate AGENT run \"open a text editor ...\"", file=sys.stderr)
            return 2
        return agentrun.run(args)
    if shutil.which(exe) is None:
        print(f"{exe} is not installed; nothing to do", file=sys.stderr)
        return 1
    if action == "status":
        r = subprocess.run(list_command(agent), capture_output=True, text=True, timeout=60, input="")
        listed = NAME in (r.stdout + r.stderr)
        print(f"{agent}: agent-workspaces MCP server is {'registered' if listed else 'not registered'}")
        sp = skill_path(agent)
        if sp:
            print(f"{agent}: instruction skill is {'present' if os.path.exists(sp) else 'absent'} ({sp})")
        if agent in guard_install.SUPPORTED:
            st = guard_install.status(agent)
            print(f"{agent}: graphical-isolation guard is {'ON' if st['on'] else 'OFF'} "
                  f"(hook {'present' if st['hook'] else 'absent'}, "
                  f"{'environment' if agent == 'claude-code' else 'consent'} "
                  f"{'set' if st.get('env', st.get('consent')) else 'missing'}, "
                  f"script {'present' if st['script'] else 'MISSING'})")
        return 0
    if action == "install":
        cmd = add_command(agent, args.workspace)
        if dry:
            print("would run: " + " ".join(cmd))
            print(_write_skill(agent, True) or "no skill for this agent")
            rn = _install_rules(agent, True)
            if rn:
                print(rn)
            if agent in guard_install.SUPPORTED and not getattr(args, "no_guard", False):
                print("\n".join(guard_install.install(agent, True)))
            return 0
        # hermes asks "Enable all tools? [Y/n/select]" after probing the server: answer yes for all of them
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90,
                           input="y\n" if agent == "hermes" else "", )
        if r.returncode != 0:
            print((r.stderr or r.stdout).strip(), file=sys.stderr)
            return r.returncode
        print(f"{agent}: registered MCP server '{NAME}' (AW_AGENT_KIND={AGENTS[agent]['kind']})")
        note = _write_skill(agent, False)
        print(note or "this agent has no skill folder support here; the MCP server sends the instructions itself")
        rn = _install_rules(agent, False)
        if rn:
            print(rn)
        if agent in guard_install.SUPPORTED:
            if getattr(args, "no_guard", False):
                print(f"{agent}: graphical-isolation guard NOT installed (--no-guard)")
            else:
                print("\n".join(guard_install.install(agent, False)))
        print("Nothing else to do: on its first graphical request the agent gets (or re-attaches to) its own "
              f"workspace named {AGENTS[agent]['kind']}-<project>. Undo with: agent-workspaces integrate {agent} uninstall")
        return 0
    if action == "uninstall":
        if dry:
            print("would run: " + " ".join(remove_command(agent)))
            if agent in guard_install.SUPPORTED:
                print("\n".join(guard_install.uninstall(agent, True)))
            return 0
        if agent in guard_install.SUPPORTED:
            print("\n".join(guard_install.uninstall(agent, False)))
        r = subprocess.run(remove_command(agent), capture_output=True, text=True, timeout=60,
                           input="y\n" if agent == "hermes" else "")
        if r.returncode != 0:
            print((r.stderr or r.stdout).strip(), file=sys.stderr)
        _remove_rules(agent)
        sp = skill_path(agent)
        if sp and os.path.exists(sp) and open(sp).read() == SKILL_MD:   # only remove what we wrote
            os.remove(sp)
            try:
                os.rmdir(os.path.dirname(sp))
            except OSError:
                pass
        print(f"{agent}: removed MCP server '{NAME}'")
        return 0
    return 2
