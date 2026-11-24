"""UNIT tests of the daemon API without any compositor: workspaces are created with start=False,
so only metadata logic, validation, takeover and assignment rules, framing and events are exercised."""
import json
import os
import queue
import shutil
import socket
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


class Base(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="awu", dir="/tmp")
        os.environ["AW_BASE_DIR"] = self.base
        from aw import daemon, paths
        self.paths = paths
        self.daemon = daemon.Daemon()
        self.st = self.daemon.admin_state()

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)
        os.environ.pop("AW_BASE_DIR", None)

    def call(self, method, **p):
        return self.daemon.handle(method, p, self.st)

    def code(self, method, **p):
        from aw.manager import ManagerError
        with self.assertRaises(ManagerError) as cm:
            self.call(method, **p)
        return cm.exception.code


class WorkspaceMetadata(Base):
    def test_create_list_rename_assign(self):
        w = self.call("workspace.create", name="alpha", start=False, agent_kind="claude-code")
        self.assertEqual(w["state"], "stopped")
        self.assertEqual(w["agent"]["icon_hint"], "claude")
        self.assertEqual(self.call("workspace.list")["workspaces"][0]["name"], "alpha")
        self.assertEqual(self.call("workspace.rename", workspace="alpha", name="beta")["name"], "beta")
        a = self.call("workspace.assign", workspace="beta", agent_kind="codex")
        self.assertEqual(a["agent"]["kind"], "codex")
        self.assertEqual(a["agent"]["icon_hint"], "openai")

    def test_lookup_by_id_and_case_insensitive_name(self):
        w = self.call("workspace.create", name="Alpha", start=False)
        self.assertEqual(self.call("workspace.get", workspace=w["id"])["name"], "Alpha")
        self.assertEqual(self.call("workspace.get", workspace="alpha")["id"], w["id"])

    def test_validation(self):
        self.assertEqual(self.code("workspace.create", name="bad/name", start=False), "E_BAD_NAME")
        self.assertEqual(self.code("workspace.create", name="x", agent_kind="nope", start=False), "E_BAD_AGENT")
        self.assertEqual(self.code("workspace.create", name="x", width=10, start=False), "E_BAD_SIZE")
        self.assertEqual(self.code("workspace.create", name="x", gpu="maybe", start=False), "E_BAD_GPU")
        self.assertEqual(self.code("workspace.create", name="x", clipboard="all", start=False), "E_BAD_CLIPBOARD")
        self.call("workspace.create", name="dup", start=False)
        self.assertEqual(self.code("workspace.create", name="DUP", start=False), "E_EXISTS")

    def test_unknown(self):
        self.assertEqual(self.code("workspace.get", workspace="ghost"), "E_NOT_FOUND")
        self.assertEqual(self.code("nope.nope"), "E_UNKNOWN_METHOD")
        self.assertEqual(self.code("workspace.get"), "E_BAD_ARGS")

    def test_persisted_and_adopted_after_restart(self):
        self.call("workspace.create", name="keep", start=False, agent_kind="hermes")
        from aw import daemon
        d2 = daemon.Daemon()
        names = [w["name"] for w in d2.handle("workspace.list", {}, d2.admin_state())["workspaces"]]
        self.assertEqual(names, ["keep"])

    def test_agent_filter_in_list(self):
        self.call("workspace.create", name="a", start=False, agent_kind="codex")
        self.call("workspace.create", name="b", start=False, agent_kind="claude-code")
        self.call("workspace.create", name="c", start=False)
        names = sorted(w["name"] for w in self.call("workspace.list", agent_kind="codex")["workspaces"])
        self.assertEqual(names, ["a", "c"])  # own + unassigned, never someone else's


class TakeoverAndAssignment(Base):
    def setUp(self):
        super().setUp()
        self.call("workspace.create", name="w", start=False, agent_kind="claude-code")

    def test_pause_rejects_agent_input_before_any_compositor_access(self):
        self.call("workspace.pause", workspace="w", reason="user typing")
        for m, p in (("input.click", {"x": 1, "y": 1}), ("input.type", {"text": "x"}), ("input.key", {"combo": "a"}),
                     ("input.move", {"x": 1, "y": 1}), ("input.scroll", {"dy": 1}),
                     ("input.drag", {"x1": 0, "y1": 0, "x2": 1, "y2": 1}),
                     ("window.action", {"window": 1, "action": "close"}), ("clipboard.set", {"text": "x"})):
            self.assertEqual(self.code(m, workspace="w", agent_kind="claude-code", **p), "E_PAUSED", m)
            self.assertEqual(self.code(m, workspace="w", **p), "E_PAUSED", m + " (anonymous caller)")

    def test_human_not_blocked_by_pause(self):
        self.call("workspace.pause", workspace="w")
        # human passes the guard and only then hits "not running" (no compositor in this unit test)
        self.assertEqual(self.code("input.click", workspace="w", agent_kind="human", x=1, y=1), "E_NOT_RUNNING")

    def test_resume_lifts_pause(self):
        self.call("workspace.pause", workspace="w")
        self.call("workspace.resume", workspace="w")
        self.assertEqual(self.code("input.click", workspace="w", agent_kind="claude-code", x=1, y=1),
                         "E_NOT_RUNNING")

    def test_assignment_enforced(self):
        self.assertEqual(self.code("input.click", workspace="w", agent_kind="codex", x=1, y=1), "E_ASSIGNMENT")
        self.assertEqual(self.code("workspace.launch", workspace="w", agent_kind="codex", argv=["x"]), "E_ASSIGNMENT")
        self.assertEqual(self.code("screen.screenshot", workspace="w", agent_kind="hermes"), "E_ASSIGNMENT")

    def test_unassigned_workspace_open_to_any_agent_kind(self):
        self.call("workspace.create", name="free", start=False)
        self.assertEqual(self.code("input.click", workspace="free", agent_kind="codex", x=1, y=1), "E_NOT_RUNNING")

    def test_launch_on_stopped_workspace_fails_closed(self):
        self.assertEqual(self.code("workspace.launch", workspace="w", argv=["true"]), "E_NOT_RUNNING")

    def test_launch_bad_argv(self):
        self.assertEqual(self.code("workspace.launch", workspace="w", argv="ls"), "E_BAD_ARGS")

    def test_input_mode_event(self):
        q = queue.Queue()
        self.daemon.bus.subscribe(q, None, ["workspace.input_mode"])
        self.call("workspace.pause", workspace="w", reason="r")
        ev = q.get(timeout=1)
        self.assertEqual(ev["event"], "workspace.input_mode")
        self.assertEqual(ev["data"]["input_mode"], "manual")
        self.assertTrue(self.call("workspace.pause", workspace="w")["input_mode"] == "manual")
        self.assertTrue(q.empty())  # no duplicate event when nothing changed


