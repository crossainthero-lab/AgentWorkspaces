"""Daemon-side glue for the `browser.*` methods (docs/browsers.md, docs/api.md).

The daemon object supplies: `mgr` (launch), `grants` (human-only grants, aw/grants.py), `audit`,
`_ws`, `_check_assignment`, `_human_channel`. Authorisation by credential and scope is done before this code
runs (aw/policy_ext.py). Granting access to a user profile needs the admin credential AND a human channel
(an interactive terminal); an agent credential can only ask (`browser.profile.request`).
"""
from __future__ import annotations

import os
import signal
import time
from typing import Any, Dict, List, Optional

from . import browsers as B
from . import paths
from .grants import GrantError
from .manager import ManagerError

GRANT_KIND = "browser_profile"


def _err(e: B.BrowserError) -> ManagerError:
    return ManagerError(e.code, e.message)


class BrowserAPI:
    def __init__(self, daemon, home: Optional[str] = None, data_dir: Optional[str] = None):
        self.d = daemon
        self._home = home
        self._data = data_dir

    @property
    def home(self) -> str:
        return self._home or os.path.expanduser("~")

    @property
    def data(self) -> str:
        return self._data or paths.data_dir()

    def discover(self) -> List[Dict[str, Any]]:
        return B.discover(self.home)

    # ---- housekeeping: an authorised copy exists only while a live grant backs it
    def sweep(self) -> int:
        root = os.path.join(self.data, "authorized")
        n = 0
        try:
            wids = os.listdir(root)
        except OSError:
            return 0
        for wid in wids:
            wdir = os.path.join(root, wid)
            if wid not in self.d.mgr.workspaces:
                B.shred(wdir)
                n += 1
                continue
            for ent in os.listdir(wdir):
                bid, _, prof = ent.partition("__")
                live = any(g.ws == wid and g.kind == GRANT_KIND and g.effective_status() == "granted"
                           and g.resource.split(":", 1)[0] == bid for g in self.d.grants.grants.values())
                if not live:
                    path = os.path.join(wdir, ent)
                    if self._kill_apps_using(self.d.mgr.workspaces[wid], path):
                        time.sleep(1.0)
                    B.shred(path)
                    self.d.audit.write("browser.profile.shredded", workspace=wid, browser=bid, reason="no live grant")
                    n += 1
        return n

    def drop_workspace(self, ws_id: str) -> None:
        B.shred(os.path.join(self.data, "authorized", ws_id))

    # ---- dispatch
    def handle(self, method: str, p: Dict[str, Any], conn_state: Dict[str, Any], pr) -> Any:
        try:
            if method == "browser.list":
                return self._list(p, pr)
            if method == "browser.launch":
                return self._launch(p, pr)
            if method == "browser.profile.list":
                return self._profiles(p, pr)
            if method == "browser.profile.request":
                return self._request(p, pr)
            if method in ("browser.profile.authorize", "browser.profile.revoke"):
                channel = self.d._human_channel(conn_state)
                if channel is None:
                    self.d.audit.write("browser.profile.refused_channel", method=method,
                                       **self.d._audit_ctx(pr, conn_state))
                    raise ManagerError("E_NOT_HUMAN_CHANNEL", f"{method} is only accepted from a human channel: run "
                                       "`agent-workspaces profile authorize|revoke` in an interactive terminal")
                return self._authorize(p, channel) if method.endswith("authorize") else self._revoke(p, channel)
        except B.BrowserError as e:
            raise _err(e)
        except GrantError as e:
            raise ManagerError(e.code, e.message)
        raise ManagerError("E_UNKNOWN_METHOD", f"unknown method {method!r}")

    # ---- browser.list
    def _list(self, p, pr):
        ws = self.d._ws(p)
        self.d._check_assignment(ws, p, pr)
        return {"browsers": B.public_view(self.discover()), "agent_profiles": B.list_agent_profiles(self.data),
                "authorized": self._authorized(ws.id)}

    def _authorized(self, ws_id: str) -> List[Dict[str, Any]]:
        out = []
        for g in self.d.grants.grants.values():
            if g.ws == ws_id and g.kind == GRANT_KIND:
                bid, _, prof = g.resource.partition(":")
                out.append({"browser": bid, "profile": prof, "grant": g.id, "status": g.effective_status(),
                            "expires_at": g.expires})
        return out

    def _profiles(self, p, pr):
        ws = self.d._ws(p)
        self.d._check_assignment(ws, p, pr)
        tdir = os.path.join(ws.dir, "browser", "throwaway")
        return {"throwaway": sorted(os.listdir(tdir)) if os.path.isdir(tdir) else [],
                "agent": B.list_agent_profiles(self.data), "authorized": self._authorized(ws.id)}

    # ---- agent: ask for access to a host profile
    def _request(self, p, pr):
        ws = self.d._ws(p)
        self.d._check_assignment(ws, p, pr)
        b = B.find_browser(self.discover(), str(p.get("browser", "")))
        prof = self._resolve_profile(b, str(p.get("profile", "")))
        reason = str(p.get("reason", ""))[:300]
        g = self.d.grants.request(ws.id, GRANT_KIND, f"{b['id']}:{prof['id']}",
                                  f"{b['label']} profile \"{prof['name']}\"" + (f": {reason}" if reason else ""))
        out = g.to_json()
        out["how"] = (f"the user must run: agent-workspaces profile authorize {b['id']} \"{prof['id']}\" "
                      f"--workspace {ws.name}")
        return out

    @staticmethod
    def _resolve_profile(b: Dict[str, Any], ref: str) -> Dict[str, str]:
        for prof in b["profiles"]:
            if ref in (prof["id"], prof["name"]):
                return prof
        raise ManagerError("E_NOT_FOUND", f"{b['label']} has no profile {ref!r}; known: "
                           f"{[q['id'] for q in b['profiles']] or 'none'}")

    # ---- human: authorise (create the copy) / revoke (shred it)
    def _authorize(self, p, channel: str):
        ws = self.d._ws(p)
        b = B.find_browser(self.discover(), str(p.get("browser", "")))
        prof = self._resolve_profile(b, str(p.get("profile", "")))
        resource = f"{b['id']}:{prof['id']}"
        dest = B.clone_dir(self.data, ws.id, b["id"], prof["id"])
        if os.path.lexists(dest):
            if not p.get("refresh"):
                raise ManagerError("E_EXISTS", "this workspace already has an authorised copy of that profile "
                                   "(revoke it first, or pass refresh)")
            self._shred_clone(ws, b["id"], prof["id"], "refresh")
        # the copy happens first: if the host profile is open (or anything else fails) no grant is created
        res = B.clone_profile(b, prof, dest)
        ttl = float(p.get("ttl_seconds") or 8 * 3600)
        g = self.d.grants.request(ws.id, GRANT_KIND, resource, f"{b['label']} profile \"{prof['name']}\"")
        if g.effective_status() == "pending":
            g = self.d.grants.resolve(g.id, "granted", ttl, channel)
        self.d.audit.write("browser.profile.cloned", workspace=ws.id, browser=b["id"], profile=prof["name"],
                           items=res["items"], bytes=res["bytes"], channel=channel, grant=g.id)
        return {"grant": g.to_json(), "copied": res["items"], "bytes": res["bytes"],
                "never_copied": ["saved passwords", "autofill/payment data", "history", "extensions", "sync data"]}

    def _kill_apps_using(self, ws, needle: str) -> int:
        n = 0
        for a in ws.apps.values():
            if a.get("running") and any(needle in x for x in a.get("argv", [])):
                try:
                    self.d.mgr.kill_app(ws, a["n"])
                    n += 1
                except ManagerError:
                    pass
        return n

    def _shred_clone(self, ws, bid: str, profile_id: str, why: str) -> int:
        dest = B.clone_dir(self.data, ws.id, bid, profile_id)
        killed = self._kill_apps_using(ws, dest)
        if killed:
            time.sleep(1.0)
        n = B.shred(dest)
        self.d.audit.write("browser.profile.shredded", workspace=ws.id, browser=bid, files=n, reason=why)
        return n

    def _revoke(self, p, channel: str):
        ws = self.d._ws(p)
        b = B.find_browser(self.discover(), str(p.get("browser", ""))) if p.get("browser") else None
        bid = b["id"] if b else str(p.get("browser", ""))
        prof = str(p.get("profile", ""))
        pid = prof
        if b:
            try:
                pid = self._resolve_profile(b, prof)["id"]
            except ManagerError:
                pid = prof
        n = self._shred_clone(ws, bid, pid, "revoked")
        for g in list(self.d.grants.grants.values()):
            if g.ws == ws.id and g.kind == GRANT_KIND and g.resource == f"{bid}:{pid}":
                self.d.grants.revoke(g.id, channel)
        self.d.audit.write("browser.profile.revoked", workspace=ws.id, browser=bid, channel=channel)
        return {"revoked": True, "files_overwritten": n}

    # ---- launch
    def _launch(self, p, pr):
        ws = self.d._ws(p)
        self.d._check_assignment(ws, p, pr)
        self.sweep()
        b = B.find_browser(self.discover(), str(p.get("browser") or "chromium"))
        if not b["launchable"]:
            raise ManagerError("E_BROWSER_UNSUPPORTED", f"{b['label']} ({b['variant']}): {b['reason']}")
        backend = str(p.get("backend") or "wayland")
        if backend == "x11" and not ws.xwayland:
            raise ManagerError("E_BAD_ARGS", "backend x11 needs a workspace with XWayland")
        url = B.validate_url(p.get("url"))
        args = B.validate_args(p.get("args"))
        mode, name, chromium_prof = self._profile_spec(p.get("profile"), b)
        claimed = None
        if ws.sandbox and mode != "throwaway":
            # a sandboxed workspace sees only its own directory and the mounts you gave it, not the data dir
            raise ManagerError("E_BAD_ARGS", "a sandboxed workspace supports only throwaway browser profiles "
                               "(browsers in sandboxed workspaces were not tested)")
        if mode == "throwaway":
            pdir = B.throwaway_dir(ws.dir, b["id"])
        elif mode == "agent":
            pdir = B.agent_dir(self.data, b["id"], name)
            B.claim_profile(pdir, ws.id)
            claimed = pdir
        else:   # authorized
            resource = f"{b['id']}:{name}"
            g = self.d.grants.require(ws.id, GRANT_KIND, resource)
            if g.resource != resource:      # a grant without a resource would be a wildcard: not accepted here
                raise ManagerError("E_GRANT_REQUIRED", f"the approved grant does not name {resource!r}; ask for "
                                   "this exact profile with browser.profile.request")
            pdir = B.clone_dir(self.data, ws.id, b["id"], name)
            if not os.path.lexists(pdir):
                # approved through the generic grant flow (`agent-workspaces grants approve`): make the copy now
                prof = self._resolve_profile(b, name)
                B.clone_profile(b, prof, pdir)
                self.d.audit.write("browser.profile.cloned", workspace=ws.id, browser=b["id"], profile=prof["name"],
                                   grant=g.id, channel=g.channel, via="launch")
            B.check_clone_safe(pdir)
            B.claim_profile(pdir, ws.id)
            claimed = pdir
            chromium_prof = name if b["family"] == "chromium" else None
        if b["family"] == "firefox" and mode != "authorized":
            B.write_firefox_user_js(pdir)
        argv = B.build_command(b, pdir, backend, url, args, chromium_prof)
        home = os.path.join(ws.dir, "home")
        os.makedirs(home, mode=0o700, exist_ok=True)
        self.d.mgr.note_action(ws, "launch-browser", "agent")
        res = self.d.mgr.launch(ws, argv, extra_env={"HOME": home})
        if claimed:
            B.bind_pid(claimed, ws.id, ws.name, res["pid"])
        self.d.audit.write("browser.launch", workspace=ws.id, browser=b["id"], mode=mode,
                           profile=name if mode != "throwaway" else None)
        return {"app": res["app"], "pid": res["pid"], "log": res["log"], "browser": b["id"],
                "profile": {"mode": mode, "name": name}, "backend": backend, "url": url}

    @staticmethod
    def _profile_spec(spec: Any, b: Dict[str, Any]):
        """-> (mode, name, chromium_profile_dir). Accepts None, "throwaway", "agent:<name>", "authorized:<id>" or
        {"mode": ..., "name": ...}. Paths are never accepted."""
        if spec in (None, "", "throwaway"):
            return "throwaway", "", None
        if isinstance(spec, str):
            mode, _, name = spec.partition(":")
            spec = {"mode": mode, "name": name}
        if not isinstance(spec, dict):
            raise ManagerError("E_BAD_ARGS", "profile must be 'throwaway', 'agent:<name>' or 'authorized:<id>'")
        mode, name = spec.get("mode", "throwaway"), str(spec.get("name") or spec.get("id") or "")
        if mode == "throwaway":
            return "throwaway", "", None
        if mode == "agent":
            if not B.SAFE_NAME.match(name):
                raise ManagerError("E_BAD_NAME", "agent profile name must be 1-40 chars: letters, digits, _ . -")
            return "agent", name, None
        if mode == "authorized":
            if not name:
                raise ManagerError("E_BAD_ARGS", "authorized profile needs the profile id")
            return "authorized", name, name
        raise ManagerError("E_BAD_ARGS", f"unknown profile mode {mode!r}")
