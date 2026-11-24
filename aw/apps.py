"""Discover launchable graphical applications (freedesktop .desktop entries) so an agent can answer
"open my browser / a text editor / the file manager" without the user supplying a command.

Pure functions, no daemon access. The result is only a list of argv arrays; launching goes through the
daemon (`workspace.launch`) which builds the workspace-only environment.
"""
from __future__ import annotations

import os
import re
import shlex
from typing import Dict, List, Optional

# Words an agent (or a user) uses, mapped to desktop-entry categories / MIME types.
ROLES = {
    "browser": ("WebBrowser",),
    "web browser": ("WebBrowser",),
    "internet": ("WebBrowser",),
    "editor": ("TextEditor",),
    "text editor": ("TextEditor",),
    "notepad": ("TextEditor",),
    "files": ("FileManager",),
    "file manager": ("FileManager",),
    "file browser": ("FileManager",),
    "calculator": ("Calculator",),
    "terminal": ("TerminalEmulator",),
    "image viewer": ("Viewer",),
}
FIELD_CODE = re.compile(r"%[a-zA-Z%]")


def _dirs() -> List[str]:
    home = os.path.expanduser("~")
    data = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    dirs = (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    return [os.path.join(d, "applications") for d in [data, *dirs] if d]


def parse_desktop(path: str) -> Optional[Dict]:
    entry: Dict[str, str] = {}
    section = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("["):
                    section = line
                    continue
                if section != "[Desktop Entry]" or "=" not in line or line.startswith("#"):
                    continue
                k, _, v = line.partition("=")
                if "[" not in k:                       # skip localised keys
                    entry[k.strip()] = v.strip()
    except OSError:
        return None
    if entry.get("Type") != "Application" or entry.get("NoDisplay") == "true" or entry.get("Hidden") == "true":
        return None
    if entry.get("Terminal") == "true" or not entry.get("Exec") or not entry.get("Name"):
        return None
    try:
        argv = [a for a in shlex.split(FIELD_CODE.sub("", entry["Exec"])) if a]
    except ValueError:
        return None
    if not argv:
        return None
    return {"id": os.path.basename(path)[:-len(".desktop")], "name": entry["Name"], "argv": argv,
            "categories": [c for c in entry.get("Categories", "").split(";") if c],
            "comment": entry.get("Comment", ""), "mime": [m for m in entry.get("MimeType", "").split(";") if m]}


def list_apps(deny: Optional[List[str]] = None, dirs: Optional[List[str]] = None) -> List[Dict]:
    """All launchable apps. `deny` is a list of lowercase substrings matched against id, name and argv[0]."""
    deny = [d.lower() for d in (deny if deny is not None else
                                [x for x in os.environ.get("AW_APP_DENY", "").split(",") if x.strip()])]
    seen, out = set(), []
    for d in (dirs or _dirs()):
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            if not n.endswith(".desktop") or n in seen:
                continue
            seen.add(n)
            e = parse_desktop(os.path.join(d, n))
            if not e:
                continue
            hay = " ".join([e["id"], e["name"], e["argv"][0]]).lower()
            if any(x.strip().lower() in hay for x in deny):
                continue
            out.append(e)
    return out


def find_app(query: str, apps: Optional[List[Dict]] = None) -> Optional[Dict]:
    """Resolve 'browser', 'Text Editor', 'nautilus', 'org.gnome.TextEditor' to one app, or None."""
    apps = apps if apps is not None else list_apps()
    q = query.strip().lower()
    if not q:
        return None
    for key in (lambda a: a["id"].lower(), lambda a: a["name"].lower(), lambda a: os.path.basename(a["argv"][0]).lower()):
        hit = [a for a in apps if key(a) == q]
        if hit:
            return hit[0]
    cats = ROLES.get(q)
    if cats:
        hit = [a for a in apps if any(c in a["categories"] for c in cats)]
        # prefer well-known default apps over exotic ones: shortest name first, stable otherwise
        hit.sort(key=lambda a: (len(a["name"]), a["name"]))
        if hit:
            return hit[0]
    hit = [a for a in apps if q in a["name"].lower() or q in a["id"].lower()]
    return hit[0] if len(hit) == 1 else (sorted(hit, key=lambda a: len(a["name"]))[0] if hit else None)