class Approvals(Base):
    def test_flow(self):
        self.call("workspace.create", name="w", start=False, agent_kind="codex")
        a = self.call("approval.request", workspace="w", agent_kind="codex", title="Need 2FA code")
        self.assertEqual(a["status"], "pending")
        self.assertEqual(self.call("workspace.list")["workspaces"][0]["pending_approvals"], 1)
        r = self.call("approval.resolve", id=a["id"], decision="approved")
        self.assertEqual(r["status"], "approved")
        self.assertEqual(self.call("approval.list", pending=True)["approvals"], [])
        self.assertEqual(self.code("approval.resolve", id=a["id"], decision="maybe"), "E_BAD_ARGS")
        self.assertEqual(self.code("approval.request", workspace="w", agent_kind="claude-code", title="x"),
                         "E_ASSIGNMENT")


class EventBusFilters(Base):
    def test_workspace_and_type_filter(self):
        a = self.call("workspace.create", name="a", start=False)
        q = queue.Queue()
        self.daemon.bus.subscribe(q, [a["id"]], ["workspace.renamed"])
        self.call("workspace.create", name="b", start=False)
        self.call("workspace.rename", workspace="b", name="bb")      # other workspace: filtered out
        self.call("workspace.assign", workspace="a", agent_kind="codex")  # other type: filtered out
        self.call("workspace.rename", workspace="a", name="aa")
        ev = q.get(timeout=1)
        self.assertEqual((ev["event"], ev["data"]["id"]), ("workspace.renamed", a["id"]))
        self.assertTrue(q.empty())


class SocketFraming(Base):
    def test_real_socket_roundtrip_and_event_stream(self):
        from aw import daemon
        daemon.Handler.daemon = self.daemon
        sock = os.path.join(self.base, "api.sock")
        srv = daemon.Server(sock, daemon.Handler)
        os.chmod(sock, 0o600)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            self.assertEqual(oct(os.stat(sock).st_mode & 0o777), "0o600")
            from aw.client import Client, ApiError
            c = Client(sock, autostart=False)
            self.assertEqual(c.call("hello")["api_version"], 2)
            w = c.call("workspace.create", name="s", start=False)
            self.assertEqual(w["name"], "s")
            with self.assertRaises(ApiError) as cm:
                c.call("workspace.get", workspace="ghost")
            self.assertEqual(cm.exception.code, "E_NOT_FOUND")
            # event stream on a second connection
            c2 = Client(sock, autostart=False)
            it = c2.events(types=["workspace.*"])
            got = []
            t = threading.Thread(target=lambda: got.append(next(it)), daemon=True)
            t.start()
            import time
            time.sleep(0.2)
            c.call("workspace.rename", workspace="s", name="s2")
            t.join(2)
            self.assertEqual(got[0]["event"], "workspace.renamed")
            # malformed JSON does not kill the connection
            raw = socket.socket(socket.AF_UNIX)
            raw.connect(sock)
            raw.sendall(b"not json\n" + json.dumps({"v": 1, "id": 1, "method": "hello"}).encode() + b"\n")
            f = raw.makefile("rb")
            self.assertEqual(json.loads(f.readline())["error"]["code"], "E_BAD_REQUEST")
            self.assertTrue(json.loads(f.readline())["ok"])
            raw.close()
        finally:
            srv.shutdown()
            srv.server_close()

    def test_viewer_attach_counts_per_connection(self):
        self.call("workspace.create", name="v", start=False)
        st = self.daemon.admin_state()
        self.daemon.handle("viewer.attach", {"workspace": "v"}, st)
        self.assertEqual(self.daemon.mgr.get("v").viewers, 1)


if __name__ == "__main__":
    unittest.main()
