# Release notes: Claude Code and Hermes Agent integrations

What this release installs, how to check it, how to remove it and how to restore your configuration bit-exactly. Everything
here was exercised for real; what was not is said so. Results of the reliability pass are in
[compatibility-matrix.md](compatibility-matrix.md#release-reliability-pass-claude-code-and-hermes-agent).

## Install, status, uninstall

```bash
./install.sh                                   # AgentWorkspaces itself, user level, no root, no GNOME changes, no service enabled
agent-workspaces integrate claude-code install --dry-run     # prints every change; nothing is written
agent-workspaces integrate claude-code install               # MCP server + rule text + graphical-isolation guard
agent-workspaces integrate hermes install --dry-run
agent-workspaces integrate hermes install
agent-workspaces integrate claude-code status                # MCP registered? skill present? guard ON/OFF
agent-workspaces integrate hermes status
agent-workspaces integrate claude-code uninstall             # removes exactly what install added
agent-workspaces integrate hermes uninstall
agent-workspaces stop-daemon                                 # stop the background daemon (workspaces keep running)
```

`--no-guard` on `install` skips the guard (the MCP integration and rule text are still installed). Codex and Antigravity are
not touched by any of this.

### Restore from a backup (before you install)

```bash
python3 -I scripts/agent_backup.py backup  ~/agentworkspaces-backup-DATE          # copies the files below (0700 dir), prints names only
python3 -I scripts/agent_backup.py verify  ~/agentworkspaces-backup-DATE          # sha256 of every copy
python3 -I scripts/agent_backup.py restore ~/agentworkspaces-backup-DATE --into /tmp/restore-test   # restore test into a scratch dir
python3 -I scripts/agent_backup.py compare ~/agentworkspaces-backup-DATE          # live files vs backup: byte-identical or CHANGED
python3 -I scripts/agent_backup.py restore ~/agentworkspaces-backup-DATE          # put every file back (and remove files install created)
```

## What an install changes (and what it does not)

| Agent | File | Change |
|---|---|---|
| Claude Code | `~/.claude.json` | `claude mcp add --scope user`: one `mcpServers` entry `agent-workspaces` (command + env `AW_AGENT_KIND`, `AW_BASE_DIR`). Claude rewrites this file constantly (session state), so it is compared by entry, not by bytes |
| Claude Code | `~/.claude/skills/agent-workspaces/SKILL.md` | new file (the rule text), removed by uninstall if unchanged |
| Claude Code | `~/.claude/settings.json` | guard: `env` keys (fail-closed display environment), `permissions.deny` entry `mcp__claude-in-chrome`, one `PreToolUse` hook (matcher `Bash|Edit|Write|MultiEdit|NotebookEdit`). Existing keys, hooks and permissions are kept; uninstall removes only these and restores the file byte-for-byte |
| Hermes | `~/.hermes/config.yaml` | the MCP server `agent-workspaces` as a **marked text block inside the existing `mcp_servers:` mapping** (`hermes mcp add` is not used: it rewrites and migrates the whole file, so the change could not be undone byte for byte); guard: a marked `hooks:` block (`pre_tool_call`, `fail_closed`) appended at the end. Install refuses to guard if you already have a top-level `hooks:` key |
| Hermes | `~/.hermes/SOUL.md` | a marked block with the rule text appended (Hermes injects SOUL.md into its system prompt; it does not show an MCP server's own instructions to the model) |
| Hermes | `~/.hermes/shell-hooks-allowlist.json` | the consent entry Hermes requires for a new hook (file created if absent) |
| AgentWorkspaces | `~/.local/share/agent-workspaces/` (code), `~/.local/bin/agent-workspaces` (symlink), `~/.config/systemd/user/agent-workspaces.service` (**copied, not enabled**), `~/.config/agent-workspaces/guard-state.json` (which deny entries were added by us) | |

Not touched: Codex and Antigravity configuration, GNOME Shell, dconf, shortcuts, the GNOME extension (not enabled), Gnotch,
your browser profiles, any other Claude Code or Hermes setting, hook, plugin or MCP server.

## Hermes specifics you should know

* Hermes starts its MCP servers in your home directory, so every Hermes session shares ONE workspace named `hermes-home` (Claude Code gets one per project: `claude-code-<project>`).
* `hermes chat` starts a background gateway process (`hermes_cli.main gateway run --idle-exit`, started by Hermes itself, not by this project). It keeps the agent-workspaces MCP server
  alive between runs and exits when idle. If you change the integration (install, uninstall, update) and a Hermes run still behaves like the old one, stop that gateway (it is the process
  whose command line contains `gateway run --idle-exit`; `kill -TERM <pid>`; the next `hermes chat` starts a fresh one).
* Hermes does not show an MCP server's own instructions to the model; the rule text therefore lives in a marked block of `~/.hermes/SOUL.md`.
* The model matters: with `qwen3.8-27b-agent` 20 of 22 runs passed; a smaller 35B/3B-active MoE passed 4 and 6 of 11 (it answered with `cat` and `ls` instead of using the editor). See the compatibility matrix.

## Idle cost

No service is enabled. The MCP server starts when the agent starts it and does nothing until a graphical tool is called. On
the first call it starts the daemon (about 15 MiB) which, when it was started on demand, **exits by itself 30 minutes after the
last workspace is gone** (`AW_IDLE_EXIT_SECONDS`). `agent-workspaces stop-daemon` stops it at once. A workspace itself costs
about 210 MiB with one app (more with a browser) and about 1.6 % of a core with Chrome open; it is destroyed with
`agent-workspaces destroy NAME`.

## The graphical-isolation guard (default, removable)

Problem: an agent's own shell tool inherits your desktop. If it decides to run `firefox`, `xeyes` or `xdg-open URL`, a window opens
on YOUR screen, and a browser or Electron app started from that shell hands its page to the instance already running on your desktop
through your real profile. That happened once during testing (a test URL appeared in the user's running Chrome); see the incident
notes in [compatibility-matrix.md](compatibility-matrix.md#incident-during-testing).

What the guard does, per agent:

| | Claude Code | Hermes Agent |
|---|---|---|
| Mechanism | settings `env` (fail-closed environment for every Bash command) + a `PreToolUse` hook that only DENIES | a `pre_tool_call` shell hook (`fail_closed: true`) that REWRITES `terminal` commands to start with the guard environment and BLOCKS the rest |
| Why not the same | rewriting the command in a hook was measured to stop the user's own `Bash(...)` permission rules from matching, so Claude gets the environment from settings | Hermes builds the terminal environment itself (the caller's variables do not reach it), so the environment has to be set inside the command |
| Environment | `WAYLAND_DISPLAY` = a socket name that does not exist, `XDG_RUNTIME_DIR` = `<runtime dir>/aw-guard` (no session bus, no Wayland socket; `systemd`, `gnupg`, `docker` sockets are linked in), `DISPLAY`, `XAUTHORITY`, `WAYLAND_SOCKET`, `DBUS_SESSION_BUS_ADDRESS` emptied, toolkits pinned to Wayland, `BROWSER=true` | the same, as an `export ...; unset ...;` prefix |
| Other tools | denies the `mcp__claude-in-chrome` tools (the Claude-in-Chrome extension drives your real Chrome) while the guard is on | blocks `browser_*` (browser-harness/CDP) and `computer_use`; also checks `execute_code` |
| Config protection | denies Bash and Edit/Write changes to the guard's own settings and files | same for `write_file`/`patch` and `hermes hooks revoke` |

**Rules of the deny layer** (the exact list is `aw/guard.py`, tested by `tests/unit/test_guard.py`). The matching looks at the command
WORD of each simple command (after `VAR=x`, `env`, `nohup`, `setsid`, `timeout`, `sudo`, shell keywords, inside `sh -c`, `$( )`, functions,
loops), resolves symlinks, and never at arbitrary text, so `git commit -m "xdg-open"` and `grep xdg-open file` are fine. Denied:

* desktop launchers and D-Bus activation: `xdg-open`, `gio open|launch`, `gtk-launch`, `gapplication launch|action`, `gnome-open`, `exo-open`,
  `kde-open`, `kioclient`, `mimeopen`, `open`, `sensible-browser`, `x-www-browser`, `www-browser`, `flatpak run`, `dbus-send`/`gdbus`/`busctl`/`qdbus` aimed at
  the session bus or at `org.gnome.*`, `org.freedesktop.Application|portal|Notifications`, `systemd-run --user`, `systemctl --user start|restart ...` of graphical units, `at`, `batch`;
* `python -m webbrowser`, `webbrowser.open(...)`;
* host input, window, screenshot and clipboard tools: `xdotool`, `ydotool`, `wtype`, `wmctrl`, `scrot`, `grim`, `gnome-screenshot`, `import`, `xclip`, `wl-copy`, `wl-paste`, ...;
* single-instance applications (browsers: chrome, chromium, firefox, brave, vivaldi, edge, opera, ...; Electron and others: code, codium, slack, discord, obsidian, signal, thunderbird, libreoffice, ...) unless the call is clearly harmless (`--headless`, `--version`, extension management) and names no real profile (`~/.config/google-chrome`, `~/.mozilla`, ...). A browser on a separate `--user-data-dir` is allowed (the guard environment has no display);
* aliasing a browser: `ln`/`cp` of a browser binary;
* re-assigning `DISPLAY`, `WAYLAND_DISPLAY`, `WAYLAND_SOCKET`, `XAUTHORITY`, `XDG_RUNTIME_DIR`, `DBUS_SESSION_BUS_ADDRESS` (except `DISPLAY` together with `xvfb-run`/`Xvfb`), naming a host display or bus socket (`wayland-0`, `/run/user/N/bus`, `/tmp/.X11-unix`);
* writing the guard's own files (`~/.claude/settings*.json`, `~/.hermes/config.yaml`, the allowlist, `aw/guard.py`).

Everything else runs, in the guard environment: git, python, node, npm, pip, make, docker, ssh, curl, `systemctl --user status|list-*`, headless Chrome and Firefox,
`xvfb-run`, extension management of `code`, tests, builds. In the stand-in desktop tests 19 ordinary commands gave identical exit codes and output guarded and unguarded.

**What is enforced and what is only instructed.** Enforced: the environment (a launched GUI program finds no display and no bus), the deny rules, the Claude-in-Chrome
deny, the Hermes browser/computer-use block, the protection of the guard's files from the agent's own tools. Only instructed: the rule text ("use the workspace tools").

**What it cannot stop** (a safety rail, not a sandbox against a hostile or very creative agent): a script that builds the display variables itself
(`os.environ['WAYLAND'+'_DISPLAY']=...`, measured: it reaches a stand-in desktop); a program reading a host socket through a path the rules do not know; an agent that
changes the configuration with a tool the guard does not watch (it protects Bash, Edit, Write, MultiEdit, NotebookEdit and Hermes' file tools, not an arbitrary MCP server); a browser or Electron app
that is not in the list; anything an agent does through a process you started yourself; the Claude desktop app and other agents on your machine. The deny rule for `mcp__claude-in-chrome`
removes that extension's tools from Claude Code while the guard is on (the extension itself stays installed); remove the guard (`integrate claude-code uninstall`) to get them back.

Uninstall removes exactly what was added; `status` shows `guard ON/OFF`.

## Known limitations

See [limitations.md](limitations.md). In particular: Ubuntu GNOME on Wayland only; the agents' own permission prompts are not intercepted; no audio from workspaces to your speakers by default;
Hermes' terminal ignores the caller's environment, which is why the guard rewrites its commands.

## What is installed now, and how it was proven (2026-11-24)

Installed on the real configuration of this machine: AgentWorkspaces (`./install.sh`), `integrate claude-code install`, `integrate hermes install`, both with the guard ON.
Not installed: Codex, Antigravity, the GNOME extension, shortcuts, any service. A backup of the four existing files (`~/.claude.json`, `~/.claude/settings.json`,
`~/.hermes/config.yaml`, `~/.hermes/SOUL.md`) is in `~/agentworkspaces-backup-20261124` (mode 0700; sha256 checked; a restore test into a scratch directory was bit-exact before
anything was installed).

| Check | Result |
|---|---|
| Post-install smoke test through the real configuration, no integration flags, host display and bus variables removed from the process | Claude Code 3 of 3 (browser, editor, calculator screenshot), Hermes 3 of 3; evidence `docs/evidence/release/post-install/` |
| Uninstall both, then compare with the backup | `~/.claude/settings.json`, `~/.hermes/config.yaml`, `~/.hermes/SOUL.md`: **byte-identical**; `~/.hermes/shell-hooks-allowlist.json` and the Claude skill: absent again; `~/.claude.json`: its `mcpServers` equal to the backup, no `agent-workspaces` entry (the file is rewritten by every running Claude session, so only that part is comparable; the other differing keys are cached feature flags and counters) |
| Reinstall, then one request each | works for both (Claude: calculator; Hermes: calculator) |
| A defect found by this smoke test | after a workspace was destroyed while a long-lived MCP server still held it, the MCP server kept answering "no credentials" and Hermes fell back to its terminal for 10 minutes: fixed (it now creates the workspace again), unit-tested and re-run on the real configuration: Hermes opened the editor again after the workspace had been destroyed |

Roll back completely: `agent-workspaces integrate claude-code uninstall`, `agent-workspaces integrate hermes uninstall`, then `agent-workspaces stop-daemon`; or restore
from the backup with `scripts/agent_backup.py restore` (also removes files that install created).

## What you still have to approve

* Codex and Antigravity integrations (not part of this release; their last results are in the compatibility matrix).
* The GNOME Shell extension (installed nowhere, not enabled) and Gnotch.
* Enabling the systemd user service (the unit is copied, not enabled).
* Publishing: nothing was pushed except the branch `feat/release-agents`; no release was cut.

