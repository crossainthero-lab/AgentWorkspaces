"""Unit tests (no compositor, no browser): discovery, argument policy, profile locks, clones, shredding, auth
policy of the browser/window/audio methods. Everything runs in a temporary fake HOME; the real home and the
real browser profiles are never read."""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from aw import auth, browsers as B  # noqa: E402
from aw.policy_ext import POLICY_EXT  # noqa: E402


def fingerprint(root):
    h = hashlib.sha256()
    for dp, dn, fn in sorted(os.walk(root)):
        dn.sort()
        for n in sorted(fn):
            p = os.path.join(dp, n)
            st = os.lstat(p)
            h.update(f"{os.path.relpath(p, root)}|{st.st_size}|{st.st_mtime_ns}|{st.st_mode}".encode())
            if stat.S_ISREG(st.st_mode):
                h.update(open(p, "rb").read())
    return h.hexdigest()


class FakeHome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="awbrw-")
        self.home = os.path.join(self.tmp, "home")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.home)
        os.makedirs(self.bin)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def exe(self, name, body="#!/bin/sh\nexec true\n"):
        p = os.path.join(self.bin, name)
        with open(p, "w") as f:
            f.write(body)
        os.chmod(p, 0o755)
        return p

    def chrome_profile(self, rel=".config/google-chrome", pid="Default", name="Work"):
        root = os.path.join(self.home, rel)
        d = os.path.join(root, pid)
        for sub in ("Network", "Local Storage/leveldb", "Extensions/abc", "IndexedDB"):
            os.makedirs(os.path.join(d, sub))
        files = {"Network/Cookies": b"COOKIES", "Network/Cookies-journal": b"J", "Login Data": b"PASSWORDS",
                 "Web Data": b"AUTOFILL", "History": b"HIST", "Preferences": b"{}",
                 "Local Storage/leveldb/000003.log": b"LS", "Extensions/abc/manifest.json": b"{}",
                 "IndexedDB/x": b"idb", "Cache": b"junk"}
        for k, v in files.items():
            open(os.path.join(d, k), "wb").write(v)
        os.makedirs(os.path.join(root), exist_ok=True)
        json.dump({"profile": {"info_cache": {pid: {"name": name, "gaia_name": "SECRET PERSON"}}}},
                  open(os.path.join(root, "Local State"), "w"))
        return root


class Discovery(FakeHome):
    def test_lists_ids_and_names_only(self):
        self.exe("google-chrome")
        root = self.chrome_profile()
        found = B.discover(self.home, path=self.bin, env={})
        b = [x for x in found if x["id"] == "google-chrome"][0]
        self.assertTrue(b["launchable"])
        self.assertEqual([(p["id"], p["name"]) for p in b["profiles"]], [("Default", "Work")])
        view = B.public_view(found)
        text = json.dumps(view)
        self.assertNotIn(self.home, text)
        self.assertNotIn("SECRET PERSON", text)          # nothing but names and ids of profiles
        self.assertEqual(set(view[0]["profiles"][0]), {"id", "name"})
        self.assertNotIn("exe", view[0])
        self.assertEqual(fingerprint(root), fingerprint(root))

    def test_firefox_profiles_ini_and_snap_stub_is_refused(self):
        self.exe("firefox", "#!/bin/sh\nif ! [ -x /snap/bin/firefox ]; then\n echo snap install firefox\nfi\n")
        ff = os.path.join(self.home, ".mozilla/firefox")
        os.makedirs(os.path.join(ff, "abcd.default-release"))
        open(os.path.join(ff, "profiles.ini"), "w").write(
            "[Profile0]\nName=default-release\nIsRelative=1\nPath=abcd.default-release\nDefault=1\n"
            "[Profile1]\nName=evil\nIsRelative=1\nPath=../../etc\n")
        found = B.discover(self.home, path=self.bin, env={})
        f = [x for x in found if x["id"] == "firefox"][0]
        self.assertEqual(f["variant"], "snap")
        self.assertFalse(f["launchable"])
        self.assertIn("snap", f["reason"])
        self.assertEqual([p["id"] for p in f["profiles"]], ["abcd.default-release"])   # traversal entry dropped

    def test_deb_binary_and_extra_entries(self):
        exe = self.exe("my-ff", "#!/bin/sh\nexec true\n")
        extra = json.dumps([{"id": "firefox-tarball", "family": "firefox", "exe": exe},
                            {"id": "../bad", "family": "firefox", "exe": exe},
                            {"id": "evil", "family": "gopher", "exe": exe}])
        found = B.discover(self.home, path=self.bin, env={"AW_BROWSER_EXTRA": extra})
        self.assertIn("firefox-tarball", [b["id"] for b in found])
        self.assertNotIn("../bad", [b["id"] for b in found])
        self.assertNotIn("evil", [b["id"] for b in found])
        self.assertTrue([b for b in found if b["id"] == "firefox-tarball"][0]["launchable"])

    def test_find_browser_aliases(self):
        self.exe("google-chrome")
        found = B.discover(self.home, path=self.bin, env={})
        self.assertEqual(B.find_browser(found, "chrome")["id"], "google-chrome")
        self.assertEqual(B.find_browser(found, "chromium")["id"], "google-chrome")   # family fallback
        with self.assertRaises(B.BrowserError):
            B.find_browser(found, "netscape")


