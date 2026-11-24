"""Graphical isolation guard for an agent's own SHELL tool (Claude Code `Bash`, Hermes `terminal`).

AgentWorkspaces gives agents their own desktop. Nothing stops an agent from also running `firefox` or `xeyes`
in its shell, where the host display variables are inherited. This module is the safety rail for that path:

  1. FAIL-CLOSED ENVIRONMENT. Every command is rewritten to start with a prelude that makes the host display
     unreachable: WAYLAND_DISPLAY points at a socket name that does not exist, XDG_RUNTIME_DIR points at a private
     directory (so libwayland's default `wayland-0`, and the session bus `bus`, are not found), DISPLAY,
     XAUTHORITY, WAYLAND_SOCKET and DBUS_SESSION_BUS_ADDRESS are unset, and the toolkits are pinned to Wayland so
     they cannot fall back to another display. Ordinary programs are not affected.
  2. DENY for what an environment cannot stop: re-assigning the display variables, host socket paths, desktop
     launchers (xdg-open, gio open, gtk-launch, gapplication ...), D-Bus activation, GUI user units, desktop input and
     screenshot tools, a browser that would hand its window to a browser already running on the host.

It is a safety rail against an agent that follows its instructions imperfectly, NOT a sandbox against a hostile
agent: a script that builds the display path itself (a file containing `os.environ["DISPLAY"]="..."`), or a
program reading the host socket through some other path, is not caught. See docs/RELEASE.md for the exact rules.

The matching is deliberately conservative: it looks at the command WORD of every simple command (after env
assignments and wrappers such as env/nohup/setsid/timeout, and inside `sh -c`, `$( )`, backticks, `eval`), not at
arbitrary text, so `git commit -m "xdg-open"` or `grep xdg-open file` are fine.

CLI (used as the hook command):
  python3 -I guard.py claude     # Claude Code PreToolUse hook (stdin JSON -> stdout JSON)
  python3 -I guard.py hermes     # Hermes pre_tool_call shell hook
  python3 -I guard.py check CMD  # print the decision for one command (for humans and tests)
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from typing import List, Optional, Tuple

GUARD_DISPLAY_NAME = "aw-guard-no-wayland-display"
DISPLAY_VARS = ("DISPLAY", "WAYLAND_DISPLAY", "WAYLAND_SOCKET", "XAUTHORITY", "XDG_RUNTIME_DIR",
                "DBUS_SESSION_BUS_ADDRESS", "GDK_BACKEND", "QT_QPA_PLATFORM", "SDL_VIDEODRIVER")
# Entries of the real runtime dir that stay reachable inside the guard directory: they are not display or desktop
# services (systemd's private socket keeps `systemctl --user status` working; the session bus does NOT).
LINKS = ("systemd", "gnupg", "podman", "docker.sock", "docker", "containers")

PRELUDE_ENV = {
    "WAYLAND_DISPLAY": GUARD_DISPLAY_NAME,
    "GDK_BACKEND": "wayland", "QT_QPA_PLATFORM": "wayland", "SDL_VIDEODRIVER": "wayland",
    "CLUTTER_BACKEND": "wayland", "ELECTRON_OZONE_PLATFORM_HINT": "wayland", "OZONE_PLATFORM": "wayland",
    "MOZ_ENABLE_WAYLAND": "1", "BROWSER": "true", "GTK_USE_PORTAL": "0", "NO_AT_BRIDGE": "1",
}
PRELUDE_UNSET = ("DISPLAY", "XAUTHORITY", "WAYLAND_SOCKET", "DBUS_SESSION_BUS_ADDRESS", "SESSION_MANAGER",
                 "GNOME_SETUP_DISPLAY", "DESKTOP_STARTUP_ID", "XDG_ACTIVATION_TOKEN", "DBUS_STARTER_ADDRESS",
                 "DBUS_STARTER_BUS_TYPE")

WRAPPERS = {"env", "nohup", "setsid", "time", "exec", "command", "builtin", "timeout", "nice", "ionice", "stdbuf",
            "unbuffer", "chronic", "flock", "taskset", "sudo", "doas", "xargs", "watch", "script", "dbus-run-session",
            "systemd-cat", "firejail", "bwrap", "unshare", "chrt", "prlimit", "setarch", "runuser"}
SHELLS = {"sh", "bash", "dash", "zsh", "ksh", "fish"}

# command word -> reason. Basenames; matched after wrappers are stripped.
DENY_BIN = {
    # desktop launchers / D-Bus activation
    "xdg-open": "desktop launcher", "gnome-open": "desktop launcher", "kde-open": "desktop launcher",
    "kde-open5": "desktop launcher", "kde-open6": "desktop launcher", "exo-open": "desktop launcher",
    "gtk-launch": "desktop launcher", "gtk4-launch": "desktop launcher", "gvfs-open": "desktop launcher",
    "sensible-browser": "opens the host browser", "x-www-browser": "opens the host browser",
    "www-browser": "opens the host browser", "gnome-www-browser": "opens the host browser",
    "xdg-email": "desktop launcher", "xdg-desktop-menu": "desktop integration", "xdg-desktop-icon": "desktop integration",
    "xdg-settings": "desktop integration", "xdg-screensaver": "desktop control", "gnome-session-quit": "desktop control",
    "gnome-extensions": "desktop control", "gnome-shell": "desktop control", "gsettings": "desktop settings",
    "dconf": "desktop settings", "notify-send": "host notification",
    # input / window / screenshot / clipboard tools aimed at the host
    "xdotool": "host input tool", "ydotool": "host input tool", "ydotoold": "host input tool",
    "wtype": "host input tool", "dotool": "host input tool", "kdotool": "host input tool", "wmctrl": "host window tool",
    "xte": "host input tool", "xmacroplay": "host input tool", "evemu-play": "host input tool",
    "scrot": "host screenshot tool", "grim": "host screenshot tool", "slurp": "host screen tool",
    "gnome-screenshot": "host screenshot tool", "maim": "host screenshot tool", "flameshot": "host screenshot tool",
    "spectacle": "host screenshot tool", "xwd": "host screenshot tool", "import": "host screenshot tool (ImageMagick)",
    "wl-copy": "host clipboard", "wl-paste": "host clipboard", "xclip": "host clipboard", "xsel": "host clipboard",
    "wl-clipboard": "host clipboard",
}
# command words denied only for particular arguments
GIO_DENY_SUBCMDS = {"open", "launch"}
GAPPLICATION_DENY = {"launch", "action"}
DBUS_TOOLS = {"dbus-send", "gdbus", "busctl", "qdbus", "qdbus-qt5", "qdbus6", "dbus-launch"}
DBUS_TARGETS = re.compile(r"org\.(gnome|gtk|kde|freedesktop\.(Application|portal|FileManager1|Notifications|"
                          r"ScreenSaver|impl)|mozilla|chromium|flatpak)|--session\b|--user\b|--address|"
                          r"unix:path=|/run/user/", re.I)
SYSTEMD_GUI_UNIT = re.compile(r"^(app-|org\.|gnome|gtk|kde|plasma|dbus-org|xdg-desktop|snap\.)|\.desktop$|"
                              r"(firefox|chrome|chromium|brave|code|slack|calculator|nautilus|gedit|terminal|"
                              r"discord|spotify|steam|obs|blender|vlc|libreoffice|thunderbird)", re.I)
BROWSERS = re.compile(r"^(google-chrome(-stable|-beta|-unstable)?|chrome|chromium(-browser)?|brave(-browser)?|"
                      r"microsoft-edge(-stable)?|vivaldi(-stable)?|opera|firefox(-esr|-developer-edition)?|"
                      r"librewolf|epiphany|midori|tor-browser|start-tor-browser|falkon|qutebrowser|"
                      r"google-chrome-whitesur-frame)$")
HEADLESS_OK = re.compile(r"(^|\s)(--headless(=\S+)?|-headless|--screenshot|--dump-dom|--print-to-pdf|--version|-v|"
                         r"--help|-h)(\s|$)")
# explicit assignment of a display-relevant variable anywhere in the text (shell, python, JS ...)
ASSIGN_RE = re.compile(r"(?<![A-Za-z0-9_])(DISPLAY|WAYLAND_DISPLAY|WAYLAND_SOCKET|XAUTHORITY|XDG_RUNTIME_DIR|"
                       r"DBUS_SESSION_BUS_ADDRESS)(?![A-Za-z0-9_])[\"'\]\s]*(\+?=)(?!=)")
SOCKET_PATH_RE = re.compile(r"(?<![A-Za-z0-9_-])wayland-[0-9]+|/\.X11-unix\b|/run/user/[0-9]+/bus\b|/run/user/[0-9]+/wayland|"
                            r"/org\.freedesktop\.(portal|secrets)|/\.X[0-9]+-lock")
VIRTUAL_X = re.compile(r"\b(xvfb-run|Xvfb|Xvnc|Xephyr|Xnest|xvnc|x11vnc|Xwayland)\b")


# The guard's own configuration: an agent must not edit it (a safety rail the guarded program can rewrite is no rail).
PROTECTED_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])\.claude/settings(\.local)?\.json|(?<![A-Za-z0-9_])\.hermes/(config\.yaml|shell-hooks-allowlist\.json)|"
                               r"/agent-workspaces/(aw/guard|guard)|shell-hooks-allowlist\.json|\.claude\.json\b")
WRITE_HINT_RE = re.compile(r"(^|[\s;&|(])(sed|perl|ruby)\s+(-[A-Za-z]*i|--in-place)|(^|[\s;&|(])(tee|mv|cp|rm|truncate|ln|"
                           r"install|dd|chmod|chattr|patch)\s|(^|[^<>0-9&])>>?\s*\S|\b(open|write_text|write_bytes|"
                           r"json\.dump|yaml\.(safe_)?dump|shutil\.(copy|move))\b|\bsponge\b|"
                           r"\bhermes\s+(hooks\s+(revoke|remove|rm)|config)|\bclaude\s+(config|mcp\s+(add|remove))", re.I)
PROTECTED_MSG = ("Blocked by the AgentWorkspaces graphical-isolation guard: this would change the guard's own "
                 "configuration ({what}). Ask the user to change it themselves (agent-workspaces integrate ... "
                 "uninstall / --no-guard).")


def protected_write(text: str) -> Optional[str]:
    if re.search(r"\bhermes\s+hooks\s+(revoke|remove|rm)\b", text or ""):
        return "hermes hooks revoke"
    m = PROTECTED_PATH_RE.search(text or "")
    if m and WRITE_HINT_RE.search(text):
        return m.group(0).strip("/")
    return None


class Decision:
    def __init__(self, deny: bool = False, reason: str = ""):
        self.deny, self.reason = deny, reason

    def __repr__(self):
        return f"Decision(deny={self.deny}, reason={self.reason!r})"


MSG = ("Blocked by the AgentWorkspaces graphical-isolation guard: {why}. Do not open or control programs on the "
       "user's personal desktop from the shell. For any graphical program, website, screenshot, window or "
       "mouse/keyboard action use your agent-workspaces tools (launch_app, launch_browser, screenshot, mouse_*, "
       "keyboard_*): they work in your own separate desktop. Ordinary terminal and development commands are not "
       "affected.")


# --------------------------------------------------------------------------- shell scanning
def _split_simple(cmd: str) -> List[List[str]]:
    """Split a shell command into simple commands (lists of words). Quotes are removed, separators ; & | && || newline
    ( ) and backticks end a command, `$(...)` is expanded recursively by the caller via _subcommands()."""
    out: List[List[str]] = []
    cur: List[str] = []
    word: List[str] = []
    i, n = 0, len(cmd)
    inword = False
    quote: Optional[str] = None

    def end_word():
        nonlocal word, inword
        if inword:
            cur.append("".join(word))
        word, inword = [], False

    def end_cmd():
        nonlocal cur
        end_word()
        if cur:
            out.append(cur)
        cur = []

    while i < n:
        c = cmd[i]
        if quote == "'":
            if c == "'":
                quote = None
            else:
                word.append(c)
            i += 1
            continue
        if quote == '"':
            if c == "\\" and i + 1 < n:
                word.append(cmd[i + 1])
                i += 2
                continue
            if c == '"':
                quote = None
            else:
                word.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            if cmd[i + 1] == "\n":
                i += 2
                continue
            word.append(cmd[i + 1])
            inword = True
            i += 2
            continue
        if c in "'\"":
            quote, inword = c, True
            i += 1
            continue
        if c == "#" and not inword:
            while i < n and cmd[i] != "\n":
                i += 1
            continue
        if c in " \t":
            end_word()
            i += 1
            continue
        if c in ";&|\n()`":
            end_cmd()
            i += 1
            continue
        word.append(c)
        inword = True
        i += 1
    end_cmd()
    return out


def _subcommands(cmd: str) -> List[str]:
    """Texts of $( ... ) and `...` substitutions and of process substitutions, outermost level and nested."""
    subs: List[str] = []
    for m in re.finditer(r"\$\(|<\(|>\(", cmd):
        depth, j = 1, m.end()
        while j < len(cmd) and depth:
            depth += {"(": 1, ")": -1}.get(cmd[j], 0)
            j += 1
        subs.append(cmd[m.end():j - 1])
    subs += re.findall(r"`([^`]*)`", cmd)
    return subs


def _strip_wrappers(words: List[str]) -> List[str]:
    """Drop leading VAR=value words and wrapper commands (with their options) to reach the real command word."""
    w = list(words)
    for _ in range(12):
        while w and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w[0]):
            w.pop(0)
        if not w:
            return w
        base = os.path.basename(w[0])
        if base not in WRAPPERS:
            return w
        w.pop(0)
        # skip the wrapper's options/arguments conservatively
        if base == "env":
            while w and (w[0].startswith("-") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w[0])):
                opt = w.pop(0)
                if opt in ("-u", "-C", "-S", "--unset", "--chdir") and w:
                    w.pop(0)
        elif base in ("timeout",):
            while w and w[0].startswith("-"):
                w.pop(0)
            if w:
                w.pop(0)               # the duration
        elif base in ("nice", "ionice", "chrt", "taskset", "prlimit", "setarch"):
            while w and (w[0].startswith("-") or re.match(r"^[0-9a-fx,]+$", w[0])):
                w.pop(0)
        elif base in ("sudo", "doas", "runuser", "xargs", "flock", "script", "watch", "stdbuf", "unshare"):
            while w and w[0].startswith("-"):
                w.pop(0)
        else:
            while w and w[0].startswith("-") and base not in ("time", "command", "builtin", "exec"):
                w.pop(0)
    return w


def _check_words(words: List[str], raw: str, depth: int) -> Optional[str]:
    w = _strip_wrappers(words)
    if not w:
        return None
    base = os.path.basename(w[0])
    args = w[1:]
    joined = " ".join(args)
    if base in DENY_BIN:
        return f"`{base}` ({DENY_BIN[base]})"
    if base in ("gio",) and args and args[0] in GIO_DENY_SUBCMDS:
        return f"`gio {args[0]}` (desktop launcher)"
    if base == "gapplication" and args and args[0] in GAPPLICATION_DENY:
        return f"`gapplication {args[0]}` (D-Bus activation)"
    if base in DBUS_TOOLS and DBUS_TARGETS.search(joined + " " + " ".join(words)):
        return f"`{base}` aimed at the desktop session bus"
    if base == "systemd-run" and "--user" in args:
        return "`systemd-run --user` (starts programs in the desktop session)"
    if base in ("systemctl", "systemd-run") and "--user" in args:
        verbs = [a for a in args if not a.startswith("-")]
        if verbs and verbs[0] in ("start", "restart", "try-restart", "reload-or-restart", "kill", "isolate", "run"):
            units = verbs[1:]
            if any(SYSTEMD_GUI_UNIT.search(u) for u in units):
                return "`systemctl --user " + verbs[0] + "` of a graphical unit"
    if base in ("at", "batch"):
        return f"`{base}` (scheduled launch inside the desktop session)"
    if BROWSERS.match(base) and not HEADLESS_OK.search(" " + joined + " "):
        if not re.search(r"--user-data-dir|-profile\b|--profile\b", joined) or base.startswith("google-chrome-whitesur"):
            return (f"`{base}` without --headless (a running host browser would open the page in its own window)")
        # a separate profile cannot attach to the host instance, and the guard environment has no display
    if base in SHELLS:
        # sh -c '...', bash -lc '...'
        for k, a in enumerate(args):
            if a == "-c" or (a.startswith("-") and not a.startswith("--") and "c" in a[1:]):
                if k + 1 < len(args) and depth < 6:
                    return check_command(args[k + 1], depth + 1).reason if check_command(args[k + 1], depth + 1).deny else None
                break
    if base == "eval" and depth < 6:
        d = check_command(" ".join(args), depth + 1)
        return d.reason if d.deny else None
    if base in ("python", "python3", "node", "ruby", "perl") and False:
        return None
    return None


def check_command(cmd: str, depth: int = 0) -> Decision:
    """Decide whether the shell command may run. Allowed commands still run under the guard environment."""
    if not isinstance(cmd, str):
        return Decision()
    pw = protected_write(cmd)
    if pw:
        return Decision(True, PROTECTED_MSG.format(what=pw))
    m = ASSIGN_RE.search(cmd)
    if m and not (m.group(1) == "DISPLAY" and VIRTUAL_X.search(cmd)):
        return Decision(True, MSG.format(why=f"it assigns {m.group(1)} (the display variables are fixed by the guard)"))
    m = SOCKET_PATH_RE.search(cmd)
    if m:
        return Decision(True, MSG.format(why=f"it refers to a display/session-bus socket ({m.group(0).strip('/')})"))
    for words in _split_simple(cmd):
        why = _check_words(words, cmd, depth)
        if why:
            return Decision(True, MSG.format(why=why))
    for sub in _subcommands(cmd):
        d = check_command(sub, depth + 1) if depth < 6 else Decision()
        if d.deny:
            return d
    return Decision()


# --------------------------------------------------------------------------- environment prelude
def guard_dir(real_runtime_dir: Optional[str] = None) -> str:
    base = real_runtime_dir or os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    if os.path.basename(base).startswith("aw-guard"):
        return base
    return os.path.join(base, "aw-guard")


def ensure_guard_dir(real_runtime_dir: Optional[str] = None) -> str:
    base = real_runtime_dir or os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    d = guard_dir(base)
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
        if d != base:
            for name in LINKS:
                src, dst = os.path.join(base, name), os.path.join(d, name)
                if os.path.exists(src) and not os.path.lexists(dst):
                    os.symlink(src, dst)
    except OSError:
        pass
    return d


def prelude(real_runtime_dir: Optional[str] = None) -> str:
    d = ensure_guard_dir(real_runtime_dir)
    exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in PRELUDE_ENV.items())
    return (f"export XDG_RUNTIME_DIR={shlex.quote(d)} {exports}; unset {' '.join(PRELUDE_UNSET)}; ")


def rewrite(cmd: str, real_runtime_dir: Optional[str] = None) -> str:
    """The command with the fail-closed environment in front. The original text follows unchanged on the same line, so
    quoting, heredocs and `cd`/`export` state behave exactly as before."""
    return prelude(real_runtime_dir) + cmd


_PRELUDE_RE = re.compile(r"^export XDG_RUNTIME_DIR=\S+ WAYLAND_DISPLAY=" + GUARD_DISPLAY_NAME + r" [^;\n]*; unset [A-Z_ ]+; ")


def already_guarded(cmd: str) -> bool:
    return bool(_PRELUDE_RE.match(cmd))


def strip_prelude(cmd: str) -> str:
    m = _PRELUDE_RE.match(cmd)
    return cmd[m.end():] if m else cmd


# --------------------------------------------------------------------------- hook entry points
def _claude(data: dict) -> Tuple[int, str]:
    if data.get("tool_name") in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        ti = data.get("tool_input") or {}
        path = str(ti.get("file_path") or ti.get("notebook_path") or "")
        if PROTECTED_PATH_RE.search(path):
            return 0, json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                         "permissionDecisionReason": PROTECTED_MSG.format(what=os.path.basename(path))}})
        return 0, ""
    if data.get("tool_name") not in (None, "Bash"):
        return 0, ""
    ti = data.get("tool_input") or {}
    cmd = ti.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        return 0, ""
    d = check_command(strip_prelude(cmd))
    if d.deny:
        return 0, json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                     "permissionDecisionReason": d.reason}})
    ensure_guard_dir()
    # Allowed: no output. The fail-closed environment comes from settings.json "env" (aw/guard_install.py); rewriting
    # the command here would stop the user's own Bash permission rules from matching.
    return 0, ""


def _hermes(data: dict) -> Tuple[int, str]:
    tool = data.get("tool_name")
    args = data.get("tool_input") or {}
    if tool in ("write_file", "patch", "edit_file", "replace_file"):
        path = str(args.get("path") or args.get("file_path") or args.get("filename") or "")
        if PROTECTED_PATH_RE.search(path):
            return 0, json.dumps({"action": "block", "message": PROTECTED_MSG.format(what=os.path.basename(path))})
        return 0, ""
    if tool == "terminal":
        key, text = "command", args.get("command")
    elif tool == "execute_code":
        key, text = "code", args.get("code")
    else:
        return 0, ""
    if not isinstance(text, str) or not text.strip():
        return 0, ""
    if key == "command":
        d = check_command(strip_prelude(text))
    else:   # python source: the assignment / socket-path rules apply to text, command words do not
        d = Decision()
        m = ASSIGN_RE.search(text) or SOCKET_PATH_RE.search(text)
        if m:
            d = Decision(True, MSG.format(why="the code assigns a display variable or names a display socket"))
        elif re.search(r"\b(webbrowser\.open|os\.startfile)\b|xdg-open|gtk-launch|xdotool|ydotool|wtype", text):
            d = Decision(True, MSG.format(why="the code launches the desktop browser or a desktop launcher"))
    if d.deny:
        return 0, json.dumps({"action": "block", "message": d.reason})
    if key == "command":
        if already_guarded(text):
            return 0, ""
        return 0, json.dumps({"action": "modify", "args": {"command": rewrite(text)}})
    pre = ("import os as _aw_os\n" + "".join(
        f"_aw_os.environ[{k!r}] = {v!r}\n" for k, v in {"XDG_RUNTIME_DIR": ensure_guard_dir(), **PRELUDE_ENV}.items()) +
        "".join(f"_aw_os.environ.pop({k!r}, None)\n" for k in PRELUDE_UNSET) + "del _aw_os\n")
    if text.startswith("import os as _aw_os\n_aw_os.environ['XDG_RUNTIME_DIR']"):
        return 0, ""
    return 0, json.dumps({"action": "modify", "args": {"code": pre + text}})


def main(argv: List[str]) -> int:
    mode = argv[1] if len(argv) > 1 else ""
    if mode == "check":
        d = check_command(" ".join(argv[2:]))
        print("DENY: " + d.reason if d.deny else "ALLOW")
        return 1 if d.deny else 0
    try:
        data = json.loads(sys.stdin.read() or "{}")
        code, out = {"claude": _claude, "hermes": _hermes}[mode](data)
    except Exception as e:      # fail closed: a broken guard must not silently let commands through
        sys.stderr.write(f"agent-workspaces guard error: {type(e).__name__}: {e}\n")
        return 2
    if out:
        sys.stdout.write(out + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
