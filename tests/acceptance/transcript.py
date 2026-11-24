"""Print a short, sanitised transcript from a run directory written by harness.run_agent.

Handles the stream formats of claude (stream-json), codex (--json). Images are shown as <image>, long text
is cut, home paths are replaced by ~ and token-looking strings are masked.
"""
import json
import os
import re
import sys

HOME = os.path.expanduser("~")


def clean(s, n=220):
    s = re.sub(re.escape(HOME) + r"/\.cache/claude-tmp/[A-Za-z0-9_\-]+", "<tmp>", str(s))
    s = re.sub(r"[A-Za-z0-9_./\-]*/\.scratch/runs/[^ \"')]+", "<runs>", s)
    s = s.replace(HOME, "~")
    s = re.sub(r"(sk-|ghp_|AIza|Bearer )[A-Za-z0-9_\-\.]{8,}", r"\1<masked>", s)
    s = re.sub(r"/run/user/\d+/awt-[A-Za-z0-9\-]+", "<run>", s)
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n] + "..."


def claude(lines):
    out, usage = [], None
    for l in lines:
        try:
            m = json.loads(l)
        except ValueError:
            continue
        t = m.get("type")
        if t == "system" and m.get("subtype") == "init":
            out.append(f"[init] model={m.get('model')} mcp={[(x['name'], x['status']) for x in m.get('mcp_servers', [])]} "
                       f"builtin_tools={[x for x in m.get('tools', []) if not x.startswith('mcp__')]}")
        elif t == "assistant":
            for c in m["message"]["content"]:
                if c["type"] == "text":
                    out.append("agent: " + clean(c["text"]))
                elif c["type"] == "tool_use":
                    out.append("  call " + c["name"].replace("mcp__agent-workspaces__", "") + " " + clean(json.dumps(c["input"]), 140))
        elif t == "user":
            for c in m["message"].get("content", []):
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    cc = c["content"]
                    if isinstance(cc, list):
                        out.append("  -> " + " | ".join("<image>" if x.get("type") == "image" else clean(x.get("text", ""), 110) for x in cc))
                    else:
                        out.append("  -> " + clean(cc, 110))
        elif t == "result":
            u = m.get("usage") or {}
            usage = {"turns": m.get("num_turns"), "cost_usd": m.get("total_cost_usd"), "duration_ms": m.get("duration_ms"),
                     "input": u.get("input_tokens"), "cache_read": u.get("cache_read_input_tokens"),
                     "cache_creation": u.get("cache_creation_input_tokens"), "output": u.get("output_tokens")}
    return out, usage


def codex(lines):
    out, usage = [], None
    for l in lines:
        try:
            m = json.loads(l)
        except ValueError:
            continue
        t, it = m.get("type"), m.get("item") or {}
        if t == "item.completed":
            ty = it.get("type")
            if ty == "agent_message":
                out.append("agent: " + clean(it.get("text", "")))
            elif ty == "mcp_tool_call":
                res = it.get("result") or {}
                content = res.get("content") if isinstance(res, dict) else None
                kinds = []
                for x in content or []:
                    kinds.append("<image>" if x.get("type") == "image" else clean(x.get("text", ""), 90))
                out.append(f"  call {it.get('tool')} {clean(json.dumps(it.get('arguments')), 120)} -> {' | '.join(kinds) or it.get('status')}")
            elif ty not in ("reasoning",):
                out.append(f"  [{ty}] " + clean(json.dumps({k: v for k, v in it.items() if k not in ('id', 'type')}), 160))
        elif t == "turn.completed":
            usage = m.get("usage")
    return out, usage


def antigravity(lines):
    out, usage = [], None
    for l in lines:
        try:
            m = json.loads(l)
        except ValueError:
            continue
        ev = m.get("event")
        if ev == "init":
            ini = m.get("init", {})
            out.append(f"[init] model={ini.get('model')} tools={len(ini.get('tools', []))} (incl. its own shell and browser tools; "
                       "only mcp(agent-workspaces/*) is allowed in headless mode)")
        elif ev == "step_update":
            s = m["step_update"]
            ti = s.get("tool_info") or {}
            if s["state"] == "DONE" and s["step_type"] == "tool":
                p = ti.get("parameters") or {}
                if s.get("tool_name") == "call_mcp_tool":
                    out.append(f"  call {p.get('ToolName')} {clean(json.dumps(p.get('Arguments')), 120)} -> {clean(ti.get('output') or '', 110)}")
                elif s.get("tool_name") != "update_steps":
                    out.append(f"  [{s.get('tool_name')}] {clean(json.dumps(p), 140)} -> {clean(ti.get('output') or ('ok, no text output (image read)' if s.get('tool_name') == 'read_file' else 'no output (auto-denied in headless mode)'), 80)}")
        elif ev == "result":
            r = m.get("result", {})
            if r.get("response"):
                out.append("agent: " + clean(r["response"]))
            usage = {"turns": r.get("num_turns"), "duration_s": r.get("duration_seconds"), **(r.get("usage") or {})}
    return out, usage


def hermes(lines):
    out, usage = [], None
    for l in lines:
        try:
            m = json.loads(l)
        except ValueError:
            continue
        t = m.get("type")
        if t == "system":
            out.append(f"[init] model={m.get('model')}")
        elif t == "tool_use":
            out.append(f"  call {m['name'].replace('mcp__agent_workspaces__', '')} {clean(json.dumps(m.get('input')), 130)}")
        elif t == "tool_result":
            o = m.get("output") or ""
            try:
                o = json.loads(o).get("result", o) if o.startswith("{") else o
            except (ValueError, AttributeError):
                pass
            img = "<image as file path: MEDIA tag>" if "MEDIA:" in str(o) else ""
            o = re.sub(r"MEDIA:\S+", "", str(o))
            out.append(f"  -> {img} {clean(o, 110)}")
        elif t == "result":
            out.append("agent: " + clean(m.get("text", ""), 400))
    return out, usage


if __name__ == "__main__":
    d = sys.argv[1]
    lines = open(os.path.join(d, "agent.stdout"), errors="replace").read().splitlines()
    kind = sys.argv[2] if len(sys.argv) > 2 else ("claude" if "claude" in d else "antigravity" if "antigravity" in d else "hermes" if "hermes" in d else "codex")
    out, usage = {"claude": claude, "codex": codex, "antigravity": antigravity, "hermes": hermes}[kind](lines)
    print("\n".join(out))
    print("USAGE", json.dumps(usage))