class Arguments(unittest.TestCase):
    def test_automation_and_profile_flags_are_refused(self):
        for bad in ("--remote-debugging-port=9222", "--remote-debugging-pipe", "--REMOTE-DEBUGGING-PORT=1",
                    "--user-data-dir=/x", "--load-extension=/e", "--enable-automation", "--no-sandbox",
                    "--headless=new", "--marionette", "--start-debugger-server=1234", "-profile", "--profile=/x",
                    "--remote-allow-origins=*", "--profile-directory=Other", "--ozone-platform=x11", "--no-remote"):
            with self.assertRaises(B.BrowserError, msg=bad) as cm:
                B.validate_args([bad])
            self.assertEqual(cm.exception.code, "E_BAD_BROWSER_ARG")
        self.assertEqual(B.validate_args(["--lang=de", "--window-size=800,600"]), ["--lang=de", "--window-size=800,600"])
        with self.assertRaises(B.BrowserError):
            B.validate_args([1])

    def test_urls(self):
        self.assertEqual(B.validate_url("https://example.org/a"), "https://example.org/a")
        self.assertIsNone(B.validate_url(None))
        for bad in ("javascript:alert(1)", "chrome://settings", "ftp://x", "data:text/html,x", "http://a\nb"):
            with self.assertRaises(B.BrowserError):
                B.validate_url(bad)

    def test_command_lines(self):
        chrome = {"exe": "/usr/bin/google-chrome", "family": "chromium"}
        a = B.build_command(chrome, "/p", "wayland", "https://x", ["--lang=de"], "Default")
        self.assertEqual(a[0], "/usr/bin/google-chrome")
        for needed in ("--user-data-dir=/p", "--ozone-platform=wayland", "--password-store=basic", "--disable-sync",
                       "--profile-directory=Default"):
            self.assertIn(needed, a)
        self.assertEqual(a[-1], "https://x")
        self.assertFalse([x for x in a if "remote-debugging" in x])
        ff = {"exe": "/opt/firefox/firefox", "family": "firefox"}
        self.assertEqual(B.build_command(ff, "/p", "wayland", None, [])[:4], ["/opt/firefox/firefox", "--no-remote", "--profile", "/p"])
        x = B.build_command(ff, "/p", "x11", None, [])
        self.assertEqual(x[:3], ["/usr/bin/env", "MOZ_ENABLE_WAYLAND=0", "GDK_BACKEND=x11"])
        with self.assertRaises(B.BrowserError):
            B.build_command(ff, "/p", "vnc", None, [])


