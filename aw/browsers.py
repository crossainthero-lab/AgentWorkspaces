"""Browsers in a workspace: discovery, profiles, locking and launch command lines (docs/browsers.md).

Principles, enforced here and covered by tests/unit/test_browsers.py + tests/integration/test_browsers_real.py:
  * A browser only ever starts through `Manager.launch`, i.e. with the workspace's private Wayland display,
    private runtime dir/bus/HOME and no host sockets. No remote-debugging port or pipe, no automation
    protocol, no extension is added; arguments that would add one are refused.
  * Profile directories are chosen by AgentWorkspaces, never by the caller. Three modes:
      throwaway   <workspace dir>/browser/throwaway/<browser>   (deleted with the workspace)
      agent       <data dir>/browser-profiles/<browser>/<name>  (kept; one writable owner at a time)
      authorized  <data dir>/authorized/<workspace id>/...      (an allow-listed COPY of one host profile,
                  created only by the human through the CLI, see `clone_profile`)
  * Discovery lists profile NAMES and IDS only. Nothing else of a profile is read, except by
    `clone_profile`, which the human triggers and which never writes to the original.
  * Locks: Chromium `SingletonLock` and Firefox `lock`/`.parentlock` of a host profile are respected and
    never removed, not even when stale. A host profile that looks open is never cloned.
"""
from __future__ import annotations

import configparser
import errno
import json
import os
import re
import secrets
import shutil
import socket
import stat
import time
from typing import Any, Dict, List, Optional, Tuple

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,39}$")


class BrowserError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


# --------------------------------------------------------------------------------- catalogue
# id, label, family, executables (PATH names or absolute), profile roots relative to $HOME
CATALOGUE: List[Dict[str, Any]] = [
    {"id": "google-chrome", "label": "Google Chrome", "family": "chromium",
     "exes": ["google-chrome", "google-chrome-stable", "/opt/google/chrome/chrome"],
     "roots": [".config/google-chrome"]},
    {"id": "google-chrome-beta", "label": "Google Chrome Beta", "family": "chromium",
     "exes": ["google-chrome-beta"], "roots": [".config/google-chrome-beta"]},
    {"id": "google-chrome-unstable", "label": "Google Chrome Dev", "family": "chromium",
     "exes": ["google-chrome-unstable"], "roots": [".config/google-chrome-unstable"]},
    {"id": "chromium", "label": "Chromium", "family": "chromium",
     "exes": ["chromium", "chromium-browser"],
     "roots": [".config/chromium", "snap/chromium/common/chromium", ".var/app/org.chromium.Chromium/config/chromium"]},
    {"id": "brave", "label": "Brave", "family": "chromium", "exes": ["brave-browser", "brave"],
     "roots": [".config/BraveSoftware/Brave-Browser"]},
    {"id": "microsoft-edge", "label": "Microsoft Edge", "family": "chromium",
     "exes": ["microsoft-edge", "microsoft-edge-stable"], "roots": [".config/microsoft-edge"]},
    {"id": "vivaldi", "label": "Vivaldi", "family": "chromium", "exes": ["vivaldi", "vivaldi-stable"],
     "roots": [".config/vivaldi"]},
    {"id": "firefox", "label": "Firefox", "family": "firefox", "exes": ["firefox", "firefox-esr"],
     "roots": [".mozilla/firefox", "snap/firefox/common/.mozilla/firefox",
               ".var/app/org.mozilla.firefox/.mozilla/firefox"]},
    {"id": "librewolf", "label": "LibreWolf", "family": "firefox", "exes": ["librewolf"],
     "roots": [".librewolf"]},
]

# Chromium profile files a clone may contain (allow-list). Saved passwords ("Login Data"), autofill
# ("Web Data"), history, extensions and sync data are NOT copied.
CHROMIUM_ALLOW = ["Network/Cookies", "Network/Cookies-journal", "Cookies", "Cookies-journal",
                  "Local Storage", "Session Storage", "IndexedDB", "Preferences"]
FIREFOX_ALLOW = ["cookies.sqlite", "cookies.sqlite-wal", "cookies.sqlite-shm", "webappsstore.sqlite",
                 "webappsstore.sqlite-wal", "storage", "permissions.sqlite"]
# Never copied, whatever the allow-list says (defence in depth).
NEVER = ("Login Data", "Login Data For Account", "Web Data", "key4.db", "key3.db", "logins.json", "signons.sqlite",
         "Extensions", "Extension Cookies", "History", "Sync Data", "Singleton", "lock", ".parentlock", "SingletonLock",
         "SingletonSocket", "SingletonCookie")

