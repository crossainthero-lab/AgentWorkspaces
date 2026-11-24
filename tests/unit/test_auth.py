"""UNIT tests of authentication, authorisation, credential lookup, grants and the audit log.
No compositor is started: workspaces are created with start=False. A call that is authorised but cannot run
answers E_NOT_RUNNING; a call that is refused answers E_FORBIDDEN / E_UNAUTHENTICATED, so the two outcomes
are distinguishable without any real app."""
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


class Base(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="awa", dir="/tmp")
        os.environ["AW_BASE_DIR"] = self.base
        for k in ("AW_TOKEN", "AW_TOKEN_FILE", "AW_ADMIN_TOKEN", "AW_ADMIN_TOKEN_FILE", "AW_WORKSPACE",
                  "AW_AGENT_KIND"):
            os.environ.pop(k, None)
        from aw import daemon
        self.daemon = daemon.Daemon()
        self.admin = self.daemon.admin_state()
        self.a = self.daemon.handle("workspace.create", {"name": "alpha", "start": False, "agent_kind": "codex"},
                                    self.admin)
        self.b = self.daemon.handle("workspace.create", {"name": "beta", "start": False, "agent_kind": "hermes"},
                                    self.admin)
        self.sa = self.daemon.workspace_state("alpha")
        self.sb = self.daemon.workspace_state("beta")

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)
        os.environ.pop("AW_BASE_DIR", None)

    def call(self, st, method, **p):
        return self.daemon.handle(method, p, st)

    def code(self, st, method, **p):
        from aw.manager import ManagerError
        with self.assertRaises(ManagerError) as cm:
            self.daemon.handle(method, p, st)
        return cm.exception.code

    def token(self, ws):
        with open(self.daemon.creds.token_path(ws["id"])) as f:
            return f.read().strip()