class Locks(FakeHome):
    def test_chromium_host_lock_states_and_never_removed(self):
        root = self.chrome_profile()
        self.assertEqual(B.host_profile_state("chromium", root, "Default")[0], "closed")
        live = subprocess.Popen(["sleep", "30"])
        try:
            lock = os.path.join(root, "SingletonLock")
            os.symlink(f"somehost-{live.pid}", lock)
            self.assertEqual(B.host_profile_state("chromium", root, "Default")[0], "open")
            live.kill()
            live.wait()
            st, detail = B.host_profile_state("chromium", root, "Default")
            self.assertEqual(st, "stale")
            self.assertIn("not removed", detail)
            self.assertTrue(os.path.islink(lock))
        finally:
            live.kill()

    def test_firefox_symlink_lock_and_parentlock(self):
        root = os.path.join(self.home, ".mozilla/firefox")
        os.makedirs(os.path.join(root, "p.default"))
        self.assertEqual(B.host_profile_state("firefox", root, "p.default")[0], "closed")
        os.symlink(f"127.0.0.1:+{os.getpid()}", os.path.join(root, "p.default", "lock"))
        self.assertEqual(B.host_profile_state("firefox", root, "p.default")[0], "open")

    def test_clone_refused_when_open_and_nothing_is_written(self):
        self.exe("google-chrome")
        root = self.chrome_profile()
        os.symlink(f"h-{os.getpid()}", os.path.join(root, "SingletonLock"))
        before = fingerprint(root)
        b = B.discover(self.home, path=self.bin, env={})[0]
        with self.assertRaises(B.BrowserError) as cm:
            B.clone_profile(b, dict(b["profiles"][0]), os.path.join(self.tmp, "clone"))
        self.assertEqual(cm.exception.code, "E_PROFILE_OPEN_ON_HOST")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "clone")))
        self.assertEqual(fingerprint(root), before)

    def test_agent_profile_ownership(self):
        d = B.agent_dir(os.path.join(self.tmp, "data"), "google-chrome", "p1")
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
        B.claim_profile(d, "wA")
        live = subprocess.Popen(["sleep", "30"])
        try:
            B.bind_pid(d, "wA", "A", live.pid)
            B.claim_profile(d, "wA")                                    # same workspace: fine
            with self.assertRaises(B.BrowserError) as cm:
                B.claim_profile(d, "wB")                                # another workspace while alive: refused
            self.assertEqual(cm.exception.code, "E_PROFILE_BUSY")
            live.kill()
            live.wait()
            B.claim_profile(d, "wB")                                    # owner gone: allowed
        finally:
            live.kill()
        self.assertEqual(B.list_agent_profiles(os.path.join(self.tmp, "data")),
                         [{"browser": "google-chrome", "name": "p1"}])       # the owner record is not a profile
        for bad in ("../x", "a/b", "", ".", "x" * 41, "a b"):
            with self.assertRaises(B.BrowserError):
                B.agent_dir(os.path.join(self.tmp, "data"), "google-chrome", bad)