# Command-line arguments an agent may not pass: they would open an automation channel, load code into the
# browser, change which profile is used, or weaken the browser's own protections.
DENY_ARG_PREFIXES = (
    "--remote-debugging", "--remote-allow-origins", "--user-data-dir", "--profile-directory", "--load-extension",
    "--disable-extensions-except", "--enable-automation", "--enable-logging=stderr", "--inspect",
    "--no-sandbox", "--disable-web-security", "--disable-site-isolation", "--proxy-pac-url",
    "--marionette", "--start-debugger-server", "--remote-", "-profile", "--profile", "-P", "--ProfileManager",
    "--new-instance", "--no-remote", "--headless", "--ozone-platform", "--enable-features=UseOzonePlatform",
    "--log-net-log", "--external-protocol", "--app-id", "--install-", "--native-messaging",
)
ALLOWED_URL = re.compile(r"^(https?://|about:|file:///)", re.I)

FIREFOX_PREFS = {
    "browser.shell.checkDefaultBrowser": "false",
    "browser.aboutwelcome.enabled": "false",
    "datareporting.policy.dataSubmissionEnabled": "false",
    "datareporting.healthreport.uploadEnabled": "false",
    "toolkit.telemetry.reportingpolicy.firstRun": "false",
    "toolkit.telemetry.enabled": "false",
    "app.update.auto": "false",
    "app.update.enabled": "false",
    "app.normandy.enabled": "false",
    "browser.startup.homepage_override.mstone": '"ignore"',
    "browser.tabs.warnOnClose": "false",
    "browser.crashReports.unsubmittedCheck.autoSubmit2": "false",
    "network.captive-portal-service.enabled": "false",
    "extensions.pocket.enabled": "false",
}


# --------------------------------------------------------------------------------- discovery
def classify_variant(exe: str) -> Tuple[str, Optional[str]]:
    """-> (variant, reason it cannot be launched or None). variant: deb|binary|snap|flatpak."""
    real = os.path.realpath(exe)
    if real.startswith(("/snap/", "/var/lib/snapd/")) or "/snap/bin/" in exe:
        return "snap", SNAP_REASON
    if "flatpak" in real or "/var/lib/flatpak/exports/" in exe or "/.local/share/flatpak/exports/" in exe:
        return "flatpak", FLATPAK_REASON
    try:
        with open(real, "rb") as f:
            head = f.read(4)
            if head[:2] == b"#!":
                f.seek(0)
                text = f.read(4096).decode("utf-8", "replace")
                if "snap" in text and ("/snap/bin" in text or "snap install" in text):
                    return "snap", SNAP_REASON        # the Ubuntu stub that tells you to install the snap
                if "flatpak run" in text:
                    return "flatpak", FLATPAK_REASON
        return ("deb" if real.startswith(("/usr/", "/opt/")) else "binary"), None
    except OSError:
        return "binary", "executable not readable"


SNAP_REASON = ("snap confinement cannot reach the workspace's private runtime dir and profile, and the snap "
               "keeps its data under the real home (~/snap); AgentWorkspaces will not launch it. Use the .deb "
               "or the vendor tarball/binary (add it with AW_BROWSER_EXTRA or browsers.json)")
FLATPAK_REASON = ("flatpak sandboxing cannot use the workspace's private runtime dir and the app keeps its data "
                  "under the real home (~/.var/app); AgentWorkspaces will not launch it")


def _which(name: str, path: str) -> Optional[str]:
    if os.path.isabs(name):
        return name if os.access(name, os.X_OK) and os.path.isfile(name) else None
    return shutil.which(name, path=path)


def _extra_entries(home: str, env: Optional[Dict[str, str]]) -> List[Dict[str, Any]]:
    """User-added browsers: AW_BROWSER_EXTRA (JSON list) and <config>/browsers.json. Each entry:
    {"id": "firefox-tarball", "label": "...", "family": "chromium|firefox", "exe": "/abs/path"}."""
    out: List[Dict[str, Any]] = []
    srcs = []
    if env and env.get("AW_BROWSER_EXTRA"):
        srcs.append(env["AW_BROWSER_EXTRA"])
    cfg = os.path.join(home, ".config", "agent-workspaces", "browsers.json")
    try:
        with open(cfg) as f:
            srcs.append(f.read())
    except OSError:
        pass
    for s in srcs:
        try:
            items = json.loads(s)
        except ValueError:
            continue
        for it in items if isinstance(items, list) else []:
            if isinstance(it, dict) and it.get("exe") and it.get("family") in ("chromium", "firefox") \
                    and SAFE_NAME.match(str(it.get("id", ""))):
                out.append({"id": it["id"], "label": it.get("label", it["id"]), "family": it["family"],
                            "exes": [it["exe"]], "roots": it.get("roots", []), "extra": True})
    return out


