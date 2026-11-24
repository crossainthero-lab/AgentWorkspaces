"""REAL INTEGRATION TESTS: reliability, isolation failures and authorisation, against real labwc compositors,
real GTK4 / Chrome apps and a real daemon process that is killed and restarted on purpose.

Nothing is simulated except the user's personal desktop, which is a throw-away headless compositor (HostSim) so
that no test can open a window on, or send input to, the real session. Processes are killed only by exact pid
(a compositor / app / daemon started by these tests). At most two compositors run at the same time.
Requires: labwc, python3-gi (GTK4), systemd --user; Chrome tests skip when it is not installed.
"""
import base64
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as K  # noqa: E402
from aw.client import ApiError, Client  # noqa: E402

C = None   # admin client (the human)


def setUpModule():
    global C
    if not K.have("labwc"):
        raise unittest.SkipTest("labwc not installed")
    C = K.start_daemon("rel")


def tearDownModule():
    K.stop_daemon(C)


# ---------------------------------------------------------------- helpers
def meta(wid):
    with open(os.path.join(K.BASE, wid, "ws.json")) as f:
        return json.load(f)


def token_file(wid):
    return os.path.join(K.BASE, wid, "token")


def agent(wid):
    """A client holding ONLY that workspace's credential, as an agent would."""
    return Client(autostart=False, role="agent", token=open(token_file(wid)).read().strip())