class Credentials(Base):
    def test_token_files_are_0600_in_0700_dirs_and_hashes_only_at_rest(self):
        for ws in (self.a, self.b):
            p = self.daemon.creds.token_path(ws["id"])
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(p)).st_mode), 0o700)
            tok = self.token(ws)
            self.assertTrue(tok.startswith("awt1_" + ws["id"] + "_"))
            self.assertGreaterEqual(len(tok), 50)
            with open(os.path.join(os.path.dirname(p), "credentials.json")) as f:
                self.assertNotIn(tok, f.read())
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.base, "admin.token")).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.base).st_mode), 0o700)

    def test_tokens_are_unique_and_unguessable(self):
        self.assertNotEqual(self.token(self.a), self.token(self.b))

    def test_authenticate_binds_workspace_and_rejects_garbage(self):
        from aw.auth import AuthError
        cs = self.daemon.creds
        pr = cs.authenticate(self.token(self.a))
        self.assertEqual((pr.kind, pr.ws_id), ("workspace", self.a["id"]))
        self.assertNotIn("admin", pr.scopes)
        for bad in ("", "x", None, 5, "awt1_" + self.a["id"] + "_" + "A" * 43):
            with self.assertRaises(AuthError) as cm:
                cs.authenticate(bad)
            self.assertEqual(cm.exception.code, "E_UNAUTHENTICATED")

    def test_credentials_survive_daemon_restart_and_rotation_revokes_old(self):
        from aw import daemon
        old = self.token(self.a)
        d2 = daemon.Daemon()
        self.assertEqual(d2.creds.authenticate(old).ws_id, self.a["id"])
        r = d2.handle("credential.rotate", {"workspace": "alpha"}, d2.admin_state())
        self.assertEqual(r["token_file"], d2.creds.token_path(self.a["id"]))
        new = self.token(self.a)
        self.assertNotEqual(old, new)
        from aw.auth import AuthError
        with self.assertRaises(AuthError):
            d2.creds.authenticate(old)
        self.assertEqual(d2.creds.authenticate(new).ws_id, self.a["id"])

    def test_deleted_or_tampered_token_file_is_regenerated_not_trusted(self):
        p = self.daemon.creds.token_path(self.a["id"])
        old = self.token(self.a)
        with open(p, "w") as f:
            f.write("awt1_%s_forged\n" % self.a["id"])
        from aw import daemon
        d2 = daemon.Daemon()
        self.assertNotEqual(self.token(self.a), "awt1_%s_forged" % self.a["id"])
        from aw.auth import AuthError
        with self.assertRaises(AuthError):
            d2.creds.authenticate(old)           # the rotated-away token must not survive
        self.assertEqual(d2.creds.authenticate(self.token(self.a)).ws_id, self.a["id"])

    def test_world_readable_token_file_is_regenerated(self):
        p = self.daemon.creds.token_path(self.a["id"])
        os.chmod(p, 0o644)
        from aw import daemon
        daemon.Daemon()
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)

    def test_issue_expire_revoke(self):
        from aw.auth import AuthError
        r = self.call(self.admin, "credential.issue", workspace="alpha", scopes=["screenshot"], ttl_seconds=1)
        pr = self.daemon.creds.authenticate(r["token"])
        self.assertEqual(sorted(pr.scopes), ["screenshot"])
        st = {"principal": pr}
        self.assertEqual(self.code(st, "input.click", workspace="alpha"), "E_FORBIDDEN")      # lacks scope
        self.assertEqual(self.code(st, "screen.screenshot", workspace="alpha"), "E_NOT_RUNNING")  # authorised
        time.sleep(1.1)
        self.assertEqual(self.code(st, "screen.screenshot", workspace="alpha"), "E_TOKEN_EXPIRED")
        r2 = self.call(self.admin, "credential.issue", workspace="alpha", ttl_seconds=600)
        st2 = {"principal": self.daemon.creds.authenticate(r2["token"])}
        self.assertEqual(self.code(st2, "screen.screenshot", workspace="alpha"), "E_NOT_RUNNING")
        self.call(self.admin, "credential.revoke", workspace="alpha", id=r2["credential"])
        self.assertEqual(self.code(st2, "screen.screenshot", workspace="alpha"), "E_UNAUTHENTICATED")  # live conn
        with self.assertRaises(AuthError):
            self.daemon.creds.authenticate(r2["token"])
        # the default credential is untouched by revoking one issued credential
        self.assertEqual(self.code(self.sa, "screen.screenshot", workspace="alpha"), "E_NOT_RUNNING")

    def test_issue_validates_scopes_and_ttl(self):
        self.assertEqual(self.code(self.admin, "credential.issue", workspace="alpha", scopes=["root"]), "E_BAD_SCOPE")
        self.assertEqual(self.code(self.admin, "credential.issue", workspace="alpha", ttl_seconds=10 ** 9),
                         "E_BAD_TTL")

    def test_exchange_only_narrows(self):
        r = self.call(self.sa, "auth.exchange", scopes=["screenshot", "read"], ttl_seconds=60)
        pr = self.daemon.creds.authenticate(r["token"])
        self.assertEqual(sorted(pr.scopes), ["read", "screenshot"])
        self.assertEqual(pr.ws_id, self.a["id"])
        self.assertLessEqual(r["expires_at"] - time.time(), 61)
        self.assertEqual(self.code(self.sa, "auth.exchange", scopes=["admin"]), "E_FORBIDDEN")
        self.assertEqual(self.code({"principal": pr}, "auth.exchange", scopes=["input"]), "E_FORBIDDEN")
        # a short-lived child cannot outlive its parent's expiry
        r2 = self.call({"principal": pr}, "auth.exchange", ttl_seconds=3600)
        self.assertLessEqual(r2["expires_at"], r["expires_at"] + 1)

    def test_admin_rotation_ends_old_admin_sessions(self):
        old = self.daemon.creds.authenticate(open(os.path.join(self.base, "admin.token")).read().strip())
        self.call(self.admin, "credential.rotate", admin=True)
        self.assertEqual(self.code({"principal": old}, "workspace.list"), "E_UNAUTHENTICATED")


