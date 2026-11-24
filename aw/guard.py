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
import shutil
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
KEYWORDS = {"{", "}", "do", "done", "then", "else", "elif", "fi", "if", "while", "until", "!", "function", "coproc"}
SHELLS = {"sh", "bash", "dash", "zsh", "ksh", "fish"}

# command word -> reason. Basenames; matched after wrappers are stripped.
DENY_BIN = {
    # desktop launchers / D-Bus activation
    "xdg-open": "desktop launcher", "gnome-open": "desktop launcher", "kde-open": "desktop launcher",
    "kde-open5": "desktop launcher", "kde-open6": "desktop launcher", "exo-open": "desktop launcher",
    "gtk-launch": "desktop launcher", "gtk4-launch": "desktop launcher", "gvfs-open": "desktop launcher",
    "sensible-browser": "opens the host browser", "x-www-browser": "opens the host browser",
    "www-browser": "opens the host browser", "gnome-www-browser": "opens the host browser",
    "open": "desktop launcher", "kioclient": "desktop launcher", "kioclient5": "desktop launcher", "kioclient6": "desktop launcher",
    "kfmclient": "desktop launcher", "mimeopen": "desktop launcher", "run-mailcap": "desktop launcher", "see": "desktop launcher",
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
# MCP servers / tools of the agents that drive the user's REAL browser or desktop. Claude Code's permission rules deny them
# while the guard is on (aw/guard_install.py); the workspace tools replace them.
HOST_BROWSER_MCP_DENY = ["mcp__claude-in-chrome"]

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
# Single-instance applications. Started from an agent's shell (which has the user's REAL home), a browser or an Electron
# app hands its work to the instance that is already running on the desktop through a socket in the real profile, so
# neither a faked display nor a hidden bus helps. They are denied unless the call is clearly harmless (headless,
# --version, extension management) AND does not name a real profile.
ELECTRON_APPS = re.compile(r"^(code(-insiders|-oss)?|codium|vscodium|cursor|windsurf|slack|discord|obsidian|signal-desktop|"
                           r"element-desktop|teams(-for-linux)?|spotify(-client)?|zoom|skype(forlinux)?|notion-app|"
                           r"1password|bitwarden|postman|insomnia|figma-linux|claude-desktop|chatgpt|whatsapp-for-linux|"
                           r"telegram-desktop|thunderbird|evolution|libreoffice|soffice|gimp|inkscape|vlc|"
                           r"agentdock|agent-dock)$", re.I)
APP_MANAGEMENT_OK = re.compile(r"(^|\s)(--list-extensions|--install-extension|--uninstall-extension|--update-extensions|"
                               r"--version|-v|--help|-h|--status|--convert-to|--headless|--cat|--print-to-pdf)(\s|$)")
REAL_PROFILE_RE = re.compile(r"\.config/(google-chrome\S*|chromium\S*|BraveSoftware|vivaldi\S*|microsoft-edge\S*|opera\S*|"
                             r"Slack|discord|obsidian|Code\b|VSCodium|Cursor|Claude)|\.mozilla|snap/(firefox|chromium)|"
                             r"\.librewolf|\.var/app|\.config/mozilla|\.thunderbird", re.I)
WEBBROWSER_RE = re.compile(r"(^|[\s;&|(`\"'])-m\s+webbrowser\b|\bwebbrowser\.(open|get|register)|\bimport\s+webbrowser\b|"
                           r"\bxdg-open\b.*\bos\.system|\bos\.(system|popen)\(.*(xdg-open|chrome|firefox)")
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
        while w and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w[0]) or w[0] in KEYWORDS):
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
    # a symlink or copy with another name must not hide a browser: look at what the word really is
    if not (BROWSERS.match(base) or ELECTRON_APPS.match(base) or base in DENY_BIN):
        try:
            real = os.path.realpath(shutil.which(w[0]) or w[0]) if ("/" in w[0] or shutil.which(w[0])) else ""
        except (OSError, ValueError):
            real = ""
        rb = os.path.basename(real) if real else ""
        if rb and rb != base and (BROWSERS.match(rb) or ELECTRON_APPS.match(rb) or rb in DENY_BIN):
            base = rb
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
    if base in ("ln", "cp", "mv", "install", "rsync") and any(
            BROWSERS.match(os.path.basename(a)) or ELECTRON_APPS.match(os.path.basename(a)) for a in args if not a.startswith("-")):
        return f"`{base}` of a browser or single-instance application binary (an alias would hide it from this guard)"
    if base == "flatpak" and args and args[0] in ("run", "override"):
        return "`flatpak run` (starts an app in the desktop session)"
    if BROWSERS.match(base) or ELECTRON_APPS.match(base):
        text = " " + joined + " "
        if REAL_PROFILE_RE.search(text):
            return f"`{base}` aimed at a real user profile (it would hand the page to the running host application)"
        harmless = HEADLESS_OK.search(text) or APP_MANAGEMENT_OK.search(text)
        separate = re.search(r"--user-data-dir[= ]\S+|(^|\s)-profile\s+\S+|--profile[= ]\S+", text) and BROWSERS.match(base)
        if not harmless and not separate:
            return (f"`{base}` (a single-instance application: it would open in, or hand its work to, the one already "
                    "running on the desktop)")
        # headless / management calls and a browser on a separate throw-away profile cannot attach to the host instance
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


HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def strip_heredocs(cmd: str) -> str:
    """Remove here-document BODIES that are data for a program (python, cat, tee, jq ...). A body fed to a shell (sh, bash, zsh,
    source, eval) is kept: those lines are commands. Without this, `python3 - <<EOF ... open(f) ... EOF` would be read as a
    command called `open`."""
    lines = cmd.split("\n")
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = HEREDOC_RE.search(line)
        i += 1
        if not m:
            continue
        tag = m.group(2)
        j = i
        while j < len(lines) and lines[j].strip() != tag and lines[j].lstrip("\t") != tag:
            j += 1
        body = lines[i:j]
        words = []
        for w in _split_simple(line[:m.start()]):
            words = _strip_wrappers(w) or words
        feeds_shell = bool(words) and (os.path.basename(words[0]) in SHELLS or words[0] in ("source", ".", "eval", "ssh"))
        if feeds_shell:
            out.extend(body)
        out.append(lines[j]) if j < len(lines) else None
        i = j + 1
    return "\n".join(out)


# --------------------------------------------------------------------------- data arguments are not assignments
# `echo "WAYLAND_DISPLAY=$WAYLAND_DISPLAY"`, `grep -n 'DISPLAY=' f`, `git commit -m "fix DISPLAY=..."` only mention the text. The
# assignment / socket-path rules therefore ignore a simple command whose command word merely prints or searches its arguments.
# Conservative on purpose: nothing is ignored when the command substitutes (`$(`, backticks), writes to a file, pipes into anything
# but a plain text filter, has a prefix assignment or wrapper before the command word, or uses an option that executes code.
DATA_CMDS = {"echo", "printf", "grep", "egrep", "fgrep", "rg", "cat", "head", "tail", "wc", "logger", "sed", "git"}
STRING_CMDS = {"echo", "printf", "logger", "git"}      # only print or record their arguments: never open a file argument
PIPE_SAFE = {"grep", "egrep", "fgrep", "rg", "head", "tail", "wc", "cut", "uniq", "cat", "nl", "tr"}
GIT_SAFE_CONFIG = {"user.name", "user.email", "commit.gpgsign", "core.quotepath"}
GIT_DATA_SUBCMDS = {"commit", "log", "tag", "grep", "show", "diff", "blame", "notes", "shortlog"}
SED_EXEC_RE = re.compile(r"(^|[\s;{}'\"/!0-9$,])e(\s|$)|\b[sy](.).*?\1.*?\1[A-Za-z]*e")


def _segments(cmd: str) -> List[Tuple[int, int, str]]:
    """(start, end, separator-after) of the top-level simple commands of `cmd`, quote and parenthesis aware."""
    out, start, i, n = [], 0, 0, len(cmd)
    quote, depth = None, 0
    while i < n:
        c = cmd[i]
        if quote == "'":
            if c == "'":
                quote = None
        elif quote == '"':
            if c == "\\":
                i += 1
            elif c == '"':
                quote = None
            elif c == "$" and cmd[i + 1:i + 2] == "(":
                depth += 1
                i += 1
        else:
            if c == "\\":
                i += 1
            elif c in "'\"":
                quote = c
            elif c == "$" and cmd[i + 1:i + 2] == "(":
                depth += 1
                i += 1
            elif c == "(" :
                depth += 1
            elif c == ")" and depth:
                depth -= 1
            elif depth == 0 and c in ";&|\n":
                sep = c
                j = i + 1
                while j < n and cmd[j] in "&|":
                    sep += cmd[j]
                    j += 1
                out.append((start, i, sep))
                start, i = j, j - 1
        i += 1
    out.append((start, n, ""))
    return out


