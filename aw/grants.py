"""Human-only grants for sensitive operations (docs/security-model.md, "Explicit human authorisation").

Agents may only REQUEST a grant (`grant.request`, scope `approval`). Granting, denying and revoking need the
admin credential AND a human channel (an interactive terminal, the shipped viewer, or GNOME Shell), see
`Daemon._human_channel`. They are never reachable with a workspace credential or through MCP.

A grant is bound to (workspace, kind[, resource]) and expires (default 15 minutes, max 8 hours). Code that
performs a sensitive operation must call `GrantStore.require(...)` first. Requests, grants, denials, revocations
and refused uses are logged to the audit log WITHOUT secrets (the resource is a label such as a profile name,
never contents).
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from typing import Any, Dict, List, Optional

# kind -> what the human is being asked
KINDS: Dict[str, str] = {
    "browser_profile": "let this workspace use a COPY of one of your signed-in browser profiles "
                       "(cookies and saved logins become available to the agent)",
    "host_clipboard": "share your real clipboard with this workspace",
}
DEFAULT_TTL = 15 * 60
MAX_TTL = 8 * 3600


class GrantError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class Grant:
    def __init__(self, gid: str, ws: str, kind: str, resource: str, detail: str):
        self.id, self.ws, self.kind, self.resource, self.detail = gid, ws, kind, resource, detail
        self.status = "pending"            # pending | granted | denied | revoked | expired
        self.created = time.time()
        self.resolved: Optional[float] = None
        self.expires: Optional[float] = None
        self.channel = ""

    def effective_status(self) -> str:
        if self.status == "granted" and self.expires and self.expires <= time.time():
            return "expired"
        return self.status

    def to_json(self) -> Dict[str, Any]:
        return {"id": self.id, "workspace": self.ws, "kind": self.kind, "resource": self.resource,
                "detail": self.detail, "status": self.effective_status(), "created_at": self.created,
                "resolved_at": self.resolved, "expires_at": self.expires, "channel": self.channel,
                "description": KINDS.get(self.kind, "")}


class GrantStore:
    def __init__(self, base_dir: str, audit=None, emit=None):
        self.path = os.path.join(base_dir, "grants.json")
        self.audit = audit
        self.emit = emit or (lambda t, d: None)
        self.lock = threading.RLock()
        self.grants: Dict[str, Grant] = {}
        self._load()

    # ---- persistence (metadata only; no secrets)
    def _load(self):
        try:
            with open(self.path) as f:
                items = json.load(f).get("grants", [])
        except (OSError, ValueError):
            return
        now = time.time()
        for d in items:
            g = Grant(d["id"], d["workspace"], d["kind"], d.get("resource", ""), d.get("detail", ""))
            g.status, g.created, g.resolved = d["status"], d["created_at"], d.get("resolved_at")
            g.expires, g.channel = d.get("expires_at"), d.get("channel", "")
            if g.status == "granted" and g.expires and g.expires <= now:
                g.status = "expired"
            self.grants[g.id] = g

    def _save(self):
        tmp = self.path + f".{secrets.token_hex(3)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"grants": [g.to_json() for g in self.grants.values()]}, f)
        os.replace(tmp, self.path)

    def _log(self, event: str, g: Grant, **extra):
        if self.audit:
            self.audit.write(event, workspace=g.ws, grant=g.id, kind=g.kind, resource=g.resource or None, **extra)

    # ---- API
    def request(self, ws_id: str, kind: str, resource: str = "", detail: str = "") -> Grant:
        if kind not in KINDS:
            raise GrantError("E_BAD_GRANT", f"unknown grant kind {kind!r}; known: {sorted(KINDS)}")
        resource, detail = str(resource or "")[:200], str(detail or "")[:500]
        with self.lock:
            for g in self.grants.values():          # idempotent: one live request per (ws, kind, resource)
                if (g.ws, g.kind, g.resource) == (ws_id, kind, resource) and g.effective_status() in (
                        "pending", "granted"):
                    return g
            g = Grant("g" + secrets.token_hex(4), ws_id, kind, resource, detail)
            self.grants[g.id] = g
            self._save()
        self._log("grant.requested", g)
        self.emit("grant.requested", {"id": ws_id, "grant": g.to_json()})
        return g

    def get(self, gid: str) -> Grant:
        g = self.grants.get(gid)
        if not g:
            raise GrantError("E_NOT_FOUND", "no such grant")
        return g

    def list(self, ws_id: Optional[str] = None, pending: bool = False) -> List[Dict[str, Any]]:
        with self.lock:
            return [g.to_json() for g in self.grants.values()
                    if (ws_id is None or g.ws == ws_id) and (not pending or g.effective_status() == "pending")]

    def resolve(self, gid: str, decision: str, ttl: Optional[float], channel: str) -> Grant:
        if decision not in ("granted", "denied"):
            raise GrantError("E_BAD_ARGS", "decision must be granted|denied")
        with self.lock:
            g = self.get(gid)
            if g.effective_status() != "pending":
                raise GrantError("E_BAD_STATE", f"grant is {g.effective_status()}, not pending")
            g.status, g.resolved, g.channel = decision, time.time(), channel
            if decision == "granted":
                t = DEFAULT_TTL if ttl is None else float(ttl)
                if not (1 <= t <= MAX_TTL):
                    raise GrantError("E_BAD_TTL", f"ttl must be 1..{MAX_TTL} seconds")
                g.expires = time.time() + t
            self._save()
        self._log("grant." + decision, g, channel=channel, expires_at=g.expires)
        self.emit("grant.resolved", {"id": g.ws, "grant": g.to_json()})
        return g

    def revoke(self, gid: str, channel: str) -> Grant:
        with self.lock:
            g = self.get(gid)
            if g.effective_status() not in ("pending", "granted"):
                return g
            g.status, g.resolved, g.channel = "revoked", time.time(), channel
            self._save()
        self._log("grant.revoked", g, channel=channel)
        self.emit("grant.resolved", {"id": g.ws, "grant": g.to_json()})
        return g

    def drop_workspace(self, ws_id: str):
        with self.lock:
            for g in list(self.grants.values()):
                if g.ws == ws_id:
                    del self.grants[g.id]
            self._save()

    # ---- enforcement points
    def is_granted(self, ws_id: str, kind: str, resource: str = "") -> Optional[Grant]:
        with self.lock:
            for g in self.grants.values():
                if (g.ws == ws_id and g.kind == kind and g.effective_status() == "granted"
                        and (not g.resource or g.resource == resource)):
                    return g
        return None

    def require(self, ws_id: str, kind: str, resource: str = "") -> Grant:
        """Call before a sensitive operation. Raises GrantError(E_GRANT_REQUIRED) when no live grant exists;
        the refused attempt is logged."""
        g = self.is_granted(ws_id, kind, resource)
        if g is None:
            if self.audit:
                self.audit.write("grant.refused_use", workspace=ws_id, kind=kind, resource=resource or None)
            raise GrantError("E_GRANT_REQUIRED",
                             f"this needs the user's permission ({kind}"
                             f"{': ' + resource if resource else ''}). Ask with grant.request; only the human "
                             f"can approve it (agent-workspaces grants approve).")
        if self.audit:
            self.audit.write("grant.used", workspace=ws_id, grant=g.id, kind=kind, resource=resource or None)
        return g