def chromium_profiles(root: str) -> List[Dict[str, str]]:
    """Profile ids and display names from the browser's `Local State` (info_cache keys and names only)."""
    out = []
    try:
        with open(os.path.join(root, "Local State"), encoding="utf-8") as f:
            cache = (json.load(f).get("profile") or {}).get("info_cache") or {}
    except (OSError, ValueError, AttributeError):
        return out
    for pid, info in sorted(cache.items()):
        if SAFE_NAME.match(pid.replace(" ", "_")) and isinstance(info, dict):
            out.append({"id": pid, "name": str(info.get("name") or pid)[:80]})
    return out


def firefox_profiles(root: str) -> List[Dict[str, str]]:
    """Profile names/paths from profiles.ini (Name= and the directory name only)."""
    cp = configparser.RawConfigParser()
    try:
        cp.read(os.path.join(root, "profiles.ini"), encoding="utf-8")
    except (OSError, configparser.Error):
        return []
    out = []
    for sec in cp.sections():
        if sec.startswith("Profile") and cp.has_option(sec, "Path"):
            path = cp.get(sec, "Path")
            if cp.get(sec, "IsRelative", fallback="1") == "1" and "/" not in path and ".." not in path:
                out.append({"id": path, "name": cp.get(sec, "Name", fallback=path)[:80]})
    return out


