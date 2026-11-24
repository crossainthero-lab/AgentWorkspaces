"""Authentication and authorisation for the local API (docs/api.md, docs/security-model.md).

Every request is authorised as  credential -> workspace -> scope  before any action happens.

* A credential is an unguessable token (256 bit). The daemon stores only SHA-256 hashes
  (`<ws>/credentials.json`, 0600). The plain token exists in exactly one place: the 0600 token file
  (`<ws>/token`, `admin.token`) or the process that was handed it. Tokens are never put in argv, in
  the daemon log, in the audit log or in error messages.
* A *workspace credential* is bound to ONE workspace id and a scope set. It can never be used on another
  workspace (E_FORBIDDEN), and a reference that does not resolve to its workspace is refused with the same
  error, so it is not an oracle for which other workspaces exist.
* The *admin credential* (`admin.token`) is for the human (CLI, viewer, panel extension). It may address any
  workspace and is the only credential that can create/destroy workspaces, take over/resume, resolve
  approvals and grants, and issue/revoke credentials.
* Unknown methods are denied to every non-admin principal (fail closed): a new API method must be added to
  POLICY below to become reachable with an agent credential.

Honest limit: all of this is same-user software. A hostile process running as you can read your 0600 token
files. It protects against other users (SO_PEERCRED + 0600/0700), against agents that were handed only a
scoped token, against mistakes (wrong workspace id, a stale client), and against sandboxed workspace apps
(`--sandbox`) that cannot see the runtime directory at all.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import struct
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

TOKEN_PREFIX = "awt1"
SCOPES = ("read", "screenshot", "input", "launch", "windows", "clipboard", "approval", "admin")
# What a workspace's default credential can do. `admin` is never in the default set.
AGENT_DEFAULT_SCOPES = ("read", "screenshot", "input", "launch", "windows", "clipboard", "approval")
MAX_TTL = 24 * 3600
DEFAULT_TTL = 900


class AuthError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


# --------------------------------------------------------------------------- policy
# method -> (target, scope)
#   target "open"    : no credential needed
#   target "any"     : any valid credential
#   target "ws"      : credential must be valid for params["workspace"]; needs `scope`
#   target "wsadmin" : as "ws", and the credential needs the `admin` scope (or be the admin credential)
#   target "list"    : admin sees everything, a workspace credential sees only its own workspace
#   target "global"  : the admin credential only (human channel)
POLICY: Dict[str, Tuple[str, Optional[str]]] = {
    "hello": ("open", None),
    "auth.login": ("open", None),
    "auth.whoami": ("any", None),
    "auth.exchange": ("any", None),
    "workspace.list": ("list", "read"),
    "workspace.get": ("ws", "read"), "workspace.status": ("ws", "read"), "workspace.usage": ("ws", "read"),
    "workspace.launch": ("ws", "launch"),
    "app.list": ("ws", "read"), "app.kill": ("ws", "launch"),
    "screen.screenshot": ("ws", "screenshot"), "screen.info": ("ws", "screenshot"),
    "window.list": ("ws", "windows"), "window.action": ("ws", "windows"),
    "input.move": ("ws", "input"), "input.click": ("ws", "input"), "input.drag": ("ws", "input"),
    "input.type": ("ws", "input"), "input.key": ("ws", "input"), "input.scroll": ("ws", "input"),
    "clipboard.get": ("ws", "clipboard"), "clipboard.set": ("ws", "clipboard"),
    "approval.request": ("ws", "approval"), "approval.list": ("list", "read"), "approval.get": ("list", "read"),
    "approval.resolve": ("global", None),
    "events.subscribe": ("list", "read"),
    "workspace.start": ("wsadmin", None), "workspace.stop": ("wsadmin", None),
    "workspace.configure": ("wsadmin", None), "workspace.pause": ("wsadmin", None),
    "workspace.takeover": ("wsadmin", None), "workspace.resume": ("wsadmin", None),
    "workspace.rename": ("wsadmin", None),
    "workspace.create": ("global", None), "workspace.destroy": ("global", None),
    "workspace.assign": ("global", None), "workspace.enter": ("global", None),
    "viewer.attach": ("global", None),
    "credential.list": ("global", None), "credential.issue": ("global", None),
    "credential.rotate": ("global", None), "credential.revoke": ("global", None),
    "grant.request": ("ws", "approval"), "grant.list": ("list", "read"), "grant.check": ("ws", "read"),
    "grant.resolve": ("global", None), "grant.revoke": ("global", None),
    "audit.tail": ("global", None),
}


class Principal:
    """Who is calling. Immutable once built."""
    __slots__ = ("kind", "ws_id", "scopes", "cred_id", "label", "expires", "peer_pid")

    def __init__(self, kind: str, ws_id: Optional[str], scopes: Iterable[str], cred_id: str, label: str = "",
                 expires: Optional[float] = None, peer_pid: Optional[int] = None):
        self.kind, self.ws_id, self.scopes = kind, ws_id, frozenset(scopes)
        self.cred_id, self.label, self.expires, self.peer_pid = cred_id, label, expires, peer_pid

    @property
    def is_admin(self) -> bool:
        return self.kind == "admin"

    def with_peer(self, pid: Optional[int]) -> "Principal":
        return Principal(self.kind, self.ws_id, self.scopes, self.cred_id, self.label, self.expires, pid)

    def describe(self) -> Dict[str, Any]:
        return {"kind": self.kind, "workspace": self.ws_id, "scopes": sorted(self.scopes),
                "credential": self.cred_id, "label": self.label, "expires_at": self.expires}

    def __repr__(self):   # never contains secrets, safe to log
        return f"<Principal {self.kind} ws={self.ws_id} cred={self.cred_id}>"


ADMIN_CRED_ID = "admin"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(ws_id: Optional[str]) -> str:
    return f"{TOKEN_PREFIX}_{ws_id or 'admin'}_{secrets.token_urlsafe(32)}"


def _write_secret(path: str, data: str) -> None:
    """Atomic write with mode 0600 from the first byte (no window with looser permissions)."""
    tmp = f"{path}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class CredentialStore:
    """Hash store for workspace credentials and the admin credential. Thread-safe."""

    def __init__(self, base_dir: str):
        self.base = base_dir
        self.lock = threading.RLock()
        self.by_hash: Dict[str, Dict[str, Any]] = {}      # hash -> record
        self.admin_hash: Optional[str] = None
        self._load_admin()

    # ---- admin
    @property
    def admin_token_path(self) -> str:
        return os.path.join(self.base, "admin.token")

    def _load_admin(self):
        p = self.admin_token_path
        tok = None
        try:
            st = os.lstat(p)
            if st.st_uid == os.getuid() and (st.st_mode & 0o077) == 0 and not os.path.islink(p):
                with open(p) as f:
                    tok = f.read().strip()
        except OSError:
            pass
        if not tok or not tok.startswith(TOKEN_PREFIX + "_admin_"):
            tok = self.rotate_admin()
        with self.lock:
            self.admin_hash = hash_token(tok)

    def admin_cred_id(self) -> str:
        return "admin:" + (self.admin_hash or "")[:8]

    def rotate_admin(self) -> str:
        tok = new_token(None)
        _write_secret(self.admin_token_path, tok + "\n")
        with self.lock:
            self.admin_hash = hash_token(tok)
        return tok

    # ---- per workspace
    def _cred_path(self, ws_id: str) -> str:
        return os.path.join(self.base, ws_id, "credentials.json")

    def token_path(self, ws_id: str) -> str:
        return os.path.join(self.base, ws_id, "token")

    def _save(self, ws_id: str):
        recs = [r for r in self.by_hash.values() if r["ws"] == ws_id]
        os.makedirs(os.path.join(self.base, ws_id), mode=0o700, exist_ok=True)
        _write_secret(self._cred_path(ws_id), json.dumps({"credentials": recs}))

    def load_workspace(self, ws_id: str) -> None:
        with self.lock:
            for h in [h for h, r in self.by_hash.items() if r["ws"] == ws_id]:
                del self.by_hash[h]
            try:
                with open(self._cred_path(ws_id)) as f:
                    recs = json.load(f).get("credentials", [])
            except (OSError, ValueError):
                recs = []
            now = time.time()
            for r in recs:
                if r.get("expires") and r["expires"] <= now:
                    continue
                self.by_hash[r["hash"]] = r

    def ensure_workspace(self, ws_id: str) -> str:
        """Make sure the workspace has a default credential whose token file matches the store.
        Returns the token file path. Idempotent."""
        with self.lock:
            self.load_workspace(ws_id)
            p = self.token_path(ws_id)
            tok = None
            try:
                st = os.lstat(p)
                if st.st_uid == os.getuid() and (st.st_mode & 0o077) == 0 and not os.path.islink(p):
                    with open(p) as f:
                        tok = f.read().strip()
            except OSError:
                pass
            rec = self.by_hash.get(hash_token(tok)) if tok else None
            if rec and rec["ws"] == ws_id and rec["kind"] == "default":
                return p
            self._rotate_default(ws_id)
            return p

    def _rotate_default(self, ws_id: str) -> None:
        for h in [h for h, r in self.by_hash.items() if r["ws"] == ws_id and r["kind"] == "default"]:
            del self.by_hash[h]
        tok = new_token(ws_id)
        cid = "c" + secrets.token_hex(4)
        self.by_hash[hash_token(tok)] = {"id": cid, "hash": hash_token(tok), "ws": ws_id, "kind": "default",
                                        "scopes": list(AGENT_DEFAULT_SCOPES), "label": "default",
                                        "created": time.time(), "expires": None}
        _write_secret(self.token_path(ws_id), tok + "\n")   # file first would leave a window; hash is in memory
        self._save(ws_id)

    def rotate(self, ws_id: str) -> str:
        """New default token (old one stops working immediately). Returns the token file path."""
        with self.lock:
            self._rotate_default(ws_id)
            return self.token_path(ws_id)

    def issue(self, ws_id: str, scopes: Iterable[str], ttl: Optional[float], label: str = "") -> Tuple[str, str, Optional[float]]:
        """Issue an additional credential. Returns (credential id, token, expires_at). The token is returned
        ONCE; only its hash is kept."""
        scopes = list(dict.fromkeys(scopes))
        bad = [s for s in scopes if s not in SCOPES]
        if bad or not scopes:
            raise AuthError("E_BAD_SCOPE", f"scopes must be a non-empty subset of {list(SCOPES)}")
        expires = None
        if ttl is not None:
            ttl = float(ttl)
            if not (1 <= ttl <= MAX_TTL):
                raise AuthError("E_BAD_TTL", f"ttl must be 1..{MAX_TTL} seconds")
            expires = time.time() + ttl
        tok = new_token(ws_id)
        cid = "c" + secrets.token_hex(4)
        with self.lock:
            self.by_hash[hash_token(tok)] = {"id": cid, "hash": hash_token(tok), "ws": ws_id, "kind": "issued",
                                            "scopes": scopes, "label": label[:60], "created": time.time(),
                                            "expires": expires}
            self._save(ws_id)
        return cid, tok, expires

    def revoke(self, ws_id: str, cred_id: Optional[str] = None, include_default: bool = False) -> int:
        """Revoke one credential, or every non-default credential of the workspace. Returns how many."""
        n = 0
        with self.lock:
            for h, r in list(self.by_hash.items()):
                if r["ws"] != ws_id:
                    continue
                if cred_id is not None and r["id"] != cred_id:
                    continue
                if cred_id is None and r["kind"] == "default" and not include_default:
                    continue
                del self.by_hash[h]
                n += 1
            if n:
                self._save(ws_id)
        return n

    def list(self, ws_id: str) -> List[Dict[str, Any]]:
        with self.lock:
            return [{"id": r["id"], "kind": r["kind"], "scopes": r["scopes"], "label": r["label"],
                     "created_at": r["created"], "expires_at": r["expires"]}
                    for r in self.by_hash.values() if r["ws"] == ws_id]

    def drop_workspace(self, ws_id: str) -> None:
        with self.lock:
            for h in [h for h, r in self.by_hash.items() if r["ws"] == ws_id]:
                del self.by_hash[h]

    # ---- authentication
    def authenticate(self, token: Any, peer_pid: Optional[int] = None) -> Principal:
        if not isinstance(token, str) or not (8 <= len(token) <= 512):
            raise AuthError("E_UNAUTHENTICATED", "missing or malformed credential")
        h = hash_token(token)
        with self.lock:
            if self.admin_hash and secrets.compare_digest(h, self.admin_hash):
                return Principal("admin", None, SCOPES, self.admin_cred_id(), "admin", None, peer_pid)
            r = self.by_hash.get(h)
        if r is None:
            raise AuthError("E_UNAUTHENTICATED", "invalid or revoked credential")
        if r.get("expires") and r["expires"] <= time.time():
            raise AuthError("E_TOKEN_EXPIRED", "credential expired")
        return Principal("workspace", r["ws"], r["scopes"], r["id"], r["label"], r.get("expires"), peer_pid)

    def still_valid(self, p: Principal) -> None:
        """Re-check an already authenticated principal (revocation / expiry take effect on live connections)."""
        if p.is_admin:
            if p.cred_id != self.admin_cred_id():
                raise AuthError("E_UNAUTHENTICATED", "the admin credential was rotated")
            return
        if p.expires and p.expires <= time.time():
            raise AuthError("E_TOKEN_EXPIRED", "credential expired")
        with self.lock:
            if not any(r["id"] == p.cred_id and r["ws"] == p.ws_id for r in self.by_hash.values()):
                raise AuthError("E_UNAUTHENTICATED", "credential was revoked")


def downscope(parent: Principal, scopes: Optional[Iterable[str]], ttl: Optional[float]) -> Tuple[List[str], float]:
    """Rules for auth.exchange: a token holder may mint a shorter-lived credential with a subset of its own
    scopes for the same workspace; never more, never `admin`, never longer than DEFAULT..MAX_TTL."""
    allowed = set(parent.scopes) - {"admin"}
    want = list(scopes) if scopes else sorted(allowed)
    extra = [s for s in want if s not in allowed]
    if extra:
        raise AuthError("E_FORBIDDEN", f"cannot grant scopes you do not hold: {extra}")
    ttl = DEFAULT_TTL if ttl is None else float(ttl)
    if not (1 <= ttl <= MAX_TTL):
        raise AuthError("E_BAD_TTL", f"ttl must be 1..{MAX_TTL} seconds")
    if parent.expires:
        ttl = min(ttl, max(1.0, parent.expires - time.time()))
    return want, ttl


def authorize(principal: Optional[Principal], method: str, params: Dict[str, Any],
              resolve_ws, store: Optional[CredentialStore] = None) -> Optional[str]:
    """Raise AuthError unless `principal` may call `method`. Returns the workspace id the call targets
    (for workspace-bound methods) or None. `resolve_ws(ref) -> Optional[str]` maps a name/id to a workspace
    id or None when unknown; it must not raise."""
    target, scope = POLICY.get(method, (None, None))
    if target == "open":
        return None
    if principal is None:
        raise AuthError("E_UNAUTHENTICATED", "authentication required (API v2): send a credential, see docs/api.md")
    if store is not None:
        store.still_valid(principal)
    if target is None:
        if principal.is_admin:
            return None            # the dispatcher reports E_UNKNOWN_METHOD
        raise AuthError("E_FORBIDDEN", f"method {method!r} is not available to workspace credentials")
    if target == "any":
        return None
    if target == "global":
        if not principal.is_admin:
            raise AuthError("E_FORBIDDEN", f"{method} needs the admin credential (human only)")
        return None
    if target == "list":
        if not principal.is_admin and scope not in principal.scopes:
            raise AuthError("E_FORBIDDEN", f"credential lacks scope {scope!r}")
        return None
    # workspace-bound
    ref = params.get("workspace")
    if principal.is_admin:
        return None                # E_NOT_FOUND / E_BAD_ARGS come from the dispatcher
    if not ref or not isinstance(ref, str):
        raise AuthError("E_BAD_ARGS", "missing 'workspace'")
    wid = resolve_ws(ref)
    if wid is None or wid != principal.ws_id:
        raise AuthError("E_FORBIDDEN", f"this credential is bound to workspace {principal.ws_id!r} and cannot "
                        f"be used for {ref!r}")
    need = "admin" if target == "wsadmin" else scope
    if need not in principal.scopes:
        raise AuthError("E_FORBIDDEN", f"credential lacks scope {need!r} for {method}")
    return wid


# --------------------------------------------------------------------------- peer checks
def peer_cred(sock: socket.socket) -> Tuple[int, int, int]:
    """(pid, uid, gid) of the process on the other end of a unix socket, as recorded by the kernel."""
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", raw)


def peer_has_tty(pid: Optional[int]) -> bool:
    """True when the process has a controlling terminal and its stdin is a terminal."""
    if not pid or pid <= 0:
        return False
    try:
        with open(f"/proc/{pid}/stat") as f:
            tty_nr = int(f.read().rsplit(")", 1)[1].split()[4])
        if tty_nr == 0:
            return False
        tgt = os.readlink(f"/proc/{pid}/fd/0")
        return tgt.startswith("/dev/pts/") or tgt.startswith("/dev/tty")
    except (OSError, ValueError, IndexError):
        return False


# --------------------------------------------------------------------------- audit log
class AuditLog:
    """Append-only JSON lines in <base>/audit.log (0600). Records WHO did WHAT to WHICH workspace and the
    outcome. Never records parameters, typed text, clipboard contents, screenshots or tokens."""
    MAX = 4 * 1024 * 1024

    def __init__(self, base_dir: str):
        self.path = os.path.join(base_dir, "audit.log")
        self.lock = threading.Lock()

    def write(self, event: str, **fields):
        rec = {"ts": round(time.time(), 3), "event": event}
        rec.update({k: v for k, v in fields.items() if v is not None})
        line = json.dumps(rec, separators=(",", ":")) + "\n"
        with self.lock:
            try:
                if os.path.exists(self.path) and os.path.getsize(self.path) > self.MAX:
                    os.replace(self.path, self.path + ".1")
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
                try:
                    os.write(fd, line.encode())
                finally:
                    os.close(fd)
            except OSError:
                pass

    def tail(self, n: int = 50) -> List[Dict[str, Any]]:
        try:
            with open(self.path) as f:
                lines = f.readlines()[-max(1, min(n, 1000)):]
        except OSError:
            return []
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
        return out


# --------------------------------------------------------------------------- client side lookup
class Credential:
    def __init__(self, token: str, source: str, kind: str):
        self.token, self.source, self.kind = token, source, kind

    def __repr__(self):
        return f"<Credential {self.kind} from {self.source}>"


def _read_token_file(path: str) -> Optional[str]:
    """Read a token file, refusing files that other users could read or that are not ours."""
    try:
        st = os.stat(path)
        if st.st_uid != os.getuid() or (st.st_mode & 0o077):
            raise AuthError("E_INSECURE_CREDENTIAL_FILE", f"{path} must be owned by you with mode 0600 "
                            f"(is {oct(st.st_mode & 0o777)}); refusing to use it")
        with open(path) as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None
    except OSError:
        return None


def workspace_dirs(base: str) -> List[Tuple[str, Dict[str, Any]]]:
    out = []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return out
    for n in names:
        try:
            with open(os.path.join(base, n, "ws.json")) as f:
                out.append((n, json.load(f)))
        except (OSError, ValueError):
            continue
    return out


def workspace_id_for(base: str, ref: str) -> Optional[str]:
    """Map a name or id to an id by reading the workspace metadata (no daemon call, no credential)."""
    ws = workspace_dirs(base)
    for wid, meta in ws:
        if wid == ref:
            return wid
    for wid, meta in ws:
        if meta.get("name") == ref:
            return wid
    low = [wid for wid, meta in ws if str(meta.get("name", "")).lower() == str(ref).lower()]
    return low[0] if len(low) == 1 else None


def find_credentials(base: str, role: str = "auto", workspace: Optional[str] = None,
                     environ: Optional[Dict[str, str]] = None, explicit: Optional[str] = None) -> List[Credential]:
    """Credential lookup order (docs/api.md). Returns candidates, best first; never raises for 'not found'.

    role "agent":  1 explicit token  2 AW_TOKEN  3 AW_TOKEN_FILE  (if any of these is set, that is final)
                   4 the token file of the addressed workspace (`workspace` argument, else AW_WORKSPACE) under the
                   base dir  5 the token files of all workspaces assigned to AW_AGENT_KIND
    role "admin":  1 explicit token  2 AW_ADMIN_TOKEN  3 AW_ADMIN_TOKEN_FILE  4 <base>/admin.token
    role "auto":   agent tiers 1-3 if any is set (an explicitly configured agent never silently escalates to
                   admin); otherwise the admin tiers; otherwise agent tiers 4-5.
    """
    env = os.environ if environ is None else environ
    out: List[Credential] = []

    def add(tok, src, kind):
        if tok and all(c.token != tok for c in out):
            out.append(Credential(tok, src, kind))

    def explicit_tiers(kind):
        add(explicit, "explicit", kind)
        add(env.get("AW_TOKEN") if kind == "workspace" else env.get("AW_ADMIN_TOKEN"),
            "AW_TOKEN" if kind == "workspace" else "AW_ADMIN_TOKEN", kind)
        fe = "AW_TOKEN_FILE" if kind == "workspace" else "AW_ADMIN_TOKEN_FILE"
        if env.get(fe):
            add(_read_token_file(env[fe]), fe, kind)

    def agent_file_tiers():
        wref = workspace or env.get("AW_WORKSPACE")
        if wref:
            wid = workspace_id_for(base, wref)
            if wid:
                add(_read_token_file(os.path.join(base, wid, "token")), f"{base}/<{wid}>/token", "workspace")
        kind = env.get("AW_AGENT_KIND")
        if kind and not wref:
            for wid, meta in workspace_dirs(base):
                if (meta.get("agent") or {}).get("kind") == kind:
                    add(_read_token_file(os.path.join(base, wid, "token")), f"{base}/<{wid}>/token", "workspace")

    def admin_file_tier():
        add(_read_token_file(os.path.join(base, "admin.token")), f"{base}/admin.token", "admin")

    if role == "agent":
        explicit_tiers("workspace")
        if not out:              # explicitly configured credentials are final: never widen to other files
            agent_file_tiers()
    elif role == "admin":
        add(explicit, "explicit", "admin")
        add(env.get("AW_ADMIN_TOKEN"), "AW_ADMIN_TOKEN", "admin")
        if env.get("AW_ADMIN_TOKEN_FILE"):
            add(_read_token_file(env["AW_ADMIN_TOKEN_FILE"]), "AW_ADMIN_TOKEN_FILE", "admin")
        admin_file_tier()
    else:
        if explicit or env.get("AW_TOKEN") or env.get("AW_TOKEN_FILE"):
            explicit_tiers("workspace")
        else:
            add(env.get("AW_ADMIN_TOKEN"), "AW_ADMIN_TOKEN", "admin")
            if env.get("AW_ADMIN_TOKEN_FILE"):
                add(_read_token_file(env["AW_ADMIN_TOKEN_FILE"]), "AW_ADMIN_TOKEN_FILE", "admin")
            admin_file_tier()
            agent_file_tiers()
    return out
