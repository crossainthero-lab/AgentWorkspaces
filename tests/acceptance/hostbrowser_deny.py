"""Does the guard's permission deny rule really remove a host-browser MCP server from a real Claude Code session?

A STAND-IN MCP server named `claude-in-chrome` (one tool `navigate`; it only returns the word NAVIGATED, it drives nothing)
is attached in a fully private environment. Without the deny rule Claude can call it; with the rule the server's tools are
unavailable to the agent. The real extension is never involved.

  python3 -m tests.acceptance.hostbrowser_deny
"""
import json
import os
import subprocess
import sys

from tests.acceptance import privenv
from aw import guard

# The name `claude-in-chrome` itself is reserved by Claude Code for its extension (a stdio server with that name is ignored), so the
# mechanism is shown with another name and the SAME rule shape (`mcp__<server>`); the real rule is guard.HOST_BROWSER_MCP_DENY.
NAME = "host-browser"

FAKE = r'''
import json, sys
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m:
        continue
    meth = m.get("method")
    if meth == "initialize":
        r = {"protocolVersion": m["params"].get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "0"}}
    elif meth == "tools/list":
        r = {"tools": [{"name": "navigate", "description": "Navigate the browser to a URL", "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}]}
    elif meth == "tools/call":
        r = {"content": [{"type": "text", "text": "NAVIGATED"}]}
    else:
        r = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}) + "\n"); sys.stdout.flush()
'''


def run(deny):
    priv = privenv.PrivateEnv("hbdeny")
    try:
        srv = os.path.join(priv.root, "fake_mcp.py")
        open(srv, "w").write(FAKE)
        cfg = os.path.join(priv.root, "mcp.json")
        json.dump({"mcpServers": {NAME: {"command": "/usr/bin/python3", "args": [srv]}}}, open(cfg, "w"))
        settings = {"permissions": {"deny": [rule.replace("claude-in-chrome", NAME) for rule in guard.HOST_BROWSER_MCP_DENY]}} if deny else {}
        env = priv.env()
        env.update(priv.claude_login())
        privenv.assert_private(env, label="hostbrowser_deny")
        argv = [os.path.realpath(os.popen("command -v claude").read().strip() or "claude"), "-p",
                "Call the navigate tool with url http://example.invalid/ and tell me exactly what it returned.",
                "--model", "haiku", "--tools", "", "--mcp-config", cfg, "--strict-mcp-config", "--allowedTools", f"mcp__{NAME}",
                "--permission-mode", "dontAsk", "--setting-sources", "", "--disable-slash-commands", "--no-session-persistence",
                "--output-format", "stream-json", "--verbose"] + (["--settings", json.dumps(settings)] if settings else [])
        p = subprocess.run(argv, env=env, cwd=priv.tmp, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        tools, called, results = [], [], []
        for l in p.stdout.splitlines():
            try:
                m = json.loads(l)
            except ValueError:
                continue
            if m.get("type") == "system" and m.get("subtype") == "init":
                tools = m.get("tools") or []
            elif m.get("type") == "assistant":
                called += [c["name"] for c in m["message"]["content"] if c.get("type") == "tool_use"]
            elif m.get("type") == "user":
                results += [str(c.get("content"))[:120] for c in m["message"].get("content", []) if isinstance(c, dict) and c.get("type") == "tool_result"]
        return {"deny_rule": deny, "tools_listed": [t for t in tools], "tool_calls": called, "results": results}
    finally:
        priv.close()


if __name__ == "__main__":
    out = {"without_rule": run(False), "with_rule": run(True)}
    print(json.dumps(out, indent=1))
    a, b = out["without_rule"], out["with_rule"]
    ok = any("NAVIGATED" in r for r in a["results"]) and not any("NAVIGATED" in r for r in b["results"]) \
        and not any(t.startswith(f"mcp__{NAME}") for t in b["tools_listed"])
    print("RESULT", "deny rule removes the host-browser tools from the agent" if ok else "NOT PROVEN")
    sys.exit(0 if ok else 1)
