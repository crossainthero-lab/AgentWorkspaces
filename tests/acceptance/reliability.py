"""RELIABILITY PASS: ordinary natural-language requests to a real agent, judged by INDEPENDENT verification.

  python3 -m tests.acceptance.reliability claude-code            # all tasks, one run each, in order
  python3 -m tests.acceptance.reliability hermes only=files-1,editor-save-1
  python3 -m tests.acceptance.reliability claude-code parallel   # two agents at once: see run_parallel()

A run succeeds only if a check made by THIS script (not the agent's claim) passes: the saved file on disk and its
content, the page content via a local test server whose request log proves the browser in the workspace loaded it and
whose pages carry random facts, the directory listing, the window titles of the workspace, the final screenshot.
The prompts never mention workspaces, MCP, commands or AgentWorkspaces. Each run uses a fresh workspace that is
destroyed afterwards. The agent runs with the host display variables removed (agentrun.clean_env).
"""
import json
import os
import random
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests.acceptance import harness
from aw import agentrun

ROOT = harness.ROOT
NAMES = ["Marta Quinn", "Idris Okoye", "Helena Vogt", "Tomas Reyes", "Priya Natarajan", "Callum Fraser"]
ROLES = ["chair", "secretary", "treasurer", "archivist", "webmaster", "events lead"]
TOPICS = [("autumn rain", "autumn"), ("the ocean at night", "ocean"), ("a quiet library", "library"), ("mountain trails", "mountain"),
          ("fresh bread", "bread"), ("an old lighthouse", "lighthouse")]


class Site:
    def __init__(self, rng):
        self.rng = rng
        self.log = []
        self.mirror = f"{rng.randint(1, 9)}.{rng.randint(10, 99)}"
        self.roles = dict(zip(rng.sample(ROLES, len(ROLES)), rng.sample(NAMES, len(NAMES))))
        site = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                site.log.append((self.path, self.headers.get("User-Agent", ""), time.time()))
                if self.path.startswith("/observatory"):
                    filler = "".join(f"<p>Visitor note {i}: the dome was open and the evening was calm.</p>" for i in range(70))
                    body = (f"<html><head><title>Greenfield Observatory</title></head><body><h1>Greenfield Observatory</h1>"
                            f"<p>Welcome to the observatory.</p>{filler}"
                            f"<p>Facts: the main mirror of the telescope measures <b>{site.mirror} metres</b> across.</p>"
                            f"<p>Open on clear nights.</p></body></html>")
                elif self.path.startswith("/team"):
                    rows = "".join(f"<tr><td>{r}</td><td>{n}</td></tr>" for r, n in site.roles.items())
                    body = f"<html><head><title>Club committee</title></head><body><h1>Club committee</h1><table border=1>{rows}</table></body></html>"
                else:
                    body = "<html><head><title>Test server</title></head><body>ok</body></html>"
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def fetched(self, prefix):
        return [e for e in self.log if e[0].startswith(prefix) and "Chrome" in e[1]]

    def close(self):
        self.srv.shutdown()


def final_answer(agent, run_dir):
    txt = ""
    for l in open(os.path.join(run_dir, "agent.stdout"), errors="replace"):
        try:
            m = json.loads(l)
        except ValueError:
            continue
        if agent == "claude-code" and m.get("type") == "result":
            txt = m.get("result") or txt
        elif agent == "hermes" and m.get("type") == "result":
            txt = m.get("text") or txt
        elif agent == "codex" and m.get("type") == "item.completed" and (m.get("item") or {}).get("type") == "agent_message":
            txt = m["item"].get("text") or txt
    return txt


def find_file(name, res, plan):
    for root in (res.get("home"), plan.cwd):
        if not root:
            continue
        for dp, dn, fn in os.walk(root):
            if name in fn:
                return os.path.join(dp, name)
    return None


def has_all(answer, items):
    low = answer.lower()
    return [i for i in items if i.lower() not in low]


def wins(res):
    return (res.get("workspace_state") or {}).get("windows") or []