class Authorisation(Base):
    def test_no_credential_means_nothing_works_except_hello(self):
        self.assertTrue(self.call({}, "hello")["auth"]["required"])
        for m in ("workspace.list", "workspace.get", "input.click", "screen.screenshot", "workspace.create",
                  "approval.resolve", "workspace.resume"):
            self.assertEqual(self.code({}, m, workspace="alpha"), "E_UNAUTHENTICATED", m)

    def test_foreign_token_cannot_touch_another_workspace_by_name_or_id(self):
        for ref in ("beta", self.b["id"], "BETA"):
            for m in ("workspace.get", "screen.screenshot", "input.click", "input.type", "window.list",
                      "clipboard.get", "workspace.launch", "approval.request", "app.list"):
                self.assertEqual(self.code(self.sa, m, workspace=ref, argv=["x"], text="t", title="t"),
                                 "E_FORBIDDEN", f"{m} {ref}")

    def test_unknown_workspace_is_forbidden_for_agents_not_found_for_admin_and_never_a_fallback(self):
        self.assertEqual(self.code(self.sa, "screen.screenshot", workspace="ghost"), "E_FORBIDDEN")
        self.assertEqual(self.code(self.sa, "input.click", workspace=""), "E_BAD_ARGS")
        self.assertEqual(self.code(self.sa, "input.click"), "E_BAD_ARGS")          # no implicit default workspace
        self.assertEqual(self.code(self.admin, "screen.screenshot", workspace="ghost"), "E_NOT_FOUND")
        self.assertEqual(self.code(self.admin, "input.click"), "E_BAD_ARGS")
        msg = ""
        try:
            self.call(self.sa, "screen.screenshot", workspace="beta")
        except Exception as e:
            msg = str(e)
        self.assertIn(self.a["id"], msg)       # the error says which workspace the credential IS bound to
        self.assertNotIn(self.token(self.b), msg)

    def test_forbidden_and_notfound_are_indistinguishable_for_existing_vs_missing_foreign_ref(self):
        from aw.manager import ManagerError
        msgs = []
        for ref in ("beta", "ghost"):
            with self.assertRaises(ManagerError) as cm:
                self.call(self.sa, "workspace.get", workspace=ref)
            msgs.append(cm.exception.code)
        self.assertEqual(msgs[0], msgs[1])

    def test_own_workspace_is_authorised(self):
        for ref in ("alpha", self.a["id"], "ALPHA"):
            self.assertEqual(self.code(self.sa, "screen.screenshot", workspace=ref), "E_NOT_RUNNING")
        self.assertEqual(self.call(self.sa, "workspace.get", workspace="alpha")["id"], self.a["id"])

    def test_agents_cannot_use_human_or_admin_methods(self):
        for m, p in (("workspace.create", {"name": "x"}), ("workspace.destroy", {"workspace": "alpha"}),
                     ("workspace.pause", {"workspace": "alpha"}), ("workspace.resume", {"workspace": "alpha"}),
                     ("workspace.takeover", {"workspace": "alpha"}), ("workspace.configure", {"workspace": "alpha",
                      "clipboard": "both"}), ("workspace.assign", {"workspace": "alpha", "agent_kind": "none"}),
                     ("workspace.enter", {"workspace": "alpha"}), ("viewer.attach", {"workspace": "alpha"}),
                     ("approval.resolve", {"id": "a1", "decision": "approved"}),
                     ("credential.issue", {"workspace": "alpha"}), ("credential.rotate", {"workspace": "alpha"}),
                     ("credential.list", {"workspace": "alpha"}), ("grant.resolve", {"id": "g1",
                      "decision": "granted"}), ("grant.revoke", {"id": "g1"}), ("audit.tail", {}),
                     ("workspace.start", {"workspace": "alpha"}), ("workspace.stop", {"workspace": "alpha"}),
                     ("workspace.rename", {"workspace": "alpha", "name": "z"})):
            self.assertEqual(self.code(self.sa, m, **p), "E_FORBIDDEN", m)

    def test_unlisted_future_methods_are_denied_to_agents(self):
        self.assertEqual(self.code(self.sa, "browser.clone_profile", workspace="alpha"), "E_FORBIDDEN")
        self.assertEqual(self.code(self.admin, "browser.clone_profile", workspace="alpha"), "E_UNKNOWN_METHOD")

    def test_agent_cannot_claim_to_be_human_to_bypass_takeover(self):
        self.call(self.admin, "workspace.pause", workspace="alpha", reason="me")
        # state is stopped so we only look at the actor decision
        ws = self.daemon.mgr.get("alpha")
        self.assertEqual(self.daemon._actor({"agent_kind": "human"}, self.sa["principal"]), "agent")
        self.assertEqual(self.daemon._actor({"actor": "human"}, self.sa["principal"]), "agent")
        self.assertEqual(self.daemon._actor({"agent_kind": "human"}, self.admin["principal"]), "human")
        from aw.manager import ManagerError
        with self.assertRaises(ManagerError) as cm:
            self.daemon._guard_input(ws, {"agent_kind": "human"}, self.sa["principal"])
        self.assertEqual(cm.exception.code, "E_PAUSED")
        self.assertEqual(self.daemon._guard_input(ws, {"agent_kind": "human"}, self.admin["principal"]), "human")

    def test_list_shows_only_the_bound_workspace(self):
        names = [w["name"] for w in self.call(self.sa, "workspace.list")["workspaces"]]
        self.assertEqual(names, ["alpha"])
        names = sorted(w["name"] for w in self.call(self.admin, "workspace.list")["workspaces"])
        self.assertEqual(names, ["alpha", "beta"])

    def test_approvals_are_scoped_and_agents_cannot_approve_themselves(self):
        a1 = self.call(self.sa, "approval.request", workspace="alpha", title="need X")
        b1 = self.call(self.sb, "approval.request", workspace="beta", title="need Y")
        self.assertEqual([a["id"] for a in self.call(self.sa, "approval.list")["approvals"]], [a1["id"]])
        self.assertEqual(self.code(self.sa, "approval.get", id=b1["id"]), "E_NOT_FOUND")
        self.assertEqual(self.code(self.sa, "approval.resolve", id=a1["id"], decision="approved"), "E_FORBIDDEN")
        self.assertEqual(self.call(self.admin, "approval.resolve", id=a1["id"], decision="approved")["status"],
                         "approved")
        self.assertEqual(self.call(self.sa, "approval.get", id=a1["id"])["status"], "approved")

    def test_screenshot_path_is_confined_for_agents(self):
        ws = self.daemon.mgr.get("alpha")
        pr = self.sa["principal"]
        d = os.path.join(ws.dir, "shots")
        self.assertEqual(self.daemon._shot_path(ws, "a.png", pr), os.path.join(os.path.realpath(d), "a.png"))
        victim = os.path.join(self.base, "victim")
        for bad in (victim, "../x.png", "/etc/passwd", os.path.join(d, "..", "z.png"), ""):
            from aw.manager import ManagerError
            with self.assertRaises(ManagerError):
                self.daemon._shot_path(ws, bad, pr)
        os.symlink(self.base, os.path.join(d, "link"))
        with self.assertRaises(Exception):
            self.daemon._shot_path(ws, "link/escape.png", pr)
        self.assertEqual(self.daemon._shot_path(ws, victim, self.admin["principal"]), victim)

    def test_names_cannot_impersonate_ids(self):
        self.assertEqual(self.code(self.admin, "workspace.create", name=self.a["id"], start=False), "E_BAD_NAME")
        self.assertEqual(self.code(self.admin, "workspace.rename", workspace="alpha", name="wabcdef"), "E_BAD_NAME")

    def test_events_match_ids_only(self):
        import queue
        q = queue.Queue()
        self.daemon.bus.subscribe(q, [self.a["id"]], None)
        self.daemon.bus.emit("x", {"name": self.a["id"]})        # a name equal to an id must not match
        self.daemon.bus.emit("x", {"workspace": self.a["id"]})
        self.assertTrue(q.empty())
        self.daemon.bus.emit("x", {"id": self.a["id"]})
        self.assertFalse(q.empty())


