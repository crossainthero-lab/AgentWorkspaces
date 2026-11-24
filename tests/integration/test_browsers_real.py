"""REAL INTEGRATION TESTS: browsers in workspaces (real Google Chrome / Firefox, real compositors, a local web
server). Nothing here reads or writes the user's real browser profiles: the daemon and the CLI run with a
throw-away fake HOME (with its own XDG dirs), "host" profiles are fixtures that a real browser creates inside
that fake home, and every GUI program runs only inside a workspace compositor.

Firefox needs a non-snap build: set AW_TEST_FIREFOX=/path/to/firefox (a vendor tarball); the Ubuntu snap is
refused by design (see test_snap_firefox_is_refused).
"""
import hashlib
import json
import os
import pty
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as K  # noqa: E402
from aw.client import ApiError  # noqa: E402
from apps.web_probe import WebProbe  # noqa: E402

C = None
WEB = None
FAKE_HOME = None
FF = os.environ.get("AW_TEST_FIREFOX")


def setUpModule():
    global C, WEB, FAKE_HOME
    if not K.have("labwc"):
        raise unittest.SkipTest("labwc not installed")
    FAKE_HOME = tempfile.mkdtemp(prefix="awt-fakehome-", dir=f"/run/user/{os.getuid()}")
    os.chmod(FAKE_HOME, 0o700)
    extra = {}
    if FF and os.access(FF, os.X_OK):
        extra["AW_BROWSER_EXTRA"] = json.dumps([{"id": "firefox-tarball", "label": "Firefox (tarball)",
                                                "family": "firefox", "exe": FF}])
    C = K.start_daemon("brw", home=FAKE_HOME, extra_env=extra)
    WEB = WebProbe(os.path.join(K.BASE, "web.log")).start()


def tearDownModule():
    K.stop_daemon(C)
    if WEB:
        WEB.stop()
    if FAKE_HOME:
        shutil.rmtree(FAKE_HOME, ignore_errors=True)


def run_cli_in_pty(args, answers=(), timeout=30):
    """Run `agent-workspaces ARGS` with a real controlling terminal (as the human would), feeding `answers` to
    its prompts. -> (exit code, output). HOME is the fake home."""
    env = {"PATH": os.environ["PATH"], "HOME": FAKE_HOME, "AW_BASE_DIR": K.BASE, "PYTHONPATH": K.ROOT,
           "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}", "LANG": "C.UTF-8", "AW_NO_AUTOSTART": "1",
           "XDG_DATA_HOME": os.path.join(FAKE_HOME, ".local/share"), "TERM": "dumb"}
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(K.ROOT)
        os.execve(sys.executable, [sys.executable, "-m", "aw"] + list(args), env)
    out, answers = b"", list(answers)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
            if (b"? [y/N]" in chunk or b"anything else cancels:" in chunk) and answers:
                os.write(fd, (answers.pop(0) + "\n").encode())
    try:
        _, status = os.waitpid(pid, 0)
    except ChildProcessError:
        status = 0
    return os.waitstatus_to_exitcode(status), out.decode(errors="replace")


