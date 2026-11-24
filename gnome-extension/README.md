# AgentWorkspaces GNOME Shell extension (optional)

Ctrl+Alt+Down enters the AgentWorkspaces viewer on its own GNOME workspace. Ctrl+Alt+Up returns to the personal
workspace you came from. The move is a vertical slide. Ctrl+Alt+Left/Right and every other workspace key are left
alone.

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
`create-on-demand` (`b`). The keys `backup`, `conflicts`, `test-hooks` are state, not options.

## How it behaves

* Enter: records the workspace you are on, finds the workspace that holds the viewer window (window app id
  `dev.agentworkspaces.Viewer`, or title starting with `AgentWorkspaces:`), or creates one (the empty workspace
  GNOME keeps at the end, or a new one) and asks the daemon (`workspace.enter`, `docs/api.md`) to open the viewer
  on it. With no daemon running it still switches, and shows a notice with the command to start it.
* Return: back to the recorded workspace. If it no longer exists (GNOME removes empty workspaces), to the
  neighbouring one.
* On a vertical workspace layout (for example with V-Shell) Ctrl+Alt+Down already means "next workspace", so the
  keys are passed through to the normal neighbour switch.
* Animations follow GNOME's *enable-animations* setting and are skipped while the Overview is open.

## Tests

`python3 tests/run_isolated.py` starts one headless GNOME Shell completely cut off from your session (private
HOME/XDG dirs, GSETTINGS_BACKEND=keyfile, private session bus and a private stand-in for the system bus so GDM,
logind and polkit are unreachable), installs the extension there only, injects key events inside that shell and
prints PASS/FAIL per check. `--coexist` runs a shorter check next to desktop-cube and tilingshell (private copies).
`--prefs out.png` opens the preferences window in the isolated shell and saves a screenshot. It uses
`/run/user/$UID/aw-ext-test/` and removes it afterwards (`--keep` leaves it). Needs `gnome-shell`, `dbus-daemon`,
`glib-compile-schemas`, python3-gi with Gtk 4. Nothing is sent to your running session.
