#!/usr/bin/env python3
"""Back up the agent configuration files an integration install touches, verify the copies, and restore them.

  python3 -I scripts/agent_backup.py backup  DIR [FILE ...]   copy each existing FILE into DIR (0700) with sha256 sums
  python3 -I scripts/agent_backup.py verify  DIR              recompute the sums of DIR against SHA256SUMS
  python3 -I scripts/agent_backup.py restore DIR [--into ROOT] [--only NAME]
                                                          put the files back (default: to their original paths) or
                                                          into ROOT/<original path> (a scratch directory: restore test)
  python3 -I scripts/agent_backup.py compare DIR              compare the live files with the backup (names + OK/CHANGED/MISSING)

Only file NAMES and checksums are ever printed, never contents. Files that do not exist at backup time are recorded as
"absent" so that restore removes what an install created.
"""
import hashlib
import json
import os
import shutil
import stat
import sys

DEFAULT_FILES = [
    "~/.claude.json", "~/.claude/settings.json", "~/.claude/skills/agent-workspaces/SKILL.md",
    "~/.hermes/config.yaml", "~/.hermes/shell-hooks-allowlist.json",
    "~/.config/agent-workspaces/guard-state.json", "~/.local/bin/agent-workspaces",
    "~/.config/systemd/user/agent-workspaces.service",
]


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def slot(i):
    return f"f{i:02d}"


def backup(d, files):
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    manifest = []
    for i, f in enumerate(files):
        p = os.path.expanduser(f)
        ent = {"name": f, "path": p, "slot": slot(i)}
        if os.path.islink(p):
            ent.update(kind="symlink", target=os.readlink(p))
        elif os.path.isfile(p):
            dst = os.path.join(d, slot(i))
            shutil.copy2(p, dst)
            os.chmod(dst, 0o600)
            ent.update(kind="file", sha256=sha(p), mode=stat.S_IMODE(os.stat(p).st_mode))
        else:
            ent.update(kind="absent")
        manifest.append(ent)
    json.dump(manifest, open(os.path.join(d, "manifest.json"), "w"), indent=1)
    os.chmod(os.path.join(d, "manifest.json"), 0o600)
    with open(os.path.join(d, "SHA256SUMS"), "w") as f:
        for e in manifest:
            if e["kind"] == "file":
                f.write(f"{e['sha256']}  {e['slot']}\n")
    for e in manifest:
        print(f"{e['kind']:8} {e['name']}")


def load(d):
    return json.load(open(os.path.join(d, "manifest.json")))


def verify(d):
    bad = 0
    for e in load(d):
        if e["kind"] == "file":
            ok = sha(os.path.join(d, e["slot"])) == e["sha256"]
            bad += not ok
            print(("OK       " if ok else "CORRUPT  ") + e["name"])
    return 1 if bad else 0


def restore(d, into=None, only=None):
    for e in load(d):
        if only and e["name"] != only:
            continue
        dst = os.path.join(into, e["path"].lstrip("/")) if into else e["path"]
        if e["kind"] == "absent":
            if not into and (os.path.lexists(dst)):
                os.remove(dst)
                print(f"removed  {e['name']} (did not exist before)")
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.lexists(dst):
            os.remove(dst)
        if e["kind"] == "symlink":
            os.symlink(e["target"], dst)
        else:
            shutil.copy2(os.path.join(d, e["slot"]), dst)
            os.chmod(dst, e["mode"])
        print(f"restored {e['name']}")


def compare(d):
    rc = 0
    for e in load(d):
        p = e["path"]
        if e["kind"] == "absent":
            st = "OK (still absent)" if not os.path.lexists(p) else "PRESENT (was absent)"
        elif e["kind"] == "symlink":
            st = "OK" if os.path.islink(p) and os.readlink(p) == e["target"] else "CHANGED"
        else:
            st = "MISSING" if not os.path.isfile(p) else ("OK (byte-identical)" if sha(p) == e["sha256"] else "CHANGED")
        rc |= not st.startswith("OK")
        print(f"{st:22} {e['name']}")
    return rc


if __name__ == "__main__":
    cmd, d = sys.argv[1], os.path.expanduser(sys.argv[2])
    rest = sys.argv[3:]
    if cmd == "backup":
        backup(d, rest or DEFAULT_FILES)
    elif cmd == "verify":
        sys.exit(verify(d))
    elif cmd == "restore":
        into = rest[rest.index("--into") + 1] if "--into" in rest else None
        only = rest[rest.index("--only") + 1] if "--only" in rest else None
        restore(d, into, only)
    elif cmd == "compare":
        sys.exit(compare(d))
    else:
        sys.exit(2)