# --------------------------------------------------------------------------- tasks
def tasks(rng, site, datadir):
    T = {}
    topic = rng.sample(TOPICS, 4)

    def mk(prefix):
        d = os.path.join(datadir, prefix)
        os.makedirs(d, exist_ok=True)
        return d

    # 1 live website
    def v1(res, plan, ans):
        t = [w for w in wins(res) if "AgentDock" in w or "agdc" in w.lower()]
        miss = has_all(ans, ["One workspace"])
        return (bool(t) and not miss, f"window titles {wins(res)}; answer missing {miss}")
    T["browser-live"] = ("Could you open my browser and go to agdc.dev? I'd like to know what the big headline says.", v1)

    # 2/3 local pages with random facts
    def v2(res, plan, ans):
        got = site.fetched("/observatory")
        return (bool(got) and site.mirror in ans, f"server log: {len(got)} Chrome request(s) for /observatory; expected {site.mirror} in answer")
    T["browser-fact-1"] = (f"Open {site.url}/observatory.html in a browser, scroll down and tell me the diameter of the telescope's main mirror.", v2)

    def v3(res, plan, ans):
        who = site.roles["treasurer"]
        got = site.fetched("/team")
        return (bool(got) and who.lower() in ans.lower(), f"server log: {len(got)} Chrome request(s) for /team; treasurer is {who}")
    T["browser-fact-2"] = (f"Go to {site.url}/team.html and tell me who the club's treasurer is.", v3)

    # 4/5 write and save
    t1, t2 = topic[0], topic[1]
    f1, f2 = f"note-{rng.randint(100, 999)}.txt", f"{t2[1]}-{rng.randint(100, 999)}.txt"

    def mkv(fname, key, minwords):
        def v(res, plan, ans):
            p = find_file(fname, res, plan)
            if not p:
                return (False, f"{fname} not found in the workspace home or project dir")
            txt = open(p, errors="replace").read()
            n = len(txt.split())
            return (n >= minwords and key in txt.lower(), f"{fname}: {n} words, contains '{key}': {key in txt.lower()}")
        return v
    T["editor-save-1"] = (f"Open a text editor, write a short paragraph about {t1[0]} and save it as {f1}.", mkv(f1, t1[1], 15))
    T["editor-save-2"] = (f"I need a quick note. Use a text editor to write two sentences on {t2[0]} and keep it under the file name {f2}.", mkv(f2, t2[1], 8))

    # 6/7 directory listing
    def mkfiles(prefix):
        d = mk(prefix)
        names = [f"{rng.choice(['report', 'photo', 'budget', 'draft', 'scan'])}-{rng.randint(10, 99)}.{rng.choice(['txt', 'pdf', 'jpg', 'csv', 'odt'])}" for _ in range(4)]
        names = list(dict.fromkeys(names))
        sub = f"folder-{rng.randint(10, 99)}"
        os.makedirs(os.path.join(d, sub), exist_ok=True)
        for n in names:
            open(os.path.join(d, n), "w").write("x")
        return d, names + [sub]

    d1, items1 = mkfiles("files1")
    d2, items2 = mkfiles("files2")

    def vf(items):
        def v(res, plan, ans):
            miss = has_all(ans, items)
            shown = any(os.path.basename(d) in w for d in (d1, d2) for w in wins(res))      # the file manager really showed it
            return (not miss and shown, f"answer missing {miss}; file manager window on the folder: {shown}; windows {wins(res)}")
        return v
    T["files-1"] = (f"Open the file manager and tell me what is inside {d1}.", vf(items1))
    T["files-2"] = (f"What files are in the folder {d2}? Please look at it in the file manager and list them.", vf(items2))

    # 8 screenshot of an app
    def v8(res, plan, ans):
        okw = any("Calculator" in w for w in wins(res))
        hits = [k for k in ("basic", "keypad", "buttons", "display", "clear", "equals", "calculator") if k in ans.lower()]
        return (okw and len(hits) >= 3, f"calculator window {okw}; description terms {hits}")
    T["screenshot-describe"] = ("Open the calculator, take a screenshot of it and describe what you see.", v8)

    # 9 read an existing file in an editor
    nf = f"notes-{rng.randint(100, 999)}.txt"
    second = f"the {rng.choice(['violet', 'copper', 'amber', 'jade'])} door opens at {rng.randint(2, 9)}:{rng.choice(['15', '30', '45'])}"
    open(os.path.join(mk("notes"), nf), "w").write(f"Meeting memo\n{second}\nBring snacks\n")

    def v9(res, plan, ans):
        edw = any(nf in w for w in wins(res))
        return (second.lower() in ans.lower() and edw, f"editor window for {nf}: {edw}; second line '{second}' in answer: {second.lower() in ans.lower()}")
    T["editor-read"] = (f"Open {os.path.join(datadir, 'notes', nf)} in a text editor and tell me what the second line says.", v9)

    # 10 copy a number between apps
    amount = rng.randint(1200, 9800)
    inv = f"invoice-{rng.randint(100, 999)}.txt"
    open(os.path.join(mk("inv"), inv), "w").write(f"Invoice\nCustomer: Brightwood Ltd\nAmount due: {amount}\nThank you\n")

    def v10(res, plan, ans):
        w = wins(res)
        return (str(amount * 2) in ans.replace(",", "") and any("Calculator" in x for x in w) and any(inv in x for x in w),
                f"expected {amount * 2} in answer; windows {w}")
    T["copy-number"] = (f"The file {os.path.join(datadir, 'inv', inv)} has an amount due. Open it in a text editor, find the amount, and then use the calculator to double it. What do I get?", v10)

    # 11 two apps
    a, b = rng.randint(12, 49), rng.randint(12, 49)

    def v11(res, plan, ans):
        w = wins(res)
        return (any(str(a * b) in x and "Editor" in x for x in w), f"expected {a * b} in an editor window title; windows {w}")
    T["twoapps"] = (f"Open the calculator and a text editor. Work out {a} times {b} in the calculator, then type the result into the editor.", v11)
    return T


