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
import re
import shlex
import shutil
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from . import integrations as ig

PASS_THROUGH = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TZ")
HOST_DISPLAY_VARS = ("DISPLAY", "WAYLAND_DISPLAY", "WAYLAND_SOCKET", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS",
                     "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP", "GNOME_SETUP_DISPLAY", "SESSION_MANAGER")

# Cheapest suitable model per agent (names as accepted by that agent's own --model flag).
CHEAP_MODEL = {"claude-code": "haiku", "codex": "gpt-6-luna", "antigravity": "gemini-3.8-flash-low",
               "hermes": "qwen3.6-35b-a3b"}

# Name of the provider block in ~/.hermes/config.yaml that points at the (local) OpenAI-compatible router, and of the
# environment variable holding its key. Both are the acceptance harness's defaults; override for another setup.
HERMES_PROVIDER = os.environ.get("AW_HERMES_PROVIDER", "spark")
HERMES_KEY_NAME = os.environ.get("AW_HERMES_KEY_ENV", "SPARK_LLM_API_KEY")
# outside every real home directory: the agent process must not see a real-home path (tests/acceptance/privenv.py)
HERMES_RUN_HOME = os.path.join("/tmp", f"awt-hermes-home-{os.getuid()}")


def _hermes_key_env() -> Dict[str, str]:
    """The router key for this one process, from the user's existing Hermes .env (never printed or written)."""
    name = HERMES_KEY_NAME
    if os.environ.get(name):
        return {name: os.environ[name]}
    try:
        for line in open(os.path.join(os.path.expanduser("~"), ".hermes", ".env")):
            if line.startswith(name + "="):
                return {name: line.split("=", 1)[1].strip().strip("\"'")}
    except OSError:
        pass
    return {}


def stop_hermes_runtime() -> List[int]:
    """Hermes starts a background gateway ('gateway run --idle-exit') that keeps the MCP servers of a previous run
    alive for its HERMES_HOME. Stop exactly the processes whose environment says HERMES_HOME is OUR throw-away
    home (matched on the environment variable, never on a command-line pattern), so each run starts a fresh MCP
    server with the right settings."""
    import signal
    import time
    want = f"HERMES_HOME={HERMES_RUN_HOME}".encode()
    pids = []
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) == os.getpid():
            continue
        try:
            with open(f"/proc/{d}/environ", "rb") as f:
                if want in f.read().split(b"\0"):
                    pids.append(int(d))
        except OSError:
            continue
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.time() + 8
    while time.time() < deadline and any(os.path.exists(f"/proc/{p}") for p in pids):
        time.sleep(0.2)
    for pid in pids:
        if os.path.exists(f"/proc/{pid}"):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    return pids


def _router_accepts_images(base_url: Optional[str], model: str) -> bool:
    """Ask the router (read-only /v1/models, no model is loaded by this call) whether `model` takes image input."""
    import urllib.request
    key = _hermes_key_env().get(HERMES_KEY_NAME)
    if not base_url or not key:
        return False
    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/models", headers={"Authorization": "Bearer " + key})
        for m in json.load(urllib.request.urlopen(req, timeout=10)).get("data", []):
            if m.get("id") == model:
                return "image" in ((m.get("architecture") or {}).get("input_modalities") or [])
    except Exception:
        return False
    return False