class Grants(Base):
    def test_agents_can_only_request_and_check(self):
        g = self.call(self.sa, "grant.request", workspace="alpha", kind="browser_profile", resource="Default")
        self.assertEqual(g["status"], "pending")
        self.assertEqual(self.call(self.sa, "grant.check", workspace="alpha", kind="browser_profile",
                                   resource="Default")["granted"], False)
        self.assertEqual(self.code(self.sa, "grant.resolve", id=g["id"], decision="granted"), "E_FORBIDDEN")
        self.assertEqual(self.code(self.sa, "grant.revoke", id=g["id"]), "E_FORBIDDEN")
        self.assertEqual(self.code(self.sa, "grant.request", workspace="beta", kind="host_clipboard"), "E_FORBIDDEN")
        self.assertEqual(self.code(self.sa, "grant.request", workspace="alpha", kind="root"), "E_BAD_GRANT")

    def test_admin_without_a_human_channel_is_refused(self):
        g = self.call(self.sa, "grant.request", workspace="alpha", kind="host_clipboard")
        st = {"principal": self.admin["principal"], "peer_pid": os.getpid()}      # this test process: no tty
        if not __import__("aw.auth", fromlist=["x"]).peer_has_tty(os.getpid()):
            self.assertEqual(self.code(st, "grant.resolve", id=g["id"], decision="granted"), "E_NOT_HUMAN_CHANNEL")
        st = {"principal": self.admin["principal"], "peer_pid": None}
        self.assertEqual(self.code(st, "grant.resolve", id=g["id"], decision="granted"), "E_NOT_HUMAN_CHANNEL")
        self.assertEqual(self.call(self.sa, "grant.check", workspace="alpha", kind="host_clipboard")["granted"], False)

    def test_lifecycle_grant_use_expiry_revocation_and_logging(self):
        from aw.grants import GrantError
        g = self.call(self.sa, "grant.request", workspace="alpha", kind="browser_profile", resource="Work")
        with self.assertRaises(GrantError) as cm:
            self.daemon.grants.require(self.a["id"], "browser_profile", "Work")
        self.assertEqual(cm.exception.code, "E_GRANT_REQUIRED")
        r = self.call(self.admin, "grant.resolve", id=g["id"], decision="granted", ttl_seconds=1)
        self.assertEqual(r["status"], "granted")
        self.daemon.grants.require(self.a["id"], "browser_profile", "Work")
        with self.assertRaises(GrantError):                             # bound to the resource
            self.daemon.grants.require(self.a["id"], "browser_profile", "Personal")
        with self.assertRaises(GrantError):                             # bound to the workspace
            self.daemon.grants.require(self.b["id"], "browser_profile", "Work")
        time.sleep(1.1)
        with self.assertRaises(GrantError):                             # time-limited
            self.daemon.grants.require(self.a["id"], "browser_profile", "Work")
        g2 = self.call(self.sa, "grant.request", workspace="alpha", kind="host_clipboard")
        self.call(self.admin, "grant.resolve", id=g2["id"], decision="granted")
        self.assertTrue(self.call(self.sa, "grant.check", workspace="alpha", kind="host_clipboard")["granted"])
        self.call(self.admin, "grant.revoke", id=g2["id"])                # revocable
        self.assertFalse(self.call(self.sa, "grant.check", workspace="alpha", kind="host_clipboard")["granted"])
        events = [e["event"] for e in self.daemon.audit.tail(100)]
        for ev in ("grant.requested", "grant.granted", "grant.refused_use", "grant.used", "grant.revoked"):
            self.assertIn(ev, events)

    def test_denied_and_cross_workspace_listing(self):
        g = self.call(self.sb, "grant.request", workspace="beta", kind="host_clipboard")
        self.assertEqual(self.call(self.admin, "grant.resolve", id=g["id"], decision="denied")["status"], "denied")
        self.assertEqual(self.call(self.sa, "grant.list")["grants"], [])
        self.assertEqual(len(self.call(self.admin, "grant.list")["grants"]), 1)
        self.assertEqual(self.call(self.sa, "grant.list", workspace="beta")["grants"], [])   # forced to own ws

    def test_grants_persist_and_die_with_the_workspace(self):
        g = self.call(self.sa, "grant.request", workspace="alpha", kind="host_clipboard")
        self.call(self.admin, "grant.resolve", id=g["id"], decision="granted", ttl_seconds=600)
        from aw import daemon
        d2 = daemon.Daemon()
        self.assertIsNotNone(d2.grants.is_granted(self.a["id"], "host_clipboard"))
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.base, "grants.json")).st_mode), 0o600)
        d2.handle("workspace.destroy", {"workspace": "alpha"}, d2.admin_state())
        self.assertIsNone(d2.grants.is_granted(self.a["id"], "host_clipboard"))