def discover(home: str, path: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """Installed browsers and their profiles (names/ids only). `home` is explicit: tests pass a fake home and
    never touch the real one."""
    path = path or os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    out = []
    for ent in CATALOGUE + _extra_entries(home, env if env is not None else os.environ):
        exe = next((p for p in (_which(n, path) for n in ent["exes"]) if p), None)
        roots = [os.path.join(home, r) for r in ent.get("roots", [])]
        have_roots = [r for r in roots if os.path.isdir(r)]
        if not exe and not have_roots:
            continue
        variant, reason = classify_variant(exe) if exe else ("not-installed", "profile data found but the "
                                                             "browser executable is not installed")
        prof = []
        for r in have_roots:
            prof += [dict(p, root=r) for p in (chromium_profiles(r) if ent["family"] == "chromium"
                                              else firefox_profiles(r))]
        out.append({"id": ent["id"], "label": ent["label"], "family": ent["family"], "exe": exe,
                    "variant": variant, "launchable": reason is None and bool(exe),
                    "reason": reason, "profiles": prof})
    return out


def public_view(browsers: List[Dict[str, Any]], include_roots: bool = False) -> List[Dict[str, Any]]:
    """What the API returns: names and ids only (no filesystem paths of host profiles)."""
    out = []
    for b in browsers:
        d = {k: b[k] for k in ("id", "label", "family", "variant", "launchable", "reason")}
        d["profiles"] = [{"id": p["id"], "name": p["name"]} for p in b["profiles"]]
        if include_roots:
            d["exe"] = b["exe"]
        out.append(d)
    return out


def find_browser(browsers: List[Dict[str, Any]], ref: str) -> Dict[str, Any]:
    for b in browsers:
        if b["id"] == ref:
            return b
    alias = {"chrome": "google-chrome", "google": "google-chrome", "ff": "firefox"}.get(ref, ref)
    for b in browsers:
        if b["id"] == alias:
            return b
    fam = [b for b in browsers if b["family"] == ref and b["launchable"]]
    if fam:
        return fam[0]
    raise BrowserError("E_NO_BROWSER", f"no browser {ref!r}; installed: {[b['id'] for b in browsers] or 'none'}")


# --------------------------------------------------------------------------------- host-profile lock state
def host_profile_state(family: str, root: str, profile_dir: str) -> Tuple[str, str]:
    """Is this HOST profile open in the user's own browser? -> ("open"|"stale"|"closed", detail).
    Reads lock files only (never removes them). "stale" (a lock whose owner is gone) is NOT treated as safe."""
    cands = []
    if family == "chromium":
        cands = [os.path.join(root, "SingletonLock")]
    else:
        cands = [os.path.join(root, profile_dir, "lock"), os.path.join(root, profile_dir, ".parentlock")]
    state, detail = "closed", "no lock file"
    for lk in cands:
        try:
            st = os.lstat(lk)
        except OSError:
            continue
        pid = None
        if stat.S_ISLNK(st.st_mode):
            tgt = os.readlink(lk)                     # chromium: "<host>-<pid>"; firefox: "<ip>:+<pid>"
            m = re.search(r"[-+](\d+)$", tgt)
            pid = int(m.group(1)) if m else None
        if pid is not None:
            if os.path.exists(f"/proc/{pid}"):
                return "open", f"{os.path.basename(lk)} held by running pid {pid}"
            state, detail = "stale", (f"{os.path.basename(lk)} names pid {pid}, which is not running (stale lock; "
                                      "not removed)")
        elif os.path.basename(lk) == ".parentlock":
            # a plain file: Firefox holds an fcntl lock on it while running; a held lock means open
            try:
                import fcntl
                fd = os.open(lk, os.O_RDONLY)
                try:
                    fcntl.lockf(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except OSError as e:
                    if e.errno in (errno.EAGAIN, errno.EACCES):
                        return "open", ".parentlock is locked by a running Firefox"
                finally:
                    os.close(fd)
            except OSError:
                pass
        else:
            state, detail = "stale", f"{os.path.basename(lk)} exists but names no process (not removed)"
    return state, detail


# --------------------------------------------------------------------------------- profile directories
def _private(path: str) -> str:
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def throwaway_dir(ws_dir: str, browser_id: str) -> str:
    d = _private(os.path.join(ws_dir, "browser"))
    d = _private(os.path.join(d, "throwaway"))
    return _private(os.path.join(d, browser_id))


def agent_dir(data_dir: str, browser_id: str, name: str) -> str:
    if not SAFE_NAME.match(name or ""):
        raise BrowserError("E_BAD_NAME", "profile name must be 1-40 chars: letters, digits, _ . -")
    d = _private(os.path.join(data_dir, "browser-profiles", browser_id))
    return _private(os.path.join(d, name))


def list_agent_profiles(data_dir: str) -> List[Dict[str, str]]:
    out = []
    root = os.path.join(data_dir, "browser-profiles")
    try:
        for b in sorted(os.listdir(root)):
            for n in sorted(os.listdir(os.path.join(root, b))):
                if SAFE_NAME.match(n) and not n.endswith(".aw-owner") and os.path.isdir(os.path.join(root, b, n)):
                    out.append({"browser": b, "name": n})
    except OSError:
        pass
    return out


# --- ownership of a writable profile: one workspace/browser process at a time -------------------------
def _pid_alive(pid: Optional[int], start: Optional[str]) -> bool:
    if not pid:
        return False
    try:
        with open(f"/proc/{pid}/stat") as f:
            st = f.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return False
    return start is None or st == start


def proc_start(pid: int) -> Optional[str]:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def claim_profile(profile_dir: str, ws_id: str) -> None:
    """Record that `ws_id` is about to run a browser on this writable profile. Refuses when another workspace
    (or an earlier browser of any workspace that is still alive) owns it. The record lives next to, not in,
    the profile. Locks of the browser itself (SingletonLock, lock) are a second layer and are never removed."""
    rec = profile_dir.rstrip("/") + ".aw-owner"
    try:
        with open(rec) as f:
            cur = json.load(f)
    except (OSError, ValueError):
        cur = None
    if cur and _pid_alive(cur.get("pid"), cur.get("start")) and cur.get("ws") != ws_id:
        raise BrowserError("E_PROFILE_BUSY", f"this profile is in use by workspace {cur.get('ws_name') or cur['ws']}"
                           " (a writable profile is never shared between workspaces)")
    if cur and cur.get("ws") == ws_id and _pid_alive(cur.get("pid"), cur.get("start")):
        return                                      # already ours and running: keep the owner record
    tmp = rec + f".{secrets.token_hex(3)}.tmp"
    with open(tmp, "w") as f:
        json.dump({"ws": ws_id, "pid": None, "start": None, "t": time.time()}, f)
    os.replace(tmp, rec)


def bind_pid(profile_dir: str, ws_id: str, ws_name: str, pid: int) -> None:
    rec = profile_dir.rstrip("/") + ".aw-owner"
    tmp = rec + f".{secrets.token_hex(3)}.tmp"
    with open(tmp, "w") as f:
        json.dump({"ws": ws_id, "ws_name": ws_name, "pid": pid, "start": proc_start(pid), "t": time.time()}, f)
    os.replace(tmp, rec)


def release_profile(profile_dir: str, ws_id: str) -> None:
    rec = profile_dir.rstrip("/") + ".aw-owner"
    try:
        with open(rec) as f:
            cur = json.load(f)
        if cur.get("ws") == ws_id:
            os.unlink(rec)
    except (OSError, ValueError):
        pass


# --------------------------------------------------------------------------------- command lines
def validate_args(args: Optional[List[Any]]) -> List[str]:
    out = []
    for a in args or []:
        if not isinstance(a, str) or "\x00" in a or len(a) > 2000:
            raise BrowserError("E_BAD_ARGS", "browser args must be strings")
        low = a.lower()
        if any(low == d.lower() or low.startswith(d.lower() + "=") or (d.endswith("-") and low.startswith(d.lower()))
               for d in DENY_ARG_PREFIXES):
            raise BrowserError("E_BAD_BROWSER_ARG", f"argument {a.split('=')[0]!r} is not allowed (it would open an "
                               "automation channel, add code, or change the profile)")
        out.append(a)
    return out


def validate_url(url: Optional[str]) -> Optional[str]:
    if url is None or url == "":
        return None
    if not isinstance(url, str) or not ALLOWED_URL.match(url) or any(c in url for c in "\x00\n\r"):
        raise BrowserError("E_BAD_URL", "url must start with http://, https://, about: or file:///")
    return url


def write_firefox_user_js(profile_dir: str) -> None:
    """Quiet first-run and telemetry prefs. Does not accept any terms on the user's behalf."""
    p = os.path.join(profile_dir, "user.js")
    with open(p, "w") as f:
        f.write("// generated by agent-workspaces\n")
        for k, v in FIREFOX_PREFS.items():
            f.write(f'user_pref("{k}", {v});\n')
    os.chmod(p, 0o600)


def build_command(browser: Dict[str, Any], profile_dir: str, backend: str, url: Optional[str],
                  extra_args: List[str], chromium_profile: Optional[str] = None) -> List[str]:
    """argv for Manager.launch. The environment comes from the launcher (private display etc.)."""
    exe = browser["exe"]
    if backend not in ("wayland", "x11"):
        raise BrowserError("E_BAD_ARGS", "backend must be 'wayland' or 'x11'")
    if browser["family"] == "chromium":
        argv = [exe, f"--user-data-dir={profile_dir}", f"--ozone-platform={backend}", "--no-first-run",
                "--no-default-browser-check", "--password-store=basic", "--disable-sync",
                "--disable-background-networking", "--disable-features=Translate"]
        if chromium_profile:
            argv.append(f"--profile-directory={chromium_profile}")
        argv += extra_args
        if url:
            argv.append(url)
        return argv
    argv = [exe, "--no-remote", "--profile", profile_dir] + extra_args
    if url:
        argv.append(url)
    if backend == "x11":      # the launcher fixes MOZ_ENABLE_WAYLAND/GDK_BACKEND; env(1) overrides them for this app
        argv = ["/usr/bin/env", "MOZ_ENABLE_WAYLAND=0", "GDK_BACKEND=x11"] + argv
    return argv


# --------------------------------------------------------------------------------- authorised clones
def clone_dir(data_dir: str, ws_id: str, browser_id: str, profile_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", profile_id)
    return os.path.join(data_dir, "authorized", ws_id, f"{browser_id}__{safe}")


def _copy_tree_files(src: str, dst: str) -> int:
    """Copy a file or directory tree without following symlinks (symlinks are skipped). Files are 0600, dirs
    0700. Returns bytes copied."""
    n = 0
    if os.path.islink(src):
        return 0
    if os.path.isdir(src):
        os.makedirs(dst, mode=0o700, exist_ok=True)
        for name in sorted(os.listdir(src)):
            if name in NEVER or name.startswith("Singleton"):
                continue
            n += _copy_tree_files(os.path.join(src, name), os.path.join(dst, name))
    elif os.path.isfile(src):
        os.makedirs(os.path.dirname(dst), mode=0o700, exist_ok=True)
        with open(src, "rb") as fi, os.fdopen(os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as fo:
            shutil.copyfileobj(fi, fo)
        n = os.path.getsize(dst)
    return n


def plan_clone(browser: Dict[str, Any], profile: Dict[str, str]) -> Dict[str, Any]:
    """What `clone_profile` would copy (names of allow-listed items that exist) and what it never copies."""
    root, pid = profile["root"], profile["id"]
    base = os.path.join(root, pid)
    allow = CHROMIUM_ALLOW if browser["family"] == "chromium" else FIREFOX_ALLOW
    present = [a for a in allow if os.path.lexists(os.path.join(base, a))]
    return {"copies": present, "never_copied": ["saved passwords (Login Data / logins.json, key4.db)",
                                                  "autofill and payment data (Web Data)", "history",
                                                  "extensions", "sync data", "caches"]}


def clone_profile(browser: Dict[str, Any], profile: Dict[str, str], dest: str) -> Dict[str, Any]:
    """Copy the allow-listed session data of a HOST profile into `dest` (0700). The original is only read:
    nothing is written, locked, renamed or deleted there. Refuses when the host browser has it open."""
    family, root, pid = browser["family"], profile["root"], profile["id"]
    state, detail = host_profile_state(family, root, pid)
    if state != "closed":
        raise BrowserError("E_PROFILE_OPEN_ON_HOST", f"the profile appears to be in use by your own browser "
                           f"({detail}). Close that browser completely and try again. Nothing was copied and no "
                           "lock was touched.")
    src = os.path.join(root, pid)
    if not os.path.isdir(src) or os.path.islink(src):
        raise BrowserError("E_NOT_FOUND", "profile directory not found")
    if os.path.lexists(dest):
        raise BrowserError("E_EXISTS", "an authorised copy for this workspace and profile already exists")
    allow = CHROMIUM_ALLOW if family == "chromium" else FIREFOX_ALLOW
    tmp = dest.rstrip("/") + f".part-{secrets.token_hex(3)}"
    os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
    os.chmod(os.path.dirname(dest), 0o700)
    total, items = 0, []
    try:
        os.makedirs(tmp, mode=0o700)
        base_dst = os.path.join(tmp, pid) if family == "chromium" else tmp
        os.makedirs(base_dst, mode=0o700, exist_ok=True)
        for a in allow:
            s = os.path.join(src, a)
            if os.path.lexists(s):
                total += _copy_tree_files(s, os.path.join(base_dst, a))
                items.append(a)
        if family == "firefox":
            write_firefox_user_js(base_dst)
        os.rename(tmp, dest)
        os.chmod(dest, 0o700)
    except BaseException:
        shred(tmp)
        raise
    return {"items": items, "bytes": total, "profile_dir": pid if family == "chromium" else None}


def shred(path: str) -> int:
    """Overwrite every regular file under `path` with zeros, then delete the tree. Best effort: on journaling,
    copy-on-write or SSD storage the old blocks may survive (documented in docs/browsers.md). Never follows
    symlinks. Returns the number of files overwritten."""
    n = 0
    if not os.path.lexists(path):
        return 0
    if os.path.islink(path) or os.path.isfile(path):
        try:
            if os.path.isfile(path) and not os.path.islink(path):
                size = os.path.getsize(path)
                with open(path, "r+b", buffering=0) as f:
                    z = b"\0" * 65536
                    left = size
                    while left > 0:
                        f.write(z[:min(left, len(z))])
                        left -= len(z)
                    os.fsync(f.fileno())
                n = 1
            os.unlink(path)
        except OSError:
            pass
        return n
    for dirpath, dirnames, filenames in os.walk(path, topdown=False, followlinks=False):
        for fn in filenames:
            n += shred(os.path.join(dirpath, fn))
        for dn in dirnames:
            p = os.path.join(dirpath, dn)
            try:
                os.unlink(p) if os.path.islink(p) else os.rmdir(p)
            except OSError:
                pass
    try:
        os.rmdir(path)
    except OSError:
        shutil.rmtree(path, ignore_errors=True)
    return n


def check_clone_safe(path: str) -> None:
    """Before launching on a clone: it must be a real directory owned by us, mode 0700, not a symlink."""
    try:
        st = os.lstat(path)
    except OSError:
        raise BrowserError("E_NOT_FOUND", "authorised profile copy is missing")
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or \
            (st.st_mode & 0o077):
        raise BrowserError("E_UNSAFE_PROFILE", "authorised profile copy has unsafe ownership or permissions")