def run_cli_no_tty(args, timeout=30):
    env = {"PATH": os.environ["PATH"], "HOME": FAKE_HOME, "AW_BASE_DIR": K.BASE, "PYTHONPATH": K.ROOT,
           "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}", "LANG": "C.UTF-8", "AW_NO_AUTOSTART": "1"}
    r = subprocess.run([sys.executable, "-m", "aw"] + list(args), env=env, cwd=K.ROOT, capture_output=True,
                       text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    return r.returncode, r.stdout + r.stderr


def tree_fingerprint(root):
    h = hashlib.sha256()
    for dp, dn, fn in sorted(os.walk(root)):
        dn.sort()
        for n in sorted(fn):
            p = os.path.join(dp, n)
            st = os.lstat(p)
            h.update(f"{os.path.relpath(p, root)}|{st.st_size}|{st.st_mtime_ns}|{st.st_mode}".encode())
            if stat.S_ISREG(st.st_mode):
                with open(p, "rb") as f:
                    h.update(hashlib.sha256(f.read()).digest())
    return h.hexdigest()


def close_browser(ws, app_n, timeout=25):
    """Close every window (retrying: a browser that is still starting ignores an early close request)."""
    def gone():
        return not any(a["n"] == app_n and a["running"] for a in C.call("app.list", workspace=ws)["apps"])
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if gone():
            return True
        for w in C.call("window.list", workspace=ws)["windows"]:
            try:
                C.call("window.action", workspace=ws, window=w["id"], action="close")
            except ApiError:
                pass
        K.wait_for(gone, 3)
    return gone()


def web_events(kind=None):
    return [e for e in WEB.events() if kind is None or e.get("ev") == kind]


@unittest.skipUnless(K.have("google-chrome"), "google-chrome not installed")
class ChromeProfiles(unittest.TestCase):
    def test_discovery_uses_the_fake_home_and_lists_names_only(self):
        C.call("workspace.create", name="disc", width=800, height=500)
        try:
            res = C.call("browser.list", workspace="disc")
            ids = {b["id"]: b for b in res["browsers"]}
            self.assertIn("google-chrome", ids)
            self.assertTrue(ids["google-chrome"]["launchable"])
            for pr in ids["google-chrome"]["profiles"]:                  # names/ids only
                self.assertEqual(set(pr), {"id", "name"})
            self.assertNotIn(FAKE_HOME, json.dumps(res))                 # no filesystem paths in the answer
            self.assertNotIn(os.path.expanduser("~/.config"), json.dumps(res))
        finally:
            C.call("workspace.destroy", workspace="disc")

    def test_throwaway_chrome_native_wayland_input_and_no_debug_port(self):
        w = C.call("workspace.create", name="thr", width=1200, height=700)
        try:
            n0 = len(web_events("loaded"))
            r = C.call("browser.launch", workspace="thr", browser="google-chrome", url=WEB.url + "/?b=THROW")
            self.assertEqual(r["profile"]["mode"], "throwaway")
            self.assertTrue(K.wait_for(lambda: len(web_events("loaded")) > n0, 25), "page never loaded")
            time.sleep(1.5)
            # the browser command line: profile inside the workspace dir, wayland, no automation flags
            with open(f"/proc/{r['pid']}/cmdline", "rb") as f:
                cmd = f.read().decode().replace("\0", " ").split(" ")      # chrome rewrites argv into one string
            self.assertIn("--ozone-platform=wayland", cmd)
            udd = [c for c in cmd if c.startswith("--user-data-dir=")][0].split("=", 1)[1]
            self.assertTrue(udd.startswith(os.path.join(K.BASE, w["id"]) + "/"), udd)
            self.assertFalse([c for c in cmd if "remote-debugging" in c or "load-extension" in c])
            # no process of the workspace listens on a TCP port (no debugging port, nothing exposed to the host)
            slice_ = subprocess.run(["systemctl", "--user", "show", f"aw-{w['id']}.slice", "-p", "ControlGroup"],
                                    capture_output=True, text=True).stdout
            ss = subprocess.run(["ss", "-ltnpH"], capture_output=True, text=True).stdout
            # `ss -p` lists pids; none of the workspace's pids may be in a listening TCP socket
            cg = slice_.strip().split("=", 1)[1] if "=" in slice_ else ""
            procs = set()
            for root, dirs, files in os.walk("/sys/fs/cgroup" + cg):
                if "cgroup.procs" in files:
                    procs |= set(open(os.path.join(root, "cgroup.procs")).read().split())
            self.assertTrue(procs, "could not read the workspace cgroup")
            for line in ss.splitlines():
                for p in procs:
                    self.assertNotIn(f"pid={p},", line, f"workspace process {p} listens: {line}")
            # real input reaches the page: click the field, type, click the button
            wins = C.call("window.list", workspace="thr")["windows"]
            self.assertTrue(any("THROW" in x["title"] for x in wins), wins)
            C.call("input.click", workspace="thr", x=200, y=224)
            C.call("input.type", workspace="thr", text="hello chrome")
            C.call("input.click", workspace="thr", x=70, y=284)
            self.assertTrue(K.wait_for(lambda: any(e["v"] == "hello chrome" for e in web_events("text")), 5))
            self.assertTrue(K.wait_for(lambda: web_events("click"), 5))
            self.assertTrue(os.path.isdir(udd))
            C.call("workspace.destroy", workspace="thr")
            self.assertFalse(os.path.exists(udd), "throwaway profile must be deleted with the workspace")
            w = None
        finally:
            if w:
                C.call("workspace.destroy", workspace="thr")

    def test_agent_profile_keeps_the_session_and_is_never_shared_by_two_workspaces(self):
        C.call("workspace.create", name="ag1", width=900, height=600)
        C.call("workspace.create", name="ag2", width=900, height=600)
        try:
            n0 = len(web_events("loaded"))
            a = C.call("browser.launch", workspace="ag1", browser="google-chrome",
                       profile={"mode": "agent", "name": "persist"}, url=WEB.url + "/login?token=agent-session-1")
            time.sleep(4)
            # a second workspace must not open the same writable profile while ag1's browser runs
            with self.assertRaises(ApiError) as cm:
                C.call("browser.launch", workspace="ag2", browser="google-chrome",
                       profile={"mode": "agent", "name": "persist"}, url=WEB.url + "/ping")
            self.assertEqual(cm.exception.code, "E_PROFILE_BUSY")
            self.assertTrue(close_browser("ag1", a["app"]), "browser did not exit")
            time.sleep(1)
            # restart in the same workspace: the cookie set before is still there
            nw = len(web_events("whoami"))
            a2 = C.call("browser.launch", workspace="ag1", browser="google-chrome",
                        profile={"mode": "agent", "name": "persist"}, url=WEB.url + "/whoami")
            self.assertTrue(K.wait_for(lambda: any(e["v"] == "agent-session-1" for e in web_events("whoami")[nw:]), 20),
                            web_events("whoami"))
            ok = close_browser("ag1", a2["app"])
            self.assertTrue(ok, (C.call("app.list", workspace="ag1"), C.call("window.list", workspace="ag1")))
            # after ag1's browser exited, ag2 may take the profile over (persisted across workspaces)
            nw = len(web_events("whoami"))
            b = C.call("browser.launch", workspace="ag2", browser="google-chrome",
                       profile={"mode": "agent", "name": "persist"}, url=WEB.url + "/whoami")
            self.assertTrue(K.wait_for(lambda: any(e["v"] == "agent-session-1" for e in web_events("whoami")[nw:]), 20))
        finally:
            C.call("workspace.destroy", workspace="ag1")
            C.call("workspace.destroy", workspace="ag2")
            shutil.rmtree(os.path.join(FAKE_HOME, ".local/share/agent-workspaces/browser-profiles"), ignore_errors=True)

    def test_refused_arguments_and_urls(self):
        C.call("workspace.create", name="deny", width=800, height=500)
        try:
            for bad in ("--remote-debugging-port=9222", "--remote-debugging-pipe", "--user-data-dir=/tmp/x",
                        "--load-extension=/tmp/e", "--profile-directory=Default", "--ozone-platform=x11"):
                with self.assertRaises(ApiError) as cm:
                    C.call("browser.launch", workspace="deny", browser="google-chrome", args=[bad])
                self.assertEqual(cm.exception.code, "E_BAD_BROWSER_ARG", bad)
            with self.assertRaises(ApiError) as cm:
                C.call("browser.launch", workspace="deny", browser="google-chrome", url="javascript:alert(1)")
            self.assertEqual(cm.exception.code, "E_BAD_URL")
            with self.assertRaises(ApiError) as cm:
                C.call("browser.launch", workspace="deny", browser="google-chrome",
                       profile={"mode": "agent", "name": "../../etc"})
            self.assertEqual(cm.exception.code, "E_BAD_NAME")
            self.assertEqual(C.call("workspace.get", workspace="deny")["apps"], [])      # nothing was started
        finally:
            C.call("workspace.destroy", workspace="deny")


@unittest.skipUnless(K.have("google-chrome"), "google-chrome not installed")
class AuthorizedProfile(unittest.TestCase):
    """The only way an agent can use a signed-in profile of the user: the human authorises a COPY."""

    HOST_ROOT = None

    @classmethod
    def setUpClass(cls):
        cls.HOST_ROOT = os.path.join(FAKE_HOME, ".config", "google-chrome")
        C.call("workspace.create", name="fixture", width=900, height=600)
        try:
            # build a "host" profile with a real Chrome: sign in (cookie) and close cleanly
            r = C.call("workspace.launch", workspace="fixture", argv=[
                "google-chrome", f"--user-data-dir={cls.HOST_ROOT}", "--ozone-platform=wayland", "--no-first-run",
                "--no-default-browser-check", "--password-store=basic", "--disable-sync",
                WEB.url + "/login?token=user-session-77"])
            time.sleep(5)
            assert close_browser("fixture", r["app"]), "fixture browser did not exit"
            time.sleep(1)
        finally:
            C.call("workspace.destroy", workspace="fixture")
        assert os.path.isfile(os.path.join(cls.HOST_ROOT, "Local State")), "fixture profile was not created"

    def setUp(self):
        C.call("workspace.create", name="auth", width=1000, height=640)

    def tearDown(self):
        try:
            C.call("workspace.destroy", workspace="auth")
        except ApiError:
            pass

    def test_agent_can_only_request_and_cannot_use_without_grant(self):
        info = C.call("browser.list", workspace="auth")
        prof = [b for b in info["browsers"] if b["id"] == "google-chrome"][0]["profiles"]
        self.assertEqual([p["id"] for p in prof], ["Default"])
        # launching an authorised profile without a grant is refused
        with self.assertRaises(ApiError) as cm:
            C.call("browser.launch", workspace="auth", browser="google-chrome",
                   profile={"mode": "authorized", "name": "Default"})
        self.assertEqual(cm.exception.code, "E_GRANT_REQUIRED")
        # an agent credential can request ...
        tokfile = os.path.join(K.BASE, C.call("workspace.get", workspace="auth")["id"], "token")
        from aw.client import Client
        agent = Client(autostart=False, role="agent", token=open(tokfile).read().strip())
        req = agent.call("browser.profile.request", workspace="auth", browser="google-chrome", profile="Default",
                         reason="check my account")
        self.assertEqual(req["status"], "pending")
        self.assertIn("profile authorize", req["how"])
        # ... but can never authorise or revoke, neither directly nor by self-approving the grant
        for method, params in (("browser.profile.authorize", {"browser": "google-chrome", "profile": "Default"}),
                               ("browser.profile.revoke", {"browser": "google-chrome", "profile": "Default"}),
                               ("grant.resolve", {"id": req["id"], "decision": "granted"})):
            with self.assertRaises(ApiError) as cm:
                agent.call(method, workspace="auth", **params)
            self.assertEqual(cm.exception.code, "E_FORBIDDEN", method)
        # even the admin credential over a non-terminal connection is refused (needs a human channel)
        with self.assertRaises(ApiError) as cm:
            C.call("browser.profile.authorize", workspace="auth", browser="google-chrome", profile="Default")
        self.assertEqual(cm.exception.code, "E_NOT_HUMAN_CHANNEL")
        # and the CLI refuses without a terminal
        rc, out = run_cli_no_tty(["profile", "authorize", "google-chrome", "Default", "--workspace", "auth"])
        self.assertEqual(rc, 2, out)
        self.assertIn("interactive terminal", out)

    def test_host_profile_open_in_the_users_browser_is_never_copied_nor_unlocked(self):
        lock = os.path.join(self.HOST_ROOT, "SingletonLock")
        sleeper = subprocess.Popen(["sleep", "60"])
        try:
            os.symlink(f"fakehost-{sleeper.pid}", lock)            # a live owner
            before = tree_fingerprint(self.HOST_ROOT)
            rc, out = run_cli_in_pty(["profile", "authorize", "google-chrome", "Default", "--workspace", "auth"],
                                     answers=["auth"])
            self.assertNotEqual(rc, 0, out)
            self.assertIn("appears to have this profile open", out)
            self.assertTrue(os.path.islink(lock), "lock must not be removed")
            sleeper.terminate()
            sleeper.wait()
            rc, out = run_cli_in_pty(["profile", "authorize", "google-chrome", "Default", "--workspace", "auth"],
                                     answers=["auth"])
            self.assertNotEqual(rc, 0, out)                          # a stale lock is not treated as safe either
            self.assertTrue(os.path.islink(lock), "a stale lock must not be removed either")
            self.assertEqual(tree_fingerprint(self.HOST_ROOT), before, "original profile was modified")
            self.assertEqual(C.call("browser.profile.list", workspace="auth")["authorized"], [])
            self.assertFalse(os.path.exists(os.path.join(FAKE_HOME, ".local/share/agent-workspaces/authorized")) and
                             os.listdir(os.path.join(FAKE_HOME, ".local/share/agent-workspaces/authorized")))
        finally:
            sleeper.kill()
            try:
                os.unlink(lock)
            except OSError:
                pass

    def test_authorize_use_existing_session_revoke(self):
        before = tree_fingerprint(self.HOST_ROOT)
        rc, out = run_cli_in_pty(["profile", "authorize", "google-chrome", "Default", "--workspace", "auth"],
                                 answers=["wrong-name"])
        self.assertEqual(rc, 1, out)                                  # confirmation requires the workspace name
        self.assertIn("cancelled", out)
        rc, out = run_cli_in_pty(["profile", "authorize", "google-chrome", "Default", "--workspace", "auth"],
                                 answers=["auth"])
        self.assertEqual(rc, 0, out)
        self.assertIn("Will NOT copy", out)
        wid = C.call("workspace.get", workspace="auth")["id"]
        clone = os.path.join(FAKE_HOME, ".local/share/agent-workspaces/authorized", wid, "google-chrome__Default")
        self.assertTrue(os.path.isdir(clone))
        self.assertEqual(stat.S_IMODE(os.stat(clone).st_mode), 0o700)
        names = {n for _, _, fs in os.walk(clone) for n in fs}
        self.assertNotIn("Login Data", names)
        self.assertNotIn("Web Data", names)
        self.assertTrue(names & {"Cookies"}, names)
        self.assertEqual(tree_fingerprint(self.HOST_ROOT), before, "the original profile was modified")
        # the consent is in the audit log (names only)
        log = open(os.path.join(K.BASE, "audit.log")).read()
        self.assertIn("browser.profile.cloned", log)
        self.assertNotIn("user-session-77", log)
        # the existing session works inside the workspace
        nw = len(web_events("whoami"))
        r = C.call("browser.launch", workspace="auth", browser="google-chrome",
                   profile={"mode": "authorized", "name": "Default"}, url=WEB.url + "/whoami")
        self.assertTrue(K.wait_for(lambda: any(e["v"] == "user-session-77" for e in web_events("whoami")[nw:]), 25),
                        web_events("whoami")[nw:])
        # revoke: the copy is overwritten and removed, the browser using it is closed, launching is refused
        rc, out = run_cli_in_pty(["profile", "revoke", "google-chrome", "Default", "--workspace", "auth"],
                                 answers=["y"])
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(clone))
        self.assertTrue(K.wait_for(lambda: not any(a["running"] for a in C.call("app.list", workspace="auth")["apps"]), 10))
        with self.assertRaises(ApiError) as cm:
            C.call("browser.launch", workspace="auth", browser="google-chrome",
                   profile={"mode": "authorized", "name": "Default"})
        self.assertIn(cm.exception.code, ("E_GRANT_REQUIRED", "E_NOT_FOUND"))
        self.assertEqual(tree_fingerprint(self.HOST_ROOT), before)

    def test_generic_grant_flow_denial_approval_expiry_and_revocation(self):
        """The generic grants of the security layer (`grant.request` by the agent, `grants approve|deny|revoke`
        by the human in a real pseudo-terminal) drive the same profile copy: no grant -> refused; denied ->
        refused; approved -> the copy is made at launch and the session works; expired -> refused and the copy
        is removed; revoked -> removed at once."""
        from aw.client import Client
        wid = C.call("workspace.get", workspace="auth")["id"]
        with open(os.path.join(K.BASE, wid, "token")) as f:
            agent = Client(autostart=False, role="agent", token=f.read().strip())
        clone = os.path.join(FAKE_HOME, ".local/share/agent-workspaces/authorized", wid, "google-chrome__Default")
        prof = {"mode": "authorized", "name": "Default"}

        def launch(url=None):
            return agent.call("browser.launch", workspace="auth", browser="google-chrome", profile=prof, url=url)

        def refused():
            with self.assertRaises(ApiError) as cm:
                launch()
            self.assertEqual(cm.exception.code, "E_GRANT_REQUIRED")

        # 1. nothing granted
        refused()
        # 2. requested, still pending: refused; the agent cannot resolve it
        g1 = agent.call("grant.request", workspace="auth", kind="browser_profile",
                        resource="google-chrome:Default", detail="read my mail")
        self.assertEqual(g1["status"], "pending")
        refused()
        with self.assertRaises(ApiError):
            agent.call("grant.resolve", id=g1["id"], decision="granted")
        # 3. the human denies in a terminal
        rc, out = run_cli_in_pty(["grants", "deny", g1["id"]], answers=["no"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(agent.call("grant.check", workspace="auth", kind="browser_profile",
                                    resource="google-chrome:Default")["granted"], False)
        refused()
        self.assertFalse(os.path.exists(clone))
        # 4. a wildcard grant (no resource) is not accepted for a profile
        gw = agent.call("grant.request", workspace="auth", kind="browser_profile", resource="")
        rc, out = run_cli_in_pty(["grants", "approve", gw["id"], "--ttl", "60"], answers=["yes"])
        self.assertEqual(rc, 0, out)
        refused()
        rc, out = run_cli_in_pty(["grants", "revoke", gw["id"]], answers=["revoke"])
        self.assertEqual(rc, 0, out)
        # 5. approved for 4 seconds: the copy is made at launch and the session works
        g2 = agent.call("grant.request", workspace="auth", kind="browser_profile", resource="google-chrome:Default")
        rc, out = run_cli_in_pty(["grants", "approve", g2["id"], "--ttl", "20"], answers=["yes"])
        self.assertEqual(rc, 0, out)
        nw = len(web_events("whoami"))
        r = launch(WEB.url + "/whoami")
        self.assertTrue(K.wait_for(lambda: any(e["v"] == "user-session-77" for e in web_events("whoami")[nw:]), 25))
        self.assertTrue(os.path.isdir(clone))
        self.assertTrue(close_browser("auth", r["app"]))
        # 6. expiry: refused, and the copy is removed
        K.wait_for(lambda: agent.call("grant.check", workspace="auth", kind="browser_profile",
                                      resource="google-chrome:Default")["granted"] is False, 30, 1)
        refused()
        self.assertTrue(K.wait_for(lambda: not os.path.exists(clone), 5), "expired copy must be removed")
        # 7. approved again, then revoked by the human: the copy goes at once
        g3 = agent.call("grant.request", workspace="auth", kind="browser_profile", resource="google-chrome:Default")
        rc, out = run_cli_in_pty(["grants", "approve", g3["id"]], answers=["yes"])
        self.assertEqual(rc, 0, out)
        r = launch(WEB.url + "/ping")
        self.assertTrue(K.wait_for(lambda: os.path.isdir(clone), 5))
        rc, out = run_cli_in_pty(["grants", "revoke", g3["id"]], answers=["revoke"])
        self.assertEqual(rc, 0, out)
        self.assertTrue(K.wait_for(lambda: not os.path.exists(clone), 5), "revoked copy must be removed")
        refused()
        log = open(os.path.join(K.BASE, "audit.log")).read()
        for ev in ("grant.requested", "grant.denied", "grant.granted", "grant.revoked", "grant.refused_use",
                   "browser.profile.cloned", "browser.profile.shredded"):
            self.assertIn(ev, log)

    def test_destroying_the_workspace_removes_the_copy(self):
        rc, out = run_cli_in_pty(["profile", "authorize", "google-chrome", "Default", "--workspace", "auth"],
                                 answers=["auth"])
        self.assertEqual(rc, 0, out)
        wid = C.call("workspace.get", workspace="auth")["id"]
        d = os.path.join(FAKE_HOME, ".local/share/agent-workspaces/authorized", wid)
        self.assertTrue(os.path.isdir(d))
        C.call("workspace.destroy", workspace="auth")
        self.assertFalse(os.path.exists(d))


@unittest.skipUnless(FF and os.access(FF or "", os.X_OK), "set AW_TEST_FIREFOX to a non-snap Firefox binary")
class FirefoxProfiles(unittest.TestCase):
    def test_firefox_throwaway_and_agent_profile_persistence(self):
        C.call("workspace.create", name="ff", width=1200, height=760)
        try:
            ids = [b["id"] for b in C.call("browser.list", workspace="ff")["browsers"]]
            self.assertIn("firefox-tarball", ids)
            n0 = len(web_events("loaded"))
            r = C.call("browser.launch", workspace="ff", browser="firefox-tarball",
                       profile={"mode": "agent", "name": "ffp"}, url=WEB.url + "/login?token=ff-session-5")
            self.assertTrue(K.wait_for(lambda: C.call("window.list", workspace="ff")["windows"], 30))
            time.sleep(5)
            with open(f"/proc/{r['pid']}/cmdline", "rb") as f:
                cmd = f.read().decode().replace("\0", " ").split(" ")
            self.assertIn("--no-remote", cmd)
            self.assertFalse([c for c in cmd if "marionette" in c or "remote-debugging" in c])
            # Firefox 160 shows a first-run terms notice: the human-equivalent click is "Continue"
            png = C.call("screen.screenshot", workspace="ff", inline=True)
            self.assertTrue(close_browser("ff", r["app"], 30), "firefox did not exit")
            time.sleep(1)
            nw = len(web_events("whoami"))
            r2 = C.call("browser.launch", workspace="ff", browser="firefox-tarball",
                        profile={"mode": "agent", "name": "ffp"}, url=WEB.url + "/whoami")
            self.assertTrue(K.wait_for(lambda: any(e["v"] == "ff-session-5" for e in web_events("whoami")[nw:]), 30),
                            web_events("whoami")[nw:])
        finally:
            C.call("workspace.destroy", workspace="ff")
            shutil.rmtree(os.path.join(FAKE_HOME, ".local/share/agent-workspaces/browser-profiles"), ignore_errors=True)


class SnapFirefox(unittest.TestCase):
    @unittest.skipUnless(os.path.exists("/snap/bin/firefox"), "no snap firefox installed")
    def test_snap_firefox_is_refused(self):
        C.call("workspace.create", name="snap", width=800, height=500)
        try:
            ff = [b for b in C.call("browser.list", workspace="snap")["browsers"] if b["id"] == "firefox"]
            self.assertTrue(ff and not ff[0]["launchable"])
            self.assertEqual(ff[0]["variant"], "snap")
            with self.assertRaises(ApiError) as cm:
                C.call("browser.launch", workspace="snap", browser="firefox")
            self.assertEqual(cm.exception.code, "E_BROWSER_UNSUPPORTED")
            self.assertEqual(C.call("workspace.get", workspace="snap")["apps"], [])
        finally:
            C.call("workspace.destroy", workspace="snap")


if __name__ == "__main__":
    unittest.main()
