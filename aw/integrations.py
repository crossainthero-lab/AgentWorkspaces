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
import re
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
    existed = os.path.exists(rp)
    cur = open(rp).read() if existed else ""
    if BLOCK_BEGIN in cur:
        return f"{rp} already has the managed block; left untouched"
    if dry:
        return f"would append a managed block to {rp}"
    nl = bool(cur) and not cur.endswith("\n")
    os.makedirs(os.path.dirname(rp), exist_ok=True)
    with open(rp, "w") as f:
        f.write(cur + ("\n" if nl else "") + managed_block(INSTRUCTIONS))
    # remember what we did to the file so that uninstall restores the original bytes exactly
    guard_install._state_set("rules:" + rp, (["created"] if not existed else ["existed"]) + (["added-newline"] if nl else []))
    return f"appended a managed block to {rp} (removed again by 'uninstall'; the rest of the file is untouched)"


def _remove_rules(agent: str) -> None:
    rp = rules_path(agent)
    if not rp or not os.path.exists(rp):
        return
    cur = open(rp).read()
    a, b = cur.find(BLOCK_BEGIN), cur.find(BLOCK_END)
    if a < 0 or b < 0:
        return
    flags = guard_install._state_get("rules:" + rp)
    rest = cur[:a] + cur[b + len(BLOCK_END) + 1:]              # the block is BEGIN..END plus one newline
    if "added-newline" in flags and rest.endswith("\n"):
        rest = rest[:-1]
    if not rest.strip() and ("created" in flags or not flags):
        os.remove(rp)
    else:
        open(rp, "w").write(rest)
    guard_install._state_set("rules:" + rp, [])


# --- Hermes: register the MCP server by a TEXT edit of config.yaml. `hermes mcp add` rewrites the whole file (it migrated 44
# lines of defaults on a copy of a real config), so it could never be undone byte-for-byte; a marked block can.
MCP_BEGIN = "  # >>> agent-workspaces MCP server (managed by `agent-workspaces integrate hermes install`, removed by uninstall) >>>"
MCP_END = "  # <<< agent-workspaces MCP server <<<"


def _hermes_cfg() -> str:
    return guard_install.hermes_config_path()


def hermes_mcp_block(workspace: Optional[str]) -> str:
    import json as _j
    cmd = server_command()
    env = {"AW_AGENT_KIND": "hermes", "AW_BASE_DIR": paths.base_dir()}
    if workspace:
        env["AW_WORKSPACE"] = workspace
    lines = [MCP_BEGIN, f"  {NAME}:", f"    command: {_j.dumps(cmd[0])}", "    args:"]
    lines += [f"    - {_j.dumps(a)}" for a in cmd[1:]]
    lines += ["    env:"] + [f"      {k}: {_j.dumps(v)}" for k, v in env.items()] + [MCP_END]
    return "\n".join(lines) + "\n"


def hermes_mcp_install(workspace: Optional[str], dry: bool) -> Optional[str]:
    """Returns a message, or None when the text edit is not possible (the caller falls back to `hermes mcp add`)."""
    path = _hermes_cfg()
    try:
        txt = open(path).read()
    except OSError:
        txt = ""
    if MCP_BEGIN in txt:
        return f"{path}: the MCP server block is already there"
    if re.search(rf"^\s+{re.escape(NAME)}\s*:", txt, re.M) and re.search(r"^mcp_servers\s*:", txt, re.M):
        return f"{path}: an `{NAME}` MCP server is already configured (not by this installer); left untouched"
    lines = txt.split("\n")
    block = hermes_mcp_block(workspace)
    idx = next((i for i, l in enumerate(lines) if re.match(r"^mcp_servers\s*:\s*$", l)), None)
    if idx is None:
        if re.search(r"^mcp_servers\s*:", txt, re.M):
            return None                                   # inline form (`mcp_servers: {}`): let hermes do it
        added_nl = bool(txt) and not txt.endswith("\n")
        new = txt + ("\n" if added_nl else "") + "mcp_servers:\n" + block
        if not dry:
            guard_install._state_set("hermes-mcp", ["created-section"] + (["added-newline"] if added_nl else []))
    else:
        end = len(lines)
        for j in range(idx + 1, len(lines)):
            if lines[j].strip() and not lines[j].startswith((" ", "\t")) and not lines[j].startswith("#"):
                end = j
                break
        last = max((j for j in range(idx + 1, end) if lines[j].startswith((" ", "\t")) and lines[j].strip()), default=idx)
        ins = block.rstrip("\n").split("\n")
        lines[last + 1:last + 1] = ins
        new = "\n".join(lines)
        added_nl = False
    if dry:
        return f"would add a marked MCP server block to {path} (text edit; `hermes mcp add` is not used because it rewrites the whole file)"
    open(path, "w").write(new)
    return f"{path}: MCP server block added (text edit, exactly reversible)"


def hermes_mcp_uninstall(dry: bool) -> Optional[str]:
    path = _hermes_cfg()
    try:
        txt = open(path).read()
    except OSError:
        return None
    a, b = txt.find(MCP_BEGIN), txt.find(MCP_END)
    if a < 0 or b < a:
        return None
    # remove whole lines from the BEGIN line to the END line (inclusive)
    line_start = txt.rfind("\n", 0, a) + 1
    line_end = txt.find("\n", b)
    line_end = len(txt) if line_end < 0 else line_end + 1
    new = txt[:line_start] + txt[line_end:]
    flags = guard_install._state_get("hermes-mcp")
    if "created-section" in flags and new.endswith("mcp_servers:\n"):
        new = new[:-len("mcp_servers:\n")]               # we created the section: take it out again
        if "added-newline" in flags and new.endswith("\n"):
            new = new[:-1]
    if not dry:
        open(path, "w").write(new)
        guard_install._state_set("hermes-mcp", [])
    return f"{path}: MCP server block removed"


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
        listed = NAME in (r.stdout + r.stderr) or (agent == "hermes" and MCP_BEGIN in (open(_hermes_cfg()).read() if os.path.exists(_hermes_cfg()) else ""))
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
            print((hermes_mcp_install(args.workspace, True) if agent == "hermes" else None) or "would run: " + " ".join(cmd))
            print(_write_skill(agent, True) or "no skill for this agent")
            rn = _install_rules(agent, True)
            if rn:
                print(rn)
            if agent in guard_install.SUPPORTED and not getattr(args, "no_guard", False):
                print("\n".join(guard_install.install(agent, True)))
            return 0
        done = hermes_mcp_install(args.workspace, False) if agent == "hermes" else None
        if done:
            print(done)
        else:
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
        manual = hermes_mcp_uninstall(False) if agent == "hermes" else None
        if manual:
            print(manual)
        else:
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