def alive(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def scopes(wid):
    return subprocess.run(["systemctl", "--user", "list-units", f"aw-{wid}-*", "--no-legend", "--plain"],
                          capture_output=True, text=True).stdout.strip()


def shot(c, ws):
    r = c.call("screen.screenshot", workspace=ws, inline=True)
    return base64.b64decode(r["base64"])


def code_of(fn):
    try:
        fn()
    except ApiError as e:
        return e.code
    return None


def new_ws(name, **kw):
    kw.setdefault("width", 900)
    kw.setdefault("height", 600)
    return C.call("workspace.create", name=name, **kw)


def destroy(*names):
    for n in names:
        try:
            C.call("workspace.destroy", workspace=n)
        except ApiError:
            pass


# ---------------------------------------------------------------- compositor crash
class CompositorCrash(unittest.TestCase):
    """Kill workspace A's labwc by exact pid. A fails cleanly, recovers on `start`, and B is never touched."""

    def test_compositor_killed_clean_error_cleanup_recovery_and_neighbour_untouched(self):
        a, b = new_ws("crash-a", agent_kind="claude-code"), new_ws("crash-b", agent_kind="codex")
        events = []
        stop = threading.Event()

        def watch():
            try:
                for ev in Client(autostart=False, role="admin").events(types=["workspace.state"]):
                    events.append(ev)
                    if stop.is_set():
                        return
            except ApiError:
                pass
        threading.Thread(target=watch, daemon=True).start()
        try:
            loga, appa = K.launch_probe(C, "crash-a", "A-BEFORE", "#c05621", "ca.log")
            logb, _ = K.launch_probe(C, "crash-b", "B-NEIGHBOUR", "#2f855a", "cb.log")
            ag_a, ag_b = agent(a["id"]), agent(b["id"])
            cpid_a, cpid_b = meta(a["id"])["compositor_pid"], meta(b["id"])["compositor_pid"]
            self.assertTrue(alive(cpid_a) and alive(appa["pid"]))
            tok_before = open(token_file(a["id"])).read()
            sock = os.path.join(K.BASE, a["id"], "rt", "wayland-0")

            os.kill(cpid_a, signal.SIGKILL)                       # exact pid of A's compositor

            # every route to A now fails with a clear error, never with data from elsewhere
            for name, call in (("screenshot", lambda: ag_a.call("screen.screenshot", workspace="crash-a")),
                               ("click", lambda: ag_a.call("input.click", workspace="crash-a", x=5, y=5)),
                               ("type", lambda: ag_a.call("input.type", workspace="crash-a", text="x")),
                               ("windows", lambda: ag_a.call("window.list", workspace="crash-a")),
                               ("launch", lambda: ag_a.call("workspace.launch", workspace="crash-a",
                                                            argv=["/usr/bin/true"])),
                               ("clipboard", lambda: ag_a.call("clipboard.get", workspace="crash-a"))):
                self.assertEqual(code_of(call), "E_NOT_RUNNING", name)
            self.assertTrue(K.wait_for(lambda: C.call("workspace.get", workspace="crash-a")["state"] == "stopped", 5))
            # cleanup: apps and private bus of A are gone, no systemd scope is left behind
            self.assertTrue(K.wait_for(lambda: not alive(appa["pid"]), 10), "A's app is orphaned")
            self.assertTrue(K.wait_for(lambda: scopes(a["id"]) == "", 10), f"scopes left: {scopes(a['id'])}")
            self.assertTrue(any(e["data"].get("id") == a["id"] and e["data"].get("reason") == "compositor_exited"
                                for e in events), events)

            # B did not notice anything
            self.assertTrue(alive(cpid_b))
            n = len(K.log_events(logb))
            ag_b.call("input.click", workspace="crash-b", x=450, y=232)
            self.assertTrue(K.wait_for(lambda: len(K.log_events(logb)) > n, 3))
            self.assertEqual(K.pixel(shot(ag_b, "crash-b"), 60, 60), (0x2f, 0x85, 0x5a))
            self.assertEqual(C.call("workspace.get", workspace="crash-b")["state"], "running")
            self.assertEqual(meta(b["id"])["compositor_pid"], cpid_b)

            # recovery: start again; same credential, fresh compositor, apps can be launched again
            C.call("workspace.start", workspace="crash-a")
            self.assertEqual(C.call("workspace.get", workspace="crash-a")["state"], "running")
            self.assertNotEqual(meta(a["id"])["compositor_pid"], cpid_a)
            self.assertEqual(open(token_file(a["id"])).read(), tok_before)
            loga2, _ = K.launch_probe(C, "crash-a", "A-AFTER", "#6b46c1", "ca2.log")
            n = len(K.log_events(loga2))
            ag_a.call("input.click", workspace="crash-a", x=450, y=232)
            self.assertTrue(K.wait_for(lambda: len(K.log_events(loga2)) > n, 3), "no input after recovery")
            self.assertEqual(K.pixel(shot(ag_a, "crash-a"), 60, 60), (0x6b, 0x46, 0xc1))
            self.assertEqual(meta(b["id"])["compositor_pid"], cpid_b)
        finally:
            stop.set()
            destroy("crash-a", "crash-b")


# ---------------------------------------------------------------- application crash
class ApplicationCrash(unittest.TestCase):
    def test_app_killed_workspace_survives_and_app_can_be_relaunched(self):
        w = new_ws("appcrash")
        events = []

        def watch():
            try:
                for e in Client(autostart=False, role="admin").events(types=["app.*"]):
                    events.append(e)
            except ApiError:
                pass
        threading.Thread(target=watch, daemon=True).start()
        try:
            log1, app1 = K.launch_probe(C, "appcrash", "FIRST", "#2b6cb0", "ac1.log")
            ag = agent(w["id"])
            cpid = meta(w["id"])["compositor_pid"]
            os.kill(app1["pid"], signal.SIGKILL)                   # exact pid of the app
            self.assertTrue(K.wait_for(lambda: not [a for a in C.call("app.list", workspace="appcrash")["apps"]
                                                    if a["n"] == app1["app"]][0]["running"], 5))
            self.assertTrue(K.wait_for(lambda: any(e["event"] == "app.exited" and e["data"]["n"] == app1["app"]
                                                   for e in events), 5), events)
            self.assertEqual(C.call("workspace.get", workspace="appcrash")["state"], "running")
            self.assertTrue(alive(cpid))
            png = shot(ag, "appcrash")                             # screenshots keep working, window is gone
            self.assertNotEqual(K.pixel(png, 60, 60), (0x2b, 0x6c, 0xb0))
            self.assertEqual(ag.call("window.list", workspace="appcrash")["windows"], [])
            log2, app2 = K.launch_probe(C, "appcrash", "SECOND", "#c05621", "ac2.log")
            self.assertNotEqual(app2["app"], app1["app"])
            self.assertEqual(K.pixel(shot(ag, "appcrash"), 60, 60), (0xc0, 0x56, 0x21))
        finally:
            destroy("appcrash")


# ---------------------------------------------------------------- agent disconnects
class AgentDisconnect(unittest.TestCase):
    def test_disconnect_mid_operation_leaves_daemon_and_workspace_usable(self):
        w = new_ws("discon")
        try:
            log, _ = K.launch_probe(C, "discon", "DISCON", "#2b6cb0", "dc.log")
            tok = open(token_file(w["id"])).read().strip()
            sockpath = os.path.join(K.BASE, "api.sock")

            def raw():
                s = socket.socket(socket.AF_UNIX)
                s.connect(sockpath)
                return s

            def req(m, **p):
                return (json.dumps({"v": 2, "id": 1, "method": m, "params": p, "token": tok}) + "\n").encode()

            # 1. long typing request, connection closed before the answer
            s = raw()
            s.sendall(req("input.type", workspace="discon", text="x" * 120))
            s.close()
            # 2. screenshot request, connection reset right after sending
            s = raw()
            s.sendall(req("screen.screenshot", workspace="discon", inline=True))
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
            s.close()
            # 3. half a request, then silence, then close
            s = raw()
            s.sendall(req("input.click", workspace="discon", x=1, y=1)[:25])
            s.close()
            # 4. an event subscriber and a viewer attach that vanish without saying goodbye
            s = raw()
            s.sendall((json.dumps({"v": 2, "id": 1, "method": "events.subscribe", "params": {},
                                   "token": tok}) + "\n").encode())
            s.close()
            admin_tok = open(os.path.join(K.BASE, "admin.token")).read().strip()
            v = raw()
            v.sendall((json.dumps({"v": 2, "id": 1, "method": "viewer.attach", "params": {"workspace": "discon"},
                                   "token": admin_tok}) + "\n").encode())
            self.assertTrue(K.wait_for(lambda: C.call("workspace.get", workspace="discon")["viewers"] == 1, 3))
            v.close()
            self.assertTrue(K.wait_for(lambda: C.call("workspace.get", workspace="discon")["viewers"] == 0, 5),
                            "viewer count stuck after the connection vanished")
            # the daemon and the workspace still work, and nothing is stuck holding a lock
            ag = agent(w["id"])
            time.sleep(1.0)
            n = len(K.log_events(log))
            t0 = time.time()
            ag.call("input.click", workspace="discon", x=450, y=232)
            self.assertTrue(K.wait_for(lambda: any(e["ev"] == "button" for e in K.log_events(log)[n:]), 3))
            self.assertLess(time.time() - t0, 3)
            self.assertEqual(C.call("workspace.get", workspace="discon")["state"], "running")
            logtext = open(os.path.join(K.BASE, "daemon.log")).read()
            self.assertNotIn("Traceback", logtext, logtext[-600:])
        finally:
            destroy("discon")

    def test_client_library_does_not_resend_a_state_changing_call_after_a_lost_connection(self):
        w = new_ws("lost", start=False)
        try:
            c = Client(autostart=False, role="admin")
            c.call("hello")
            c.sock.shutdown(socket.SHUT_RDWR)          # connection dies under the client
            with self.assertRaises(ApiError) as cm:
                c.call("workspace.start", workspace="lost")     # changes state: must not be silently repeated
            self.assertEqual(cm.exception.code, "E_DISCONNECTED")
            self.assertIn("unknown", cm.exception.message)
            c2 = Client(autostart=False, role="admin")
            c2.call("hello")
            c2.sock.shutdown(socket.SHUT_RDWR)
            self.assertEqual(c2.call("workspace.get", workspace="lost")["name"], "lost")   # read-only: retried
        finally:
            destroy("lost")


# ---------------------------------------------------------------- daemon restart
class DaemonRestart(unittest.TestCase):
    def test_daemon_killed_workspaces_adopted_credentials_valid_no_orphans(self):
        global C
        a, b = new_ws("rs-a", agent_kind="claude-code"), new_ws("rs-b", agent_kind="codex")
        try:
            loga, appa = K.launch_probe(C, "rs-a", "RS-A", "#c05621", "ra.log")
            logb, appb = K.launch_probe(C, "rs-b", "RS-B", "#2f855a", "rb.log")
            ag_a = agent(a["id"])
            tok_a = open(token_file(a["id"])).read()
            cp = {w["id"]: meta(w["id"])["compositor_pid"] for w in (a, b)}
            old_daemon = K.DAEMON.pid

            C = K.restart_daemon(kill=True)                     # SIGKILL of the exact daemon pid = a crash
            self.assertNotEqual(K.DAEMON.pid, old_daemon)
            # everything kept running and was re-adopted
            self.assertTrue(all(alive(p) for p in cp.values()) and alive(appa["pid"]) and alive(appb["pid"]))
            ws = {w["name"]: w for w in C.call("workspace.list")["workspaces"]}
            self.assertEqual({n: ws[n]["state"] for n in ("rs-a", "rs-b")}, {"rs-a": "running", "rs-b": "running"})
            self.assertTrue(ws["rs-a"]["apps"][0]["running"])
            # the old agent token still works, no re-provisioning
            self.assertEqual(open(token_file(a["id"])).read(), tok_a)
            ag_a = agent(a["id"])
            self.assertEqual(K.pixel(shot(ag_a, "rs-a"), 60, 60), (0xc0, 0x56, 0x21))
            n = len(K.log_events(loga))
            ag_a.call("input.click", workspace="rs-a", x=450, y=232)
            self.assertTrue(K.wait_for(lambda: len(K.log_events(loga)) > n, 3), "no input after daemon restart")
            self.assertEqual(code_of(lambda: ag_a.call("workspace.get", workspace="rs-b")), "E_FORBIDDEN")
            # a compositor that dies AFTER adoption (no Popen to wait on) is still noticed and cleaned up
            os.kill(cp[b["id"]], signal.SIGKILL)
            self.assertTrue(K.wait_for(lambda: C.call("workspace.get", workspace="rs-b")["state"] == "stopped", 6),
                            "death of an adopted compositor not noticed")
            self.assertTrue(K.wait_for(lambda: not alive(appb["pid"]), 10), "adopted app orphaned")
            self.assertEqual(C.call("workspace.get", workspace="rs-a")["state"], "running")
            # clean daemon stop and start: workspace A is adopted again
            C = K.restart_daemon(kill=False)
            self.assertEqual(C.call("workspace.get", workspace="rs-a")["state"], "running")
            self.assertEqual(C.call("workspace.get", workspace="rs-b")["state"], "stopped")
            # destroy leaves nothing: no process, scope or directory
            C.call("workspace.destroy", workspace="rs-a")
            C.call("workspace.destroy", workspace="rs-b")
            self.assertTrue(K.wait_for(lambda: not alive(cp[a["id"]]) and not alive(appa["pid"]), 8))
            self.assertEqual(scopes(a["id"]), "")
            self.assertFalse(os.path.exists(os.path.join(K.BASE, a["id"])))
        finally:
            destroy("rs-a", "rs-b")


# ---------------------------------------------------------------- concurrent agents
class ConcurrentAgents(unittest.TestCase):
    def test_two_agents_on_two_workspaces_at_the_same_time(self):
        a, b = new_ws("co-a", agent_kind="claude-code"), new_ws("co-b", agent_kind="codex")
        try:
            loga, _ = K.launch_probe(C, "co-a", "CO-A", "#c05621", "coa.log")
            logb, _ = K.launch_probe(C, "co-b", "CO-B", "#2f855a", "cob.log")
            colours = {"co-a": (0xc0, 0x56, 0x21), "co-b": (0x2f, 0x85, 0x5a)}
            errors, seen = [], {"co-a": set(), "co-b": set()}

            def run(wid, name, other):
                ag = agent(wid)
                try:
                    for i in range(25):
                        ag.call("input.move", workspace=name, x=100 + i * 10, y=300)
                        seen[name].add(K.pixel(shot(ag, name), 60, 60))
                        if i % 5 == 0:
                            e = code_of(lambda: ag.call("screen.screenshot", workspace=other))
                            if e != "E_FORBIDDEN":
                                errors.append((name, "cross access gave", e))
                except Exception as ex:  # noqa: BLE001
                    errors.append((name, repr(ex)))
            ts = [threading.Thread(target=run, args=(a["id"], "co-a", "co-b")),
                  threading.Thread(target=run, args=(b["id"], "co-b", "co-a"))]
            [t.start() for t in ts]
            [t.join(120) for t in ts]
            self.assertEqual(errors, [])
            self.assertEqual(seen["co-a"], {colours["co-a"]})
            self.assertEqual(seen["co-b"], {colours["co-b"]})
            # each pointer only ever visited its own workspace's positions
            for log in (loga, logb):
                xs = [e["x"] for e in K.log_events(log) if e["ev"] == "motion"]
                self.assertTrue(xs and 99 <= min(xs) and max(xs) <= 351, (min(xs), max(xs)))
        finally:
            destroy("co-a", "co-b")


# ---------------------------------------------------------------- wrong ids and unauthorised access (real socket)
class UnauthorisedAndWrongIds(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.a = new_ws("ua-a", agent_kind="claude-code", start=False)
        cls.b = new_ws("ua-b", agent_kind="codex", start=False)
        cls.sock = os.path.join(K.BASE, "api.sock")

    @classmethod
    def tearDownClass(cls):
        destroy("ua-a", "ua-b")

    def raw(self, *reqs):
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(10)
        s.connect(self.sock)
        f = s.makefile("rwb")
        out = []
        for r in reqs:
            f.write((json.dumps(r) + "\n").encode())
            f.flush()
            line = f.readline()
            out.append(json.loads(line) if line else None)
        s.close()
        return out

    def test_no_wrong_foreign_expired_and_revoked_credentials(self):
        ta = open(token_file(self.a["id"])).read().strip()
        tb = open(token_file(self.b["id"])).read().strip()
        cases = [("none", None), ("garbage", "x" * 40), ("tampered", ta[:-2] + ("AA" if not ta.endswith("AA") else "BB")),
                 ("empty", "")]
        for label, tok in cases:
            r = self.raw({"v": 2, "id": 1, "method": "workspace.get", "params": {"workspace": "ua-a"},
                          **({"token": tok} if tok is not None else {})})[0]
            self.assertEqual(r["error"]["code"], "E_UNAUTHENTICATED", label)
            self.assertNotIn("result", r)
        # foreign token on the other workspace: every state-changing / reading method refuses
        for m, p in (("workspace.get", {}), ("screen.screenshot", {}), ("input.click", {"x": 1, "y": 1}),
                     ("workspace.launch", {"argv": ["/usr/bin/true"]}), ("window.list", {}),
                     ("clipboard.set", {"text": "x"}), ("workspace.start", {}), ("workspace.destroy", {}),
                     ("workspace.pause", {}), ("credential.rotate", {})):
            r = self.raw({"v": 2, "id": 1, "method": m, "params": {"workspace": "ua-b", **p}, "token": ta})[0]
            self.assertEqual(r["error"]["code"], "E_FORBIDDEN", m)
        # expired and revoked
        short = C.call("credential.issue", workspace="ua-a", ttl_seconds=1)
        rev = C.call("credential.issue", workspace="ua-a", ttl_seconds=600)
        C.call("credential.revoke", workspace="ua-a", id=rev["credential"])
        time.sleep(1.2)
        for label, tok, want in (("expired", short["token"], "E_TOKEN_EXPIRED"),
                                 ("revoked", rev["token"], "E_UNAUTHENTICATED")):
            r = self.raw({"v": 2, "id": 1, "method": "workspace.get", "params": {"workspace": "ua-a"},
                          "token": tok})[0]
            self.assertEqual(r["error"]["code"], want, label)
        # nothing changed anywhere
        self.assertEqual({w["name"]: w["state"] for w in C.call("workspace.list")["workspaces"]
                          if w["name"].startswith("ua-")}, {"ua-a": "stopped", "ua-b": "stopped"})
        self.assertIn("auth.denied", open(os.path.join(K.BASE, "audit.log")).read())

    def test_revocation_and_rotation_hit_connections_that_are_already_open(self):
        c = C.call("credential.issue", workspace="ua-a", ttl_seconds=600)
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(10)
        s.connect(self.sock)
        f = s.makefile("rwb")

        def ask(i):
            f.write((json.dumps({"v": 2, "id": i, "method": "workspace.get", "params": {"workspace": "ua-a"}}) +
                     "\n").encode())
            f.flush()
            return json.loads(f.readline())
        f.write((json.dumps({"v": 2, "id": 0, "method": "auth.login", "params": {"token": c["token"]}}) + "\n").encode())
        f.flush()
        self.assertTrue(json.loads(f.readline())["ok"])
        self.assertTrue(ask(1)["ok"])
        C.call("credential.revoke", workspace="ua-a", id=c["credential"])
        self.assertEqual(ask(2)["error"]["code"], "E_UNAUTHENTICATED")
        s.close()
        # rotation of the default token: the old one dies immediately, the file has the new one
        old = open(token_file(self.a["id"])).read().strip()
        C.call("credential.rotate", workspace="ua-a")
        new = open(token_file(self.a["id"])).read().strip()
        self.assertNotEqual(old, new)
        self.assertEqual(self.raw({"v": 2, "id": 1, "method": "workspace.get", "params": {"workspace": "ua-a"},
                                   "token": old})[0]["error"]["code"], "E_UNAUTHENTICATED")
        self.assertTrue(self.raw({"v": 2, "id": 1, "method": "workspace.get", "params": {"workspace": "ua-a"},
                                  "token": new})[0]["ok"])
        self.assertEqual(oct(os.stat(token_file(self.a["id"])).st_mode & 0o777), "0o600")

    def test_incorrect_workspace_ids_fail_closed_without_a_fallback(self):
        ag = agent(self.a["id"])
        for ref in ("ghost", "", "ua-b", self.b["id"], "*", "../ua-b", "UA-B", None):
            kw = {} if ref is None else {"workspace": ref}
            self.assertIn(code_of(lambda: ag.call("screen.screenshot", **kw)), ("E_FORBIDDEN", "E_BAD_ARGS"), ref)
        self.assertEqual(code_of(lambda: C.call("screen.screenshot", workspace="ghost")), "E_NOT_FOUND")
        self.assertEqual(code_of(lambda: C.call("input.click", workspace="")), "E_BAD_ARGS")
        # a call without a workspace is never redirected to "the only" or "the first" workspace
        self.assertEqual(code_of(lambda: C.call("input.click", x=1, y=1)), "E_BAD_ARGS")
        self.assertEqual(code_of(lambda: ag.call("input.click", x=1, y=1)), "E_BAD_ARGS")

    def test_non_workspace_credentials_cannot_do_human_things(self):
        ag = agent(self.a["id"])
        for m, p in (("workspace.create", {"name": "evil", "start": False}), ("credential.issue", {"workspace": "ua-a"}),
                     ("workspace.resume", {"workspace": "ua-a"}), ("approval.resolve", {"id": "a1",
                                                                                         "decision": "approved"}),
                     ("workspace.configure", {"workspace": "ua-a", "clipboard": "both"}),
                     ("workspace.enter", {"workspace": "ua-a"}), ("grant.resolve", {"id": "g1", "decision": "granted"}),
                     ("audit.tail", {})):
            self.assertEqual(code_of(lambda: ag.call(m, **p)), "E_FORBIDDEN", m)
        self.assertNotIn("evil", [w["name"] for w in C.call("workspace.list")["workspaces"]])

    def test_throttle_and_close_after_repeated_failures(self):
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(20)
        s.connect(self.sock)
        f = s.makefile("rwb")
        closed_at = None
        for i in range(14):
            try:
                f.write((json.dumps({"v": 2, "id": i, "method": "workspace.list", "token": "n" * 30}) + "\n").encode())
                f.flush()
                line = f.readline()
            except OSError:
                line = b""
            if not line:
                closed_at = i
                break
        self.assertIsNotNone(closed_at, "connection was never closed after repeated bad credentials")
        self.assertLessEqual(closed_at, 12)

    def test_other_user_peer_check_exists_on_the_real_socket(self):
        st = os.stat(self.sock)
        self.assertEqual(oct(st.st_mode & 0o777), "0o600")
        self.assertEqual(oct(os.stat(K.BASE).st_mode & 0o777), "0o700")
        self.assertEqual(st.st_uid, os.getuid())


# ---------------------------------------------------------------- routing failures, never the personal desktop
class RoutingFailures(unittest.TestCase):
    """The daemon runs with environment variables that point at a stand-in for the PERSONAL desktop (HostSim,
    carrying a probe window that logs every event). Whatever fails in a workspace, nothing may ever reach it."""

    @classmethod
    def setUpClass(cls):
        global C
        from harness import HostSim
        cls.host = HostSim(K.BASE)
        cls.hostlog = os.path.join(K.BASE, "personal.log")
        cls.host.spawn([sys.executable, K.PROBE, "PERSONAL", cls.hostlog, "#718096"])
        assert K.wait_for(lambda: any(e.get("ev") == "ready" for e in K.log_events(cls.hostlog)), 15)
        time.sleep(0.5)
        # the daemon's environment points at the stand-in personal desktop (worst case): it must never use it
        C = K.restart_daemon(kill=False, extra_env={"WAYLAND_DISPLAY": cls.host.sock, "DISPLAY": ":0"})
        cls.ws = C.call("workspace.create", name="route", width=900, height=600)
        cls.log, cls.app = K.launch_probe(C, "route", "ROUTE", "#2b6cb0", "route.log")
        cls.ag = agent(cls.ws["id"])
        cls.n_host = len(K.log_events(cls.hostlog))
        cls.ptr = dict(cls.host.sess.display_info()["pointer"])
        cls.wins = sorted(w["title"] for w in cls.host.sess.list_windows())

    @classmethod
    def tearDownClass(cls):
        global C
        destroy("route")
        cls.host.close()
        C = K.restart_daemon(kill=False)

    def assertHostUntouched(self):
        time.sleep(0.4)
        self.assertEqual(K.log_events(self.hostlog)[self.n_host:], [], "personal desktop received events")
        self.assertEqual(dict(self.host.sess.display_info()["pointer"]), self.ptr)
        self.assertEqual(sorted(w["title"] for w in self.host.sess.list_windows()), self.wins)

    def test_everything_still_goes_to_the_workspace_while_the_daemon_env_points_at_the_host(self):
        n = len(K.log_events(self.log))
        self.ag.call("input.click", workspace="route", x=450, y=232)
        self.assertTrue(K.wait_for(lambda: len(K.log_events(self.log)) > n, 3))
        self.assertEqual(K.pixel(shot(self.ag, "route"), 60, 60), (0x2b, 0x6c, 0xb0))
        env = open(f"/proc/{K.DAEMON.pid}/environ", "rb").read().split(b"\0")
        self.assertTrue(any(e.startswith(b"WAYLAND_DISPLAY=") for e in env), "test setup: daemon env not poisoned")
        self.assertHostUntouched()

    def test_unknown_stopped_and_crashed_workspaces_never_fall_back_to_the_host(self):
        w2 = C.call("workspace.create", name="route-stopped", width=900, height=600, start=False)
        try:
            ag2 = agent(w2["id"])
            for m, p in (("screen.screenshot", {}), ("input.click", {"x": 450, "y": 232}), ("input.type", {"text": "zz"}),
                         ("input.key", {"combo": "Return"}), ("input.scroll", {"dy": 5}),
                         ("window.list", {}), ("clipboard.get", {}), ("clipboard.set", {"text": "leak"}),
                         ("workspace.launch", {"argv": ["/usr/bin/python3", K.PROBE, "ESCAPED", "/dev/null"]})):
                self.assertEqual(code_of(lambda: ag2.call(m, workspace="route-stopped", **p)), "E_NOT_RUNNING", m)
                self.assertEqual(code_of(lambda: C.call(m, workspace="route-stopped", **p)), "E_NOT_RUNNING", m)
            self.assertEqual(C.call("app.list", workspace="route-stopped")["apps"], [])
            for m in ("screen.screenshot", "input.click"):
                self.assertIn(code_of(lambda: self.ag.call(m, workspace="no-such-workspace", x=1, y=1)),
                              ("E_FORBIDDEN",))
            self.assertHostUntouched()
        finally:
            destroy("route-stopped")

    def test_stale_and_swapped_compositor_sockets_are_refused_before_any_byte_is_sent(self):
        sock = os.path.join(K.BASE, self.ws["id"], "rt", "wayland-0")
        real = sock + ".real"
        os.rename(sock, real)
        fake = socket.socket(socket.AF_UNIX)
        fake.bind(sock)
        fake.listen(4)
        got = []

        def serve():
            fake.settimeout(6)
            try:
                while True:
                    c, _ = fake.accept()
                    c.settimeout(0.3)
                    try:
                        got.append(c.recv(64))          # whatever the daemon sent before giving up
                    except OSError:
                        got.append(b"")
                    c.close()
            except OSError:
                pass
        threading.Thread(target=serve, daemon=True).start()
        try:
            for m, p in (("screen.screenshot", {}), ("input.click", {"x": 450, "y": 232}),
                         ("input.type", {"text": "x"}), ("window.list", {})):
                self.assertEqual(code_of(lambda: self.ag.call(m, workspace="route", **p)), "E_ROUTING", m)
            # the imposter saw connection attempts of the identity check but not one byte of a Wayland request
            self.assertTrue(got and all(g == b"" for g in got), got)
        finally:
            fake.close()
            os.unlink(sock)
            os.rename(real, sock)
        # the real socket is accepted again (no permanent damage)
        self.assertEqual(K.pixel(shot(self.ag, "route"), 60, 60), (0x2b, 0x6c, 0xb0))
        # a symlink to somebody else's socket is not a socket owned by the workspace either
        os.rename(sock, real)
        os.symlink(self.host.sock, sock)
        try:
            self.assertEqual(code_of(lambda: self.ag.call("screen.screenshot", workspace="route")), "E_ROUTING")
            self.assertEqual(code_of(lambda: self.ag.call("input.click", workspace="route", x=1, y=1)), "E_ROUTING")
        finally:
            os.unlink(sock)
            os.rename(real, sock)
        self.assertHostUntouched()
        self.assertEqual(K.pixel(shot(self.ag, "route"), 60, 60), (0x2b, 0x6c, 0xb0))

    def test_pid_reuse_is_detected(self):
        """A recorded compositor pid that is alive but is a different process (start time differs) counts as dead."""
        from aw import manager
        pid = os.getpid()
        self.assertTrue(manager._alive(pid, manager._proc_start(pid)))
        self.assertFalse(manager._alive(pid, "1"))
        self.assertFalse(manager._alive(pid, str(int(manager._proc_start(pid)) + 12345)))


# ---------------------------------------------------------------- takeover and release
class TakeoverAndRelease(unittest.TestCase):
    def test_human_takeover_blocks_the_agent_and_release_lets_it_resume(self):
        w = new_ws("tk2", agent_kind="claude-code")
        try:
            log, _ = K.launch_probe(C, "tk2", "TAKE2", "#6b46c1", "tk2.log")
            ag = agent(w["id"])
            ag.call("input.move", workspace="tk2", x=100, y=100)                 # agent works
            C.call("workspace.pause", workspace="tk2", reason="human is typing")
            n0 = len(K.log_events(log))
            for m, p in (("input.click", dict(x=450, y=232)), ("input.type", dict(text="zzz")),
                         ("input.key", dict(combo="a")), ("input.move", dict(x=10, y=10)),
                         ("input.scroll", dict(dy=2)), ("input.drag", dict(x1=1, y1=1, x2=5, y2=5)),
                         ("clipboard.set", dict(text="q"))):
                self.assertEqual(code_of(lambda: ag.call(m, workspace="tk2", **p)), "E_PAUSED", m)
            # the agent cannot talk its way out: claiming to be human is ignored, resume is not allowed
            self.assertEqual(code_of(lambda: ag.call("input.click", workspace="tk2", x=450, y=232,
                                                     agent_kind="human")), "E_PAUSED")
            self.assertEqual(code_of(lambda: ag.call("input.click", workspace="tk2", x=450, y=232, actor="human")),
                             "E_PAUSED")
            self.assertEqual(code_of(lambda: ag.call("workspace.resume", workspace="tk2")), "E_FORBIDDEN")
            self.assertEqual(code_of(lambda: ag.call("workspace.pause", workspace="tk2")), "E_FORBIDDEN")
            time.sleep(0.4)
            self.assertEqual(K.log_events(log)[n0:], [], "agent input reached the app during takeover")
            # reading is still fine for the agent while the human drives
            self.assertEqual(K.pixel(shot(ag, "tk2"), 60, 60), (0x6b, 0x46, 0xc1))
            self.assertEqual(ag.call("workspace.get", workspace="tk2")["input_mode"], "manual")
            # the human (admin credential) works the workspace meanwhile
            C.call("input.click", workspace="tk2", agent_kind="human", x=450, y=232)
            self.assertTrue(K.wait_for(lambda: any(e["ev"] == "button" for e in K.log_events(log)[n0:]), 3))
            # release
            C.call("workspace.resume", workspace="tk2")
            n1 = len(K.log_events(log))
            ag.call("input.click", workspace="tk2", x=450, y=232)
            self.assertTrue(K.wait_for(lambda: len(K.log_events(log)) > n1, 3), "agent could not resume")
        finally:
            destroy("tk2")


# ---------------------------------------------------------------- browser profile conflicts (real Chrome)
@unittest.skipUnless(K.have("google-chrome"), "google-chrome not installed")
class BrowserProfileConflicts(unittest.TestCase):
    FLAGS = ["--ozone-platform=wayland", "--no-first-run", "--no-default-browser-check", "--password-store=basic"]

    def page(self, colour, text):
        return f"data:text/html,<body style='margin:0;background:%23{colour}'><h1>{text}</h1>"

    def test_two_workspaces_may_not_share_a_profile_and_each_gets_its_own_by_default(self):
        import tempfile
        shared = tempfile.mkdtemp(prefix="aw-prof-", dir="/tmp")
        a, b = new_ws("prof-a", width=1000, height=600), new_ws("prof-b", width=1000, height=600)
        try:
            ag_a, ag_b = agent(a["id"]), agent(b["id"])
            r = ag_a.call("workspace.launch", workspace="prof-a", argv=["google-chrome", *self.FLAGS,
                          "--user-data-dir=" + shared, "--app=" + self.page("d53f8c", "PAGE-A")])
            self.assertTrue(K.wait_for(lambda: ag_a.call("window.list", workspace="prof-a")["windows"], 25))
            time.sleep(2)
            titles_a = sorted(w["title"] for w in ag_a.call("window.list", workspace="prof-a")["windows"])
            # same profile directory from the other workspace: refused, so the page cannot be handed to A's browser
            for form in (["--user-data-dir=" + shared], ["--user-data-dir", shared]):
                e = code_of(lambda: ag_b.call("workspace.launch", workspace="prof-b", argv=[
                    "google-chrome", *self.FLAGS, *form, "--app=" + self.page("2b6cb0", "PAGE-B")]))
                self.assertEqual(e, "E_PROFILE_BUSY", form)
            self.assertEqual(ag_b.call("app.list", workspace="prof-b")["apps"], [])
            time.sleep(1.5)
            self.assertEqual(sorted(w["title"] for w in ag_a.call("window.list", workspace="prof-a")["windows"]),
                             titles_a, "workspace A's browser received something from B")
            # the same workspace may reuse its own profile; a different directory in B is fine
            other = tempfile.mkdtemp(prefix="aw-prof-", dir="/tmp")
            ag_b.call("workspace.launch", workspace="prof-b", argv=["google-chrome", *self.FLAGS,
                      "--user-data-dir=" + other, "--app=" + self.page("2b6cb0", "PAGE-B")])
            self.assertTrue(K.wait_for(lambda: ag_b.call("window.list", workspace="prof-b")["windows"], 25))
            self.assertEqual(sorted(w["title"] for w in ag_a.call("window.list", workspace="prof-a")["windows"]),
                             titles_a)
            # once A's browser is gone the profile is free again
            os.kill(r["pid"], signal.SIGTERM)
            self.assertTrue(K.wait_for(lambda: not alive(r["pid"]), 15))
            self.assertTrue(K.wait_for(lambda: code_of(lambda: ag_b.call("workspace.launch", workspace="prof-b", argv=[
                "/usr/bin/sleep", "1", "--user-data-dir=" + shared])) is None, 5))
        finally:
            destroy("prof-a", "prof-b")

    def test_default_profiles_of_two_workspaces_are_separate_private_homes(self):
        a, b = new_ws("pd-a", width=900, height=500), new_ws("pd-b", width=900, height=500)
        try:
            ha, hb = os.path.join(K.BASE, a["id"], "home"), os.path.join(K.BASE, b["id"], "home")
            self.assertNotEqual(ha, hb)
            real = os.path.expanduser("~/.config/google-chrome")
            for name, wid in (("pd-a", a["id"]), ("pd-b", b["id"])):
                agent(wid).call("workspace.launch", workspace=name, argv=["google-chrome", *self.FLAGS,
                                "--app=" + self.page("d53f8c", name)])
                self.assertTrue(K.wait_for(lambda: agent(wid).call("window.list", workspace=name)["windows"], 25))
            time.sleep(2)
            self.assertTrue(os.path.isdir(os.path.join(ha, ".config", "google-chrome")))
            self.assertTrue(os.path.isdir(os.path.join(hb, ".config", "google-chrome")))
            ta = [w["title"] for w in agent(a["id"]).call("window.list", workspace="pd-a")["windows"]]
            tb = [w["title"] for w in agent(b["id"]).call("window.list", workspace="pd-b")["windows"]]
            self.assertTrue(any("pd-a" in t for t in ta) and not any("pd-b" in t for t in ta), ta)
            self.assertTrue(any("pd-b" in t for t in tb) and not any("pd-a" in t for t in tb), tb)
            self.assertTrue(real != os.path.join(ha, ".config", "google-chrome"))
        finally:
            destroy("pd-a", "pd-b")


if __name__ == "__main__":
    unittest.main()