class AuditAndLogs(Base):
    def test_audit_has_who_what_which_and_no_secrets(self):
        tok = self.token(self.a)
        self.code(self.sa, "screen.screenshot", workspace="beta")
        self.call(self.admin, "workspace.pause", workspace="alpha", reason="PRIVATE-REASON-123")
        self.code({}, "input.type", workspace="alpha", text="SECRET-TYPED-TEXT")
        path = os.path.join(self.base, "audit.log")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        blob = open(path).read()
        self.assertIn("auth.denied", blob)
        self.assertIn("workspace.pause", blob)
        for secret in (tok, self.token(self.b), open(os.path.join(self.base, "admin.token")).read().strip(),
                       "PRIVATE-REASON-123", "SECRET-TYPED-TEXT"):
            self.assertNotIn(secret, blob)

    def test_error_messages_never_contain_tokens(self):
        from aw.manager import ManagerError
        tok = self.token(self.b)
        try:
            self.daemon.handle("workspace.get", {"workspace": "beta"}, self.sa)
        except ManagerError as e:
            self.assertNotIn(tok, e.message)
        try:
            self.daemon.creds.authenticate(tok[:-3] + "xyz")
        except Exception as e:
            self.assertNotIn(tok[:20], str(e))


class CredentialLookup(Base):
    def lookup(self, role="auto", workspace=None, env=None, explicit=None):
        from aw import auth
        return auth.find_credentials(self.base, role, workspace, environ=env or {}, explicit=explicit)

    def test_order_agent_role(self):
        ta, tb = self.token(self.a), self.token(self.b)
        self.assertEqual(self.lookup("agent", env={}), [])           # nothing configured: no credential (fail closed)
        self.assertEqual([c.source for c in self.lookup("agent", "alpha")][0].split("/")[-1], "token")
        c = self.lookup("agent", "alpha", explicit="E", env={"AW_TOKEN": "T", "AW_WORKSPACE": "beta"})
        self.assertEqual([x.token for x in c], ["E", "T"])           # configured credentials are final
        c = self.lookup("agent", "alpha", env={"AW_WORKSPACE": "beta"})
        self.assertEqual([x.token for x in c], [ta])                 # addressed workspace beats AW_WORKSPACE
        c = self.lookup("agent", None, env={"AW_WORKSPACE": "beta"})
        self.assertEqual([x.token for x in c], [tb])
        c = self.lookup("agent", None, env={"AW_AGENT_KIND": "codex"})
        self.assertEqual([x.token for x in c], [ta])                 # only workspaces assigned to that kind

    def test_token_file_env_and_insecure_file_refused(self):
        from aw.auth import AuthError
        p = self.daemon.creds.token_path(self.a["id"])
        c = self.lookup("agent", env={"AW_TOKEN_FILE": p})
        self.assertEqual([x.token for x in c], [self.token(self.a)])
        os.chmod(p, 0o640)
        with self.assertRaises(AuthError) as cm:
            self.lookup("agent", env={"AW_TOKEN_FILE": p})
        self.assertEqual(cm.exception.code, "E_INSECURE_CREDENTIAL_FILE")
        with self.assertRaises(AuthError):
            self.lookup("agent", "alpha")                            # file lookup also refuses it

    def test_auto_never_silently_escalates_a_configured_agent_to_admin(self):
        env = {"AW_TOKEN": "agent-token-value"}
        c = self.lookup("auto", env=env)
        self.assertEqual([x.kind for x in c], ["workspace"])
        c = self.lookup("auto", env={})
        self.assertEqual(c[0].kind, "admin")                         # the human CLI
        c = self.lookup("admin", env={})
        self.assertEqual([x.kind for x in c], ["admin"])
        self.assertEqual(self.lookup("agent", "alpha", env={})[0].kind, "workspace")