def hermes_home(model: Optional[str], menv: Dict[str, str]) -> str:
    """A throw-away HERMES_HOME (outside ~/.hermes, so the real Hermes config is not edited) whose config.yaml is
    generated per run from the user's non-secret provider settings plus the agent-workspaces MCP server. Its
    dependency environment is created once by Hermes itself and reused."""
    import yaml
    real = os.path.join(os.path.expanduser("~"), ".hermes")
    with open(os.path.join(real, "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    prov = (cfg.get("providers") or {}).get(HERMES_PROVIDER) or {}
    prov = {k: v for k, v in prov.items() if k in ("base_url", "key_env", "models", "discover_models", "api", "transport")}
    server = ig.server_command()
    m = model or CHEAP_MODEL["hermes"]
    out = {"model": {"default": m, "provider": HERMES_PROVIDER, "base_url": prov.get("base_url"),
                     # Hermes passes MCP images to the model natively only for routes it knows are vision-capable;
                     # the router's own model metadata says so for this alias (checked here, not assumed).
                     **({"supports_vision": True} if _router_accepts_images(prov.get("base_url"), m) else {})},
           "providers": {HERMES_PROVIDER: prov},
           "mcp_servers": {ig.NAME: {"command": server[0], "args": server[1:], "env": menv}}}
    os.makedirs(HERMES_RUN_HOME, mode=0o700, exist_ok=True)
    # the rule text, through Hermes' identity file exactly as `integrate hermes install` writes it (SOUL.md is injected
    # into the system prompt; the MCP server's own `instructions` are not shown to the model by Hermes)
    with open(os.path.join(HERMES_RUN_HOME, "SOUL.md"), "w") as f:
        f.write(ig.managed_block(ig.INSTRUCTIONS))
    link = os.path.join(HERMES_RUN_HOME, "hermes-agent")
    if not os.path.islink(link) and not os.path.exists(link):
        os.symlink(os.path.join(real, "hermes-agent"), link)
    fd = os.open(os.path.join(HERMES_RUN_HOME, "config.yaml"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        yaml.safe_dump(out, f)
    return HERMES_RUN_HOME


class Plan:
    def __init__(self, argv: List[str], env: Dict[str, str], cwd: str, cleanup: List[str], notes: List[str]):
        self.argv, self.env, self.cwd, self.cleanup, self.notes = argv, env, cwd, cleanup, notes
        self.procs: List[Any] = []          # helper processes started for this run (stopped by exact PID)
        self.dbus_log: Optional[str] = None
        self.stop_runtime = False

    def keyring_summary(self) -> List[str]:
        """Distinct D-Bus destinations and members seen by the filtering proxy (names only, never payloads)."""
        if not self.dbus_log or not os.path.exists(self.dbus_log):
            return []
        out = set()
        for line in open(self.dbus_log, errors="replace"):
            m = re.search(r"-> (\S+) (call|signal|fake \S+) (\S+)", line)
            if m and m.group(2) == "call":
                out.add(f"{m.group(1)} {m.group(3)}")
        return sorted(out)

    def close(self):
        import signal
        for pr in self.procs:
            if pr.poll() is None:
                try:
                    pr.send_signal(signal.SIGTERM)
                    pr.wait(5)
                except Exception:
                    pr.kill()
        if self.stop_runtime:
            stop_hermes_runtime()
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
         instructions: bool = True, structured: bool = True) -> Plan:
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
                "--setting-sources", "", "--disable-slash-commands", "--no-session-persistence"]
        if structured:
            argv += ["--output-format", "stream-json", "--verbose"]
        if model:
            argv += ["--model", model]
        notes.append("built-in tools removed (--tools \"\"): only the MCP tools exist")
    elif agent == "codex":
        def toml(v):
            return json.dumps(v)
        argv = ["codex", "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config",
                "--ignore-rules", *(["--json"] if structured else []), "-s", "read-only",
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
    elif agent == "antigravity":
        import subprocess
        import time
        # agy keeps its login in the desktop keyring (Secret Service over D-Bus). Give THIS process a filtered
        # bus: xdg-dbus-proxy exposes only org.freedesktop.secrets of the host session bus, nothing else.
        host_bus = os.environ.get("DBUS_SESSION_BUS_ADDRESS") or f"unix:path=/run/user/{os.getuid()}/bus"
        if shutil.which("xdg-dbus-proxy") is None:
            raise RuntimeError("xdg-dbus-proxy is required to give agy access to the keyring only")
        sockp, logp = os.path.join(tmp, "bus"), os.path.join(tmp, "dbus-proxy.log")
        proxy = subprocess.Popen(["xdg-dbus-proxy", host_bus, sockp, "--filter", "--talk=org.freedesktop.secrets",
                                  "--log"], stdout=open(logp, "wb"), stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
        for _ in range(60):
            if os.path.exists(sockp):
                break
            time.sleep(0.1)
        else:
            proxy.kill()
            raise RuntimeError("dbus proxy did not start")
        home = os.path.join(tmp, "home")             # throw-away HOME: its own ~/.gemini config, no credentials in it
        os.makedirs(home, mode=0o700)
        env["HOME"] = home
        env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + sockp
        if instructions:     # what `integrate antigravity install` writes to ~/.gemini/GEMINI.md, here in the throw-away HOME
            gd = os.path.join(home, ".gemini")
            os.makedirs(gd, exist_ok=True)
            with open(os.path.join(gd, "GEMINI.md"), "w") as f:
                f.write(ig.managed_block(ig.INSTRUCTIONS))
        add = ["agy", "mcp", "add", *sum((["--env", f"{k}={v}"] for k, v in menv.items()), []), ig.NAME, "--", *server]
        r = subprocess.run(add, env=env, cwd=cwd, capture_output=True, text=True, timeout=60, input="")
        if r.returncode != 0:
            proxy.kill()
            raise RuntimeError("agy mcp add failed in the throw-away HOME: " + (r.stderr or r.stdout)[-300:])
        # Headless mode cannot prompt: allow ONLY this MCP server (everything else that needs a permission, such as
        # run_command or agy's own browser tools, is auto-denied). --settings is per session and never saved.
        argv = ["agy", "-p", prompt, "--print-timeout", "200s", "--settings",
                json.dumps({"permissions": {"allow": [f"mcp({ig.NAME}/*)"]}})]
        if structured:
            argv += ["--output-format", "stream-json"]
        if model:
            argv += ["--model", model]
        p_ = Plan(argv, env, cwd, [tmp], notes)
        p_.procs.append(proxy)
        p_.dbus_log = logp
        return p_
    elif agent == "hermes":
        # Its MCP servers do not start in the project directory (the working directory is HOME), so say which
        # project the workspace belongs to.
        menv["AW_PROJECT"] = os.path.basename(cwd)
        env["HERMES_HOME"] = hermes_home(model, menv)
        env.update(_hermes_key_env())        # the API key goes into THIS process's environment only
        argv = ["hermes", "chat", "-q", prompt, "-Q", "--max-turns", str(max_turns), "-t", f"mcp-{ig.NAME}",
                "--provider", HERMES_PROVIDER, "-m", model or CHEAP_MODEL["hermes"]]
        if structured:
            argv += ["--format", "stream-json"]
        notes.append("Hermes toolsets restricted to the MCP server's tools")
        stop_hermes_runtime()                 # a gateway left over from an earlier run would reuse its old MCP server
        p_ = Plan(argv, env, cwd, [tmp], notes)
        p_.stop_runtime = True
        return p_
    else:
        raise NotImplementedError(agent)
    return Plan(argv, env, cwd, [tmp], notes)


def run(args) -> int:
    import subprocess
    p = plan(args.agent, args.prompt, model=args.model, project=args.project or "demo",
             base_dir=os.environ.get("AW_BASE_DIR"), cwd=None if args.project else os.getcwd(),
             structured=False)
    try:
        print("+", " ".join(shlex.quote(a) for a in p.argv[:4]), "...", file=sys.stderr)
        return subprocess.run(p.argv, env=p.env, cwd=p.cwd, stdin=subprocess.DEVNULL).returncode
    finally:
        p.close()