class Clone(FakeHome):
    def setUp(self):
        super().setUp()
        self.exe("google-chrome")
        self.root = self.chrome_profile()
        self.b = B.discover(self.home, path=self.bin, env={})[0]
        self.prof = dict(self.b["profiles"][0])

    def test_allow_list_copy_never_modifies_the_original(self):
        before = fingerprint(self.root)
        dest = os.path.join(self.tmp, "data", "authorized", "wX", "google-chrome__Default")
        # a symlink inside an allowed directory must not be followed
        secret = os.path.join(self.tmp, "secret.txt")
        open(secret, "w").write("TOPSECRET")
        os.symlink(secret, os.path.join(self.root, "Default", "Local Storage", "leveldb", "link"))
        before = fingerprint(self.root)
        res = B.clone_profile(self.b, self.prof, dest)
        self.assertEqual(fingerprint(self.root), before)
        names = {os.path.relpath(os.path.join(dp, n), dest) for dp, _, fs in os.walk(dest) for n in fs}
        self.assertIn("Default/Network/Cookies", names)
        self.assertIn("Default/Local Storage/leveldb/000003.log", names)
        for forbidden in ("Login Data", "Web Data", "History", "Extensions", "Cache", "link"):
            self.assertFalse([n for n in names if forbidden in n], (forbidden, names))
        self.assertEqual(stat.S_IMODE(os.stat(dest).st_mode), 0o700)
        for dp, dn, fs in os.walk(dest):
            for d in dn:
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(dp, d)).st_mode), 0o700)
            for f in fs:
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(dp, f)).st_mode), 0o600)
        self.assertNotIn("TOPSECRET", b"".join(open(os.path.join(dp, f), "rb").read()
                                               for dp, _, fs in os.walk(dest) for f in fs).decode(errors="ignore"))
        B.check_clone_safe(dest)
        self.assertIn("Network/Cookies", res["items"])
        with self.assertRaises(B.BrowserError) as cm:                   # never overwrites an existing copy
            B.clone_profile(self.b, self.prof, dest)
        self.assertEqual(cm.exception.code, "E_EXISTS")

    def test_unsafe_clone_is_refused(self):
        d = os.path.join(self.tmp, "c")
        os.makedirs(d, mode=0o755)
        os.chmod(d, 0o755)
        with self.assertRaises(B.BrowserError):
            B.check_clone_safe(d)
        link = os.path.join(self.tmp, "l")
        os.symlink(d, link)
        with self.assertRaises(B.BrowserError):
            B.check_clone_safe(link)

    def test_shred_overwrites_the_file_content_before_unlinking(self):
        d = os.path.join(self.tmp, "sh")
        os.makedirs(d)
        f = os.path.join(d, "cookie.db")
        open(f, "wb").write(b"SECRETCOOKIE" * 1000)
        keep = os.path.join(self.tmp, "keepalive-hardlink")
        os.link(f, keep)                      # second name for the same inode: survives the unlink
        os.symlink("/nonexistent", os.path.join(d, "dangling"))
        n = B.shred(d)
        self.assertEqual(n, 1)
        self.assertFalse(os.path.exists(d))
        data = open(keep, "rb").read()
        self.assertEqual(len(data), 12000)
        self.assertEqual(data.strip(b"\0"), b"")                         # zeros, the secret is gone from the inode


class Policy(unittest.TestCase):
    def test_every_new_method_has_a_policy_and_human_methods_are_admin_only(self):
        for m, (target, scope) in POLICY_EXT.items():
            self.assertIn(target, ("ws", "global", "wsadmin", "list"), m)
        agent = auth.Principal("workspace", "w1", auth.AGENT_DEFAULT_SCOPES, "c1")
        admin = auth.Principal("admin", None, auth.SCOPES, "admin")
        auth.POLICY.update(POLICY_EXT)
        res = lambda ref: "w1" if ref in ("w1", "one") else None
        for m in ("browser.profile.authorize", "browser.profile.revoke", "audio.route"):
            with self.assertRaises(auth.AuthError, msg=m) as cm:
                auth.authorize(agent, m, {"workspace": "one"}, res)
            self.assertEqual(cm.exception.code, "E_FORBIDDEN")
            auth.authorize(admin, m, {"workspace": "one"}, res)
        for m in ("browser.list", "browser.launch", "browser.profile.request", "window.move", "window.resize",
                  "window.set_geometry", "audio.info", "audio.record"):
            auth.authorize(agent, m, {"workspace": "one"}, res)
            with self.assertRaises(auth.AuthError):                      # another workspace is never reachable
                auth.authorize(agent, m, {"workspace": "other"}, res)


if __name__ == "__main__":
    unittest.main()
