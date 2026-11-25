# AgentWorkspaces GNOME Shell extension (optional)

The agent desktop is stacked ON TOP of your personal ones. **Ctrl+Alt+Up** goes up to it (your personal desktop slides down
and out, the agent desktop slides down into place from above); **Ctrl+Alt+Down** comes back to the personal workspace you
came from. Ctrl+Alt+Left/Right still walk through your personal workspaces only (the agent desktop is skipped), also with
Desktop Cube, which keeps doing its own rotation. The Super overview shows the running agent desktops in a strip above the
personal ones; click one to go up to it. With no agent workspace running the extension does nothing at all: no window, no
popup, no animation, nothing in the overview.

AgentWorkspaces works fully without this extension (`agent-workspaces enter NAME` opens the viewer window from any
terminal or from Gnotch). The extension only adds the keyboard navigation.

Target: GNOME Shell 50 (Ubuntu 26.04), Wayland. Details, evidence and limits: `../docs/gnome-extension.md`.

## Install (user level, nothing is enabled for you)

```sh
./install.sh                          # copies to ~/.local/share/gnome-shell/extensions/ and compiles the schema
gnome-extensions enable agent-workspaces@agentworkspaces.dev     # when you decide to
```

Log out and in once if GNOME does not see a newly installed extension.

## Roll back

```sh
gnome-extensions disable agent-workspaces@agentworkspaces.dev    # restores the keybindings it removed
./install.sh --uninstall
```

## What it changes while enabled

* GNOME binds Ctrl+Alt+Up and Ctrl+Alt+Down to *switch-to-workspace-up/down* (no-ops in the default horizontal
  workspace layout). The extension takes those two accelerators over. It removes **only** the accelerators it
  needs from `org.gnome.desktop.wm.keybindings` `switch-to-workspace-up` and `switch-to-workspace-down`. The
  original lists are written to its own `backup` setting *before* the change, and put back when the extension is
  disabled, when the shortcut setting changes, and at the next start if the shell ended while they were removed
  (crash, power loss).
* A restore merges into the current value, so an accelerator you added in the meantime is kept.
* Any other binding that uses the same accelerators (another GNOME key, another extension's schema) is **not
  touched**. It is listed under `conflicts` and shown in the preferences window.
* It never edits Left/Right, `move-to-workspace-*`, or any other extension's settings.

## Settings

Preferences window (`gnome-extensions prefs agent-workspaces@agentworkspaces.dev`): the two shortcuts (click a
shortcut, press a new one with Ctrl, Alt or Super; the arrow button restores the default) and *Create the
AgentWorkspaces workspace on demand*.

GSettings schema `org.gnome.shell.extensions.agent-workspaces`: `enter-shortcut`, `return-shortcut` (`as`),
`create-on-demand` (`b`), `environment-mode` (`b`, open the viewer full screen), `daemon-command` (`s`, how to start the service
if its systemd unit cannot). The keys `backup`, `conflicts`, `test-hooks` are state, not options.

## How it behaves

* Up: always goes up. The service is started first if it is not up (the systemd user unit, else the daemon itself, bounded
  waits; see `../docs/gnome-extension.md`) and, when nothing is running, asked for the default desktop (`workspace.ensure_default`:
  a workspace called `agents`, no agent assigned, created or started on the first Up; never at login). The slide starts when the
  viewer has its first frame. Records the workspace you are on and
  the window that has focus, finds or creates the GNOME workspace that holds the viewer (the empty workspace GNOME keeps at
  the end, or a new one), slides to it and asks the daemon (`workspace.enter`, `docs/api.md`, admin credential: `AW_ADMIN_TOKEN`,
  the file `AW_ADMIN_TOKEN_FILE`, else `<base>/admin.token`) to open the viewer there. Never a notification or popup: if something is wrong the viewer says so, once, in its own window.
* Down: back to the recorded workspace and the window you were in; if it no longer exists (GNOME removes empty workspaces), to
  the neighbouring one. Closing the viewer window by hand while up there takes you back down.
* Super overview: Ctrl+Alt+Up/Down work there too (the overview closes first). Agent previews appear above the personal
  workspaces for the workspaces that are running (after the first Up that includes `agents`); they are refreshed only while the overview is open.
* On a vertical workspace layout (for example with V-Shell) Ctrl+Alt+Up already means "the workspace above", so the keys are
  passed through to the normal neighbour switch.
* Animations follow GNOME's *enable-animations* setting and are skipped while the Overview is open.

## Desktop Cube, Tiling Shell, V-Shell

Checked in isolated shells, see `../docs/gnome-extension.md`: Desktop Cube keeps turning for Ctrl+Alt+Left/Right and is
not touched by the vertical slide (the agent desktop is never a cube face); Tiling Shell binds no Ctrl+Alt+Up/Down by default (a foreign binding on the same
accelerator is reported, never edited); V-Shell with a horizontal layout coexists. Not run: V-Shell with a vertical layout.

## Tests

`python3 tests/run_isolated.py` starts one headless GNOME Shell completely cut off from your session (private
HOME/XDG dirs, GSETTINGS_BACKEND=keyfile, private session bus and a private stand-in for the system bus so GDM,
logind and polkit are unreachable), installs the extension there only, injects key events inside that shell and
prints PASS/FAIL per check. `--coexist` runs a shorter check next to desktop-cube and tilingshell (private copies).
`python3 tests/run_real.py` is the same isolation with the real daemon, workspaces and viewer and the developer's own
extension set (53 checks); `tests/demo_video.py OUT.mp4` records the demo; `tests/measure_gs.py` measures CPU, memory and latency.
`--prefs out.png` opens the preferences window in the isolated shell and saves a screenshot. It uses
`/run/user/$UID/aw-ext-test/` and removes it afterwards (`--keep` leaves it). Needs `gnome-shell`, `dbus-daemon`,
`glib-compile-schemas`, python3-gi with Gtk 4. Nothing is sent to your running session.
