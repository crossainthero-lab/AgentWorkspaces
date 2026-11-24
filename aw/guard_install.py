"""Install / remove / report the graphical-isolation guard (aw/guard.py) for Claude Code and Hermes Agent.

Both are small, reversible edits of the agent's own configuration, made by `agent-workspaces integrate <agent>
install` unless `--no-guard` is given, and undone by `uninstall`:

  Claude Code   <config dir>/settings.json   (default ~/.claude/settings.json; $CLAUDE_CONFIG_DIR honoured)
                  + "env": the fail-closed environment for every command the agent runs (WAYLAND_DISPLAY pointing at
                    a socket that does not exist, XDG_RUNTIME_DIR = <runtime dir>/aw-guard, DISPLAY and friends empty,
                    toolkits pinned). A PreToolUse rewrite of the command was measured to break the user's own Bash
                    permission rules (they are matched on the rewritten text), so the environment is applied through
                    settings instead and the hook only DENIES.
                  + hooks.PreToolUse[matcher "Bash"]: `python3 -I <aw>/guard.py claude`
  Hermes Agent  <hermes home>/config.yaml    (default ~/.hermes; $HERMES_HOME honoured)
                  + a marked `hooks:` block: pre_tool_call for `terminal|execute_code` -> `python3 -I <aw>/guard.py hermes`
                    (fail_closed). The hook rewrites the command to start with the guard environment and blocks the rest.
                  + <hermes home>/shell-hooks-allowlist.json: the consent entry Hermes requires for a new hook
Nothing else is touched. Uninstall removes exactly what was added and leaves the user's other settings as they were.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from typing import Any, Dict, List, Optional

from . import guard

GUARD_PY = os.path.abspath(guard.__file__)
BEGIN = "# >>> agent-workspaces graphical-isolation guard (managed by `agent-workspaces integrate hermes install`, removed by uninstall) >>>"
END = "# <<< agent-workspaces graphical-isolation guard <<<"


def hook_command(agent: str) -> str:
    return f"python3 -I {shlex.quote(GUARD_PY)} {agent}"


def claude_settings_path() -> str:
    d = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(d, "settings.json")


def hermes_home() -> str:
    return os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")


def claude_env(real_runtime_dir: Optional[str] = None) -> Dict[str, str]:
    env = {"XDG_RUNTIME_DIR": guard.guard_dir(real_runtime_dir)}
    env.update(guard.PRELUDE_ENV)
    env.update({k: "" for k in guard.PRELUDE_UNSET})
    return env


def _is_our_hook(h: Dict[str, Any]) -> bool:
    return isinstance(h, dict) and bool(re.search(r"guard\.py\S*\s+claude\s*$", str(h.get("command", ""))))


def _load(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def _dump(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".aw-tmp"
    with open(tmp, "w") as f:
        f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    try:
        os.chmod(tmp, os.stat(path).st_mode & 0o777)
    except OSError:
        pass
    os.replace(tmp, path)


# --------------------------------------------------------------------------- Claude Code
def _state_path() -> str:
    from . import paths
    return os.path.join(paths.config_dir(), "guard-state.json")


def _state_get(key: str) -> List[str]:
    try:
        return list(json.load(open(_state_path())).get(key, []))
    except (OSError, ValueError):
        return []


def _state_set(key: str, value: List[str]) -> None:
    sp = _state_path()
    try:
        data = json.load(open(sp))
    except (OSError, ValueError):
        data = {}
    if value:
        data[key] = value
    else:
        data.pop(key, None)
    if data:
        os.makedirs(os.path.dirname(sp), exist_ok=True)
        json.dump(data, open(sp, "w"), indent=2)
    elif os.path.exists(sp):
        os.remove(sp)


def claude_status() -> Dict[str, Any]:
    d = _load(claude_settings_path()) or {}
    hooks = [h for g in (d.get("hooks") or {}).get("PreToolUse", []) for h in g.get("hooks", []) if _is_our_hook(h)]
    want = claude_env()
    env_ok = all((d.get("env") or {}).get(k) == v for k, v in want.items())
    deny = (d.get("permissions") or {}).get("deny") or []
    host_mcp = all(x in deny for x in guard.HOST_BROWSER_MCP_DENY)
    return {"hook": bool(hooks), "env": env_ok, "script": os.path.exists(GUARD_PY), "host_browser_mcp_denied": host_mcp,
            "on": bool(hooks) and env_ok}


def claude_install(dry: bool = False) -> List[str]:
    path = claude_settings_path()
    d = _load(path)
    existed = d is not None
    d = d or {}
    msgs = []
    env = d.setdefault("env", {})
    for k, v in claude_env().items():
        if k in env and env[k] != v:
            msgs.append(f"{path}: env.{k} is already set to another value; left as it is (the guard is weaker)")
            continue
        env[k] = v
    perms = d.setdefault("permissions", {})
    deny = perms.setdefault("deny", [])
    added = [x for x in guard.HOST_BROWSER_MCP_DENY if x not in deny]
    deny.extend(added)
    if added:
        _state_set("claude_deny_added", _state_get("claude_deny_added") + [a for a in added if a not in _state_get("claude_deny_added")])
    pre = d.setdefault("hooks", {}).setdefault("PreToolUse", [])
    if not any(_is_our_hook(h) for g in pre for h in g.get("hooks", [])):
        pre.append({"matcher": "Bash|Edit|Write|MultiEdit|NotebookEdit", "hooks": [{"type": "command", "command": hook_command("claude")}]})
    if dry:
        return [f"would {'edit' if existed else 'create'} {path}: add env keys {sorted(claude_env())} and one "
                f"PreToolUse hook (matcher Bash): {hook_command('claude')}"] + msgs
    guard.ensure_guard_dir(os.environ.get("AW_HOST_RUNTIME_DIR"))
    _dump(path, d)
    return [f"{path}: guard added (fail-closed environment + PreToolUse deny hook)"] + msgs


def claude_uninstall(dry: bool = False) -> List[str]:
    path = claude_settings_path()
    d = _load(path)
    if d is None:
        return [f"{path} does not exist; nothing to remove"]
    removed = []
    ours = _state_get("claude_deny_added")
    perms = d.get("permissions") or {}
    deny = perms.get("deny") or []
    for x in list(ours):
        if x in deny:
            deny.remove(x)
            removed.append(f"permissions.deny:{x}")
    if "deny" in perms and not deny:
        del perms["deny"]
    if "permissions" in d and not d["permissions"]:
        del d["permissions"]
    if not dry:
        _state_set("claude_deny_added", [])
    want = claude_env()
    env = d.get("env") or {}
    for k, v in want.items():
        if env.get(k) == v:
            del env[k]
            removed.append(f"env.{k}")
    if "env" in d and not d["env"]:
        del d["env"]
    hooks = d.get("hooks") or {}
    pre = hooks.get("PreToolUse", [])
    newpre = []
    for g in pre:
        hs = [h for h in g.get("hooks", []) if not _is_our_hook(h)]
        if len(hs) != len(g.get("hooks", [])):
            removed.append("hook")
        if hs:
            g = dict(g, hooks=hs)
            newpre.append(g)
        elif len(hs) == len(g.get("hooks", [])):
            newpre.append(g)
    if pre and not newpre:
        hooks.pop("PreToolUse", None)
    elif pre:
        hooks["PreToolUse"] = newpre
    if "hooks" in d and not d["hooks"]:
        del d["hooks"]
    if dry:
        return [f"would remove from {path}: {removed or 'nothing'}"]
    if removed:
        _dump(path, d)
    return [f"{path}: removed {removed or 'nothing'}"]


# --------------------------------------------------------------------------- Hermes
def hermes_config_path() -> str:
    return os.path.join(hermes_home(), "config.yaml")


def hermes_allowlist_path() -> str:
    return os.path.join(hermes_home(), "shell-hooks-allowlist.json")


def _hermes_block() -> str:
    return (f"{BEGIN}\nhooks:\n  pre_tool_call:\n    - matcher: \"terminal|execute_code|write_file|patch|browser_.*|computer_use\"\n"
            f"      command: {json.dumps(hook_command('hermes'))}\n      timeout: 10\n      fail_closed: true\n{END}\n")


def hermes_status() -> Dict[str, Any]:
    try:
        txt = open(hermes_config_path()).read()
    except OSError:
        txt = ""
    block = BEGIN in txt and END in txt
    approved = False
    try:
        data = json.load(open(hermes_allowlist_path()))
        approved = any(e.get("event") == "pre_tool_call" and e.get("command") == hook_command("hermes")
                       for e in data.get("approvals", []))
    except (OSError, ValueError):
        pass
    return {"hook": block, "consent": approved, "script": os.path.exists(GUARD_PY), "on": block and approved}


def hermes_install(dry: bool = False) -> List[str]:
    path = hermes_config_path()
    try:
        txt = open(path).read()
    except OSError:
        txt = ""
    if BEGIN in txt:
        msgs = [f"{path}: the guard block is already there"]
    elif re.search(r"^hooks\s*:", txt, re.M):
        return [f"{path} already has a top-level `hooks:` section; the guard was NOT added (merge it by hand: see "
                "docs/RELEASE.md). Hermes keeps working without it."]
    else:
        msgs = [f"{path}: guard block appended (pre_tool_call hook for the terminal tool)"]
        if dry:
            return [f"would append the guard block to {path} and add a consent entry to {hermes_allowlist_path()}"]
        with open(path, "a") as f:
            f.write(("\n" if txt and not txt.endswith("\n") else "") + _hermes_block())
    if dry:
        return [f"would add a consent entry to {hermes_allowlist_path()}"]
    ap = hermes_allowlist_path()
    try:
        data = json.load(open(ap))
    except (OSError, ValueError):
        data = {"approvals": []}
    cmd = hook_command("hermes")
    mt = None
    try:
        from datetime import datetime, timezone
        mt = datetime.fromtimestamp(os.path.getmtime(GUARD_PY), tz=timezone.utc).isoformat().replace("+00:00", "Z")
        now = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except OSError:
        now = ""
    data["approvals"] = [e for e in data.get("approvals", []) if not (e.get("event") == "pre_tool_call" and e.get("command") == cmd)] + [
        {"event": "pre_tool_call", "command": cmd, "approved_at": now, "script_mtime_at_approval": mt}]
    new = not os.path.exists(ap)
    os.makedirs(os.path.dirname(ap), exist_ok=True)
    fd = os.open(ap, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    msgs.append(f"{ap}: consent entry for the hook (Hermes asks for one per new hook){' (file created)' if new else ''}")
    guard.ensure_guard_dir(os.environ.get("AW_HOST_RUNTIME_DIR"))
    return msgs


def hermes_uninstall(dry: bool = False) -> List[str]:
    path = hermes_config_path()
    msgs = []
    try:
        txt = open(path).read()
    except OSError:
        txt = ""
    a, b = txt.find(BEGIN), txt.find(END)
    if a >= 0 and b > a:
        new = txt[:a] + txt[b + len(END):].lstrip("\n")
        if txt[:a].endswith("\n\n") and not new.startswith("\n"):
            pass
        msgs.append(f"{path}: guard block removed")
        if not dry:
            open(path, "w").write(new)
    else:
        msgs.append(f"{path}: no guard block")
    ap = hermes_allowlist_path()
    try:
        data = json.load(open(ap))
        cmd = hook_command("hermes")
        keep = [e for e in data.get("approvals", []) if not (e.get("event") == "pre_tool_call" and e.get("command") == cmd)]
        if len(keep) != len(data.get("approvals", [])):
            msgs.append(f"{ap}: consent entry removed")
            if not dry:
                if keep:
                    data["approvals"] = keep
                    json.dump(data, open(ap, "w"), indent=2, sort_keys=True)
                else:
                    os.remove(ap)
    except (OSError, ValueError):
        pass
    return msgs


def install(agent: str, dry: bool = False) -> List[str]:
    return {"claude-code": claude_install, "hermes": hermes_install}[agent](dry)


def uninstall(agent: str, dry: bool = False) -> List[str]:
    return {"claude-code": claude_uninstall, "hermes": hermes_uninstall}[agent](dry)


def status(agent: str) -> Dict[str, Any]:
    return {"claude-code": claude_status, "hermes": hermes_status}[agent]()


SUPPORTED = ("claude-code", "hermes")