def _unquoted_has(text: str, chars: str) -> bool:
    quote = None
    i = 0
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 1
            elif c == quote:
                quote = None
        elif c == "\\":
            i += 1
        elif c in "'\"":
            quote = c
        elif c in chars:
            return True
        i += 1
    return False


def _redirects_to_file(text: str) -> bool:
    plain = re.sub(r"\d*>&\d+|\d*>>?\s*/dev/null", "", _strip_quoted(text))
    return ">" in plain


def _strip_quoted(text: str) -> str:
    return re.sub(r"'[^']*'|\"(?:\\.|[^\"\\])*\"", "''", text)


def _is_data_segment(text: str, sep: str, next_text: str, strings_only: bool = False) -> bool:
    t = text.strip()
    if not t or "$(" in t or "`" in t or _redirects_to_file(t):
        return False
    words = t.split()
    first = words[0]
    if first not in (STRING_CMDS if strings_only else DATA_CMDS):
        return False
    rest = words[1:]
    if first == "printf" and rest[:1] == ["-v"]:
        return False
    if first == "rg" and any(w == "--pre" or w.startswith("--pre=") for w in rest):
        return False
    if first == "sed" and SED_EXEC_RE.search(t[len("sed"):]):
        return False
    if first == "git":
        i = 0
        while i + 1 < len(rest) and rest[i] == "-c" and rest[i + 1].split("=")[0] in GIT_SAFE_CONFIG:
            i += 2                                         # `git -c user.name=x commit`: harmless; `-c alias.x=!cmd` is not
        if i < len(rest) and rest[i].startswith("-"):
            return False
        sub = rest[i] if i < len(rest) else ""
        if sub not in GIT_DATA_SUBCMDS or (sub == "grep" and any(w.startswith("-O") or w.startswith("--open-files") for w in rest)):
            return False
    if sep.startswith("|"):
        nw = next_text.strip().split()
        if not nw or nw[0] not in PIPE_SAFE:
            return False
    return True


def mask_data_arguments(cmd: str, strings_only: bool = False) -> str:
    """`cmd` with every data-only simple command replaced by a bare `echo`, so that text sitting in its arguments is not read as an
    assignment or a socket path. Everything else is returned unchanged."""
    segs = _segments(cmd)
    if len(segs) == 1 and not segs[0][2]:
        a, b, sep = segs[0]
        return "echo" if _is_data_segment(cmd[a:b], sep, "", strings_only) else cmd
    out, last = [], 0
    for k, (a, b, sep) in enumerate(segs):
        nxt = cmd[segs[k + 1][0]:segs[k + 1][1]] if k + 1 < len(segs) else ""
        out.append(cmd[last:a])
        out.append(" echo " if _is_data_segment(cmd[a:b], sep, nxt, strings_only) else cmd[a:b])
        last = b
    out.append(cmd[last:])
    return "".join(out)



def check_command(cmd: str, depth: int = 0) -> Decision:
    """Decide whether the shell command may run. Allowed commands still run under the guard environment."""
    if not isinstance(cmd, str):
        return Decision()
    cmd = strip_heredocs(cmd)
    if WEBBROWSER_RE.search(cmd):
        return Decision(True, MSG.format(why="it opens the desktop browser through Python's webbrowser module"))
    pw = protected_write(cmd)
    if pw:
        return Decision(True, PROTECTED_MSG.format(what=pw))
    scan = mask_data_arguments(cmd)
    m = ASSIGN_RE.search(scan)
    if m and not (m.group(1) == "DISPLAY" and VIRTUAL_X.search(cmd)):
        return Decision(True, MSG.format(why=f"it assigns {m.group(1)} (the display variables are fixed by the guard)"))
    m = SOCKET_PATH_RE.search(mask_data_arguments(cmd, strings_only=True))     # `cat /run/user/N/bus` still names the socket
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
    if isinstance(tool, str) and (tool.startswith("browser_") or tool in ("computer_use", "bot_desktop")):
        return 0, json.dumps({"action": "block", "message": MSG.format(
            why=f"`{tool}` drives a browser or the desktop of the user (host-browser / computer-use tool)")})
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