class RealSocket(Base):
    """The wire: SO_PEERCRED, per-request tokens, login, throttling, event scoping, client credential pickup."""

    def start(self):
        from aw import daemon
        daemon.Handler.daemon = self.daemon
        self.sock = os.path.join(self.base, "api.sock")
        old = os.umask(0o177)
        self.srv = daemon.Server(self.sock, daemon.Handler)
        os.umask(old)
        self.srv.audit = self.daemon.audit
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.srv.shutdown(), self.srv.server_close()))

    def raw(self, *lines):
        s = socket.socket(socket.AF_UNIX)
        s.connect(self.sock)
        s.settimeout(5)
        f = s.makefile("rwb")
        out = []
        for ln in lines:
            f.write((json.dumps(ln) + "\n").encode())
            f.flush()
            out.append(json.loads(f.readline()))
        s.close()
        return out

    def test_unauthenticated_and_wrong_token(self):
        self.start()
        r = self.raw({"v": 2, "id": 1, "method": "workspace.list"},
                     {"v": 2, "id": 2, "method": "workspace.list", "token": "awt1_x_" + "A" * 43})
        self.assertEqual([x["error"]["code"] for x in r], ["E_UNAUTHENTICATED"] * 2)
        self.assertTrue(self.raw({"v": 2, "id": 3, "method": "hello"})[0]["ok"])

    def test_v1_clients_get_a_clear_error(self):
        self.start()
        r = self.raw({"v": 1, "id": 1, "method": "workspace.list", "params": {}})[0]
        self.assertEqual(r["error"]["code"], "E_UNAUTHENTICATED")
        self.assertIn("API v2", r["error"]["message"])

    def test_login_then_scoped_requests_and_stale_after_revoke(self):
        self.start()
        tok = self.token(self.a)
        r = self.raw({"v": 2, "id": 1, "method": "auth.login", "params": {"token": tok}},
                     {"v": 2, "id": 2, "method": "workspace.list"},
                     {"v": 2, "id": 3, "method": "workspace.get", "params": {"workspace": "beta"}})
        self.assertEqual(r[0]["result"]["principal"]["workspace"], self.a["id"])
        self.assertEqual([w["name"] for w in r[1]["result"]["workspaces"]], ["alpha"])
        self.assertEqual(r[2]["error"]["code"], "E_FORBIDDEN")
        self.assertNotIn(tok, json.dumps(r[0]))

    def test_peer_uid_other_than_ours_is_refused(self):
        from aw import auth, daemon
        self.start()
        srv = self.srv
        real = auth.peer_cred
        auth.peer_cred = lambda s: (1234, os.getuid() + 1, 1)
        try:
            self.assertFalse(srv.verify_request(None, None))
        finally:
            auth.peer_cred = real
        a = socket.socket(socket.AF_UNIX)
        a.connect(self.sock)
        self.assertEqual(auth.peer_cred(a)[1], os.getuid())
        a.close()
        self.assertIn("auth.refused_uid", open(os.path.join(self.base, "audit.log")).read())

    def test_client_picks_up_credentials_automatically(self):
        from aw.client import Client, ApiError
        self.start()
        ta = self.daemon.creds.token_path(self.a["id"])
        # agent configured through the environment only
        os.environ["AW_TOKEN_FILE"] = ta
        try:
            c = Client(self.sock, autostart=False)
            self.assertEqual([w["name"] for w in c.call("workspace.list")["workspaces"]], ["alpha"])
            with self.assertRaises(ApiError) as cm:
                c.call("workspace.get", workspace="beta")
            self.assertEqual(cm.exception.code, "E_FORBIDDEN")
            with self.assertRaises(ApiError) as cm:
                c.call("workspace.create", name="evil")
            self.assertEqual(cm.exception.code, "E_FORBIDDEN")      # AW_TOKEN_FILE never escalates to admin
            self.assertEqual(c.last_credential.kind, "workspace")
        finally:
            os.environ.pop("AW_TOKEN_FILE")
        # the human: nothing set, admin.token in the base dir
        c = Client(self.sock, autostart=False)
        self.assertEqual(len(c.call("workspace.list")["workspaces"]), 2)
        # rotation: the client re-reads the file and keeps working
        os.environ["AW_TOKEN_FILE"] = ta
        try:
            c = Client(self.sock, autostart=False)
            c.call("workspace.get", workspace="alpha")
            self.daemon.handle("credential.rotate", {"workspace": "alpha"}, self.admin)
            self.assertEqual(c.call("workspace.get", workspace="alpha")["name"], "alpha")
        finally:
            os.environ.pop("AW_TOKEN_FILE")

    def test_assigned_agent_finds_its_workspace_without_any_env_token(self):
        from aw.client import Client
        self.start()
        os.environ["AW_AGENT_KIND"] = "hermes"
        try:
            c = Client(self.sock, autostart=False, role="agent")
            self.assertEqual([w["name"] for w in c.call("workspace.list")["workspaces"]], ["beta"])
            self.assertEqual(c.call("workspace.get", workspace="beta")["name"], "beta")
        finally:
            os.environ.pop("AW_AGENT_KIND")

    def test_no_credentials_is_a_clear_client_error(self):
        from aw.client import Client, ApiError
        self.start()
        os.remove(os.path.join(self.base, "admin.token"))
        c = Client(self.sock, autostart=False, role="agent")
        for call in (lambda: c.call("workspace.list"), lambda: c.call("input.click", workspace="")):
            with self.assertRaises(ApiError) as cm:
                call()
            self.assertEqual(cm.exception.code, "E_NO_CREDENTIALS")

    def test_event_stream_is_scoped_to_the_bound_workspace(self):
        from aw.client import Client
        self.start()
        os.environ["AW_TOKEN_FILE"] = self.daemon.creds.token_path(self.a["id"])
        got = []
        try:
            c = Client(self.sock, autostart=False)
            it = c.events()
            threading.Thread(target=lambda: [got.append(e) for e in it], daemon=True).start()
            time.sleep(0.3)
            self.daemon.handle("workspace.rename", {"workspace": "beta", "name": "beta2"}, self.admin)
            self.daemon.handle("workspace.rename", {"workspace": "alpha", "name": "alpha2"}, self.admin)
            time.sleep(0.4)
        finally:
            os.environ.pop("AW_TOKEN_FILE")
        self.assertEqual([e["data"]["id"] for e in got], [self.a["id"]])


if __name__ == "__main__":
    unittest.main()