def shell_and_guard(agent):
    """The configuration of a real install: the agent's own shell tool available AND the guard on (per invocation)."""
    import json as _json
    from aw import guard_install

    def cust(plan):
        if agent == "claude-code":
            a = plan.argv
            a[a.index("--tools") + 1] = "Bash"
            a[a.index("--allowedTools") + 1] = "mcp__agent-workspaces,Bash"
            real_rt = f"/run/user/{os.getuid()}"
            a += ["--settings", _json.dumps({"env": guard_install.claude_env(real_rt), "hooks": {"PreToolUse": [{
                "matcher": "Bash|Edit|Write|MultiEdit|NotebookEdit",
                "hooks": [{"type": "command", "command": guard_install.hook_command("claude")}]}]}})]
        else:
            a = plan.argv
            a[a.index("-t") + 1] = f"terminal,mcp-{agentrun.ig.NAME}"
            home = plan.env["HERMES_HOME"]
            old = os.environ.get("HERMES_HOME")
            os.environ["HERMES_HOME"] = home
            try:
                guard_install.hermes_uninstall()
                guard_install.hermes_install()
            finally:
                if old is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = old
    return cust


def run_one(agent, tid, prompt, verify, site, base, cli, out_dir, extra_env):
    holder = {}

    def post(res, plan):
        ans = final_answer(agent, res["run_dir"])
        ok, how = verify(res, plan, ans)
        holder.update(ok=ok, how=how, answer=ans[:300])
        if agent == "hermes":
            # Hermes runs its tools in a background process that is NOT in the agent's process group: audit it too (names only)
            want = f"HERMES_HOME={agentrun.HERMES_RUN_HOME}".encode()
            seen = set()
            names = ("DISPLAY", "WAYLAND_DISPLAY", "WAYLAND_SOCKET", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS")
            for d in os.listdir("/proc"):
                if d.isdigit():
                    try:
                        env = open(f"/proc/{d}/environ", "rb").read().split(b"\0")
                    except OSError:
                        continue
                    if want in env:
                        seen |= {e.split(b"=", 1)[0].decode() for e in env if e.split(b"=", 1)[0].decode() in names}
            holder["background_process_env_display_vars"] = sorted(seen)
        return dict(holder)
    r = harness.run_agent(agent, prompt, base, f"r{int(time.time()) % 100000}-{tid}", os.path.join(out_dir, f"{agent}-{tid}-{int(time.time())}"),
                          timeout=420 if agent == "hermes" else 240, extra_env=extra_env, post=post,
                          customize=None if not os.environ.get("AW_REL_SHELL") else shell_and_guard(agent))
    try:
        cli.call("workspace.destroy", workspace=r["workspace"])
    except Exception:
        pass
    r["task"] = tid
    r["prompt"] = prompt
    r["success"] = bool((r.get("verify") or {}).get("ok")) and r["exit_code"] in (0, 1) and not r["timed_out"]
    return r


def main(agent, only=None, seed=None, parallel_task=None, out_name=None):
    common, cli = harness.start_daemon("rel")
    rng = random.Random(seed or int(time.time()))
    site = Site(rng)
    datadir = os.path.join(common.BASE, "data")
    os.makedirs(datadir)
    T = tasks(rng, site, datadir)
    out_dir = os.path.join(ROOT, ".scratch", "rel")
    os.makedirs(out_dir, exist_ok=True)
    jl = open(os.path.join(out_dir, out_name or f"{agent}.jsonl"), "a")
    extra = {"AW_APP_DENY": "agentdock,waydroid,steam"}
    results = []
    try:
        for tid, (prompt, verify) in T.items():
            if only and tid not in only:
                continue
            before = harness.host_fingerprint()
            r = run_one(agent, tid, prompt, verify, site, common.BASE, cli, out_dir, extra)
            r["host_before"], r["host_after"] = before, harness.host_fingerprint()
            r["host_sockets_unchanged"] = before == r["host_after"]
            results.append(r)
            rec = {k: r.get(k) for k in ("agent", "model", "task", "prompt", "success", "seconds", "exit_code", "workspace", "agent_env_host_display_vars",
                                         "host_sockets_unchanged", "verify", "run_dir")}
            jl.write(json.dumps(rec, default=str) + "\n")
            jl.flush()
            print(("PASS " if r["success"] else "FAIL ") + tid, r["seconds"], "s |", (r.get("verify") or {}).get("how"), flush=True)
    finally:
        jl.close()
        site.close()
        common.stop_daemon(cli)
    n = len(results)
    ok = sum(1 for r in results if r["success"])
    print(f"RESULT {agent}: {ok}/{n} = {100 * ok // max(n, 1)}%")
    return results


if __name__ == "__main__":
    agent = sys.argv[1]
    only = None
    for a in sys.argv[2:]:
        if a.startswith("only="):
            only = a[5:].split(",")
    main(agent, only)
