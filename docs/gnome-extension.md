# GNOME Shell extension: investigation, design, evidence

The extension is optional. Everything it does can be done with `agent-workspaces enter NAME`; the extension adds
Ctrl+Alt+Up (go up to the agent desktop, stacked above your personal ones) and Ctrl+Alt+Down (come back down), and shows
the agent desktops in the Super overview. Files: `gnome-extension/` (install, rollback and behaviour in its
`README.md`).

Machine investigated: Ubuntu 26.04.1, GNOME Shell 50.1, Mutter 50, Wayland, Tiling Shell, desktop-cube,
V-Shell installed but disabled. All values below were read with `gsettings get` / by reading source files. Nothing
was changed in the live session, and the extension was never installed into or enabled in it.

## 1. How GNOME handles the workspace keys

Live values (read only):

| key (`org.gnome.desktop.wm.keybindings`) | value |
|---|---|
| `switch-to-workspace-up` | `['<Control><Alt>Up']` |
| `switch-to-workspace-down` | `['<Control><Alt>Down']` |
| `switch-to-workspace-left` | `['<Super>Page_Up', '<Super>KP_Prior', '<Super><Alt>Left', '<Control><Alt>Left']` |
| `switch-to-workspace-right` | `['<Super>Page_Down', '<Super>KP_Next', '<Super><Alt>Right', '<Control><Alt>Right']` |
| `move-to-workspace-up/down` | `['<Control><Shift><Alt>Up']` / `['<Control><Shift><Alt>Down']` |
| `org.gnome.mutter dynamic-workspaces` | `true`; `workspaces-only-on-primary` `false` |

The handler is GNOME Shell's `windowManager.js` (`_showWorkspaceSwitcher`, extracted from the installed
`libshell-18.so` resource bundle). With the default **horizontal** layout (`layout_columns === -1`) it returns
without doing anything when the direction is UP or DOWN:

```js
if (workspaceManager.layout_columns === -1 &&
    direction !== Meta.MotionDirection.LEFT &&
    direction !== Meta.MotionDirection.RIGHT)
    return;
```

So on this machine Ctrl+Alt+Up/Down are bound by GNOME but do nothing (derived from source; the live session was
not driven). Taking them over costs no existing behaviour. They would only mean something with a vertical layout.

Animation: Mutter emits `switch-workspace(from, to, direction)` with a direction along the layout, and the Shell
animates with `WorkspaceAnimationController.animateSwitch`, which lays workspaces out horizontally (`vertical =
layout_rows === -1`). A horizontal layout therefore cannot produce a vertical slide by itself.

## 2. Other extensions

| extension | what it owns that matters here |
|---|---|
| Tiling Shell 18.0 | `Main.wm.addKeybinding` for its own keys (move/span/focus window up/down/left/right, ...). Defaults: `move-window-up` = `<Super>Up`, others empty. None default to Ctrl+Alt. No workspace-layout or switch-animation changes. |
| desktop-cube (v35) | No keybindings. Replaces the prototype methods `WorkspaceAnimationController._prepareWorkspaceSwitch` and `_finishWorkspaceSwitch`, so **every** animated workspace switch becomes a horizontal cube rotation. A normal switch to a "below" workspace would be drawn as a cube turn, not a vertical move. |
| V-Shell (disabled) | Rewrites `org.gnome.desktop.wm.keybindings` (adds Super+PageUp/Down to up/down or left/right), calls `override_workspace_layout` (vertical or horizontal), replaces `Meta.Workspace.prototype.get_neighbor`, ships its own switch animation. With it on, Ctrl+Alt+Up/Down already mean "workspace above/below". |
| one further enabled extension | enabled in the live session but not present in `~/.local/share/gnome-shell/extensions`; not inspected. |

### What was checked about Desktop Cube (the user's session has it enabled)

Read from its source (`desktop-cube@schneegans.github.com` v35) and checked in the isolated shell:

* It registers **no keybindings** and uses the vertical axis only for mouse-drag pitch. Ctrl+Alt+Up/Down are free of it.
* Its hook is a monkey patch of `WorkspaceAnimationController.prototype._prepareWorkspaceSwitch` and `_finishWorkspaceSwitch`
  (it re-parents a skybox and connects `notify::progress` on every monitor group between the two). Anything that
  starts only one of the two would leave the cube half-set-up.
* The extension's own switch never calls either: its instance override of `animateSwitch` returns before
  `_prepareWorkspaceSwitch` runs, so the cube is neither entered nor left half-way. Verified by wrapping the patched
  method in the shell and counting calls: 0 after Ctrl+Alt+Up + Ctrl+Alt+Down, 1 after Ctrl+Alt+Right, 2 after
  Ctrl+Alt+Left; afterwards no switch is in flight (`_switchData` empty) and the window group is untransformed
  (`translation_y = 0`, `rotation_angle_y = 0`, opacity 255). The skybox option was off in the developer's settings, so
  the skybox re-parenting itself was **not** exercised.
* Visible result (frames in `gnome-extension/media/cube-left-right-frames.png` and `slide-up-frames.png`):
  Ctrl+Alt+Left/Right turn the cube (Desktop Cube's own code); Ctrl+Alt+Up/Down slide the stacked agent desktop in and out.
  The agent desktop is **never a cube face** and never uses the cube rotation (an earlier cube-turn design was dropped).

### V-Shell (vertical-workspaces)

Enabled privately next to this extension with the developer's own V-Shell settings (these keep a horizontal workspace
layout): `layout_rows` stays 1, Ctrl+Alt+Up enters, Ctrl+Alt+Down returns, Ctrl+Alt+Right navigates, no JS errors. With a
V-Shell *vertical* layout the code passes the keys to the neighbour switch instead (so V-Shell keeps Ctrl+Alt+Up for
"workspace above"); that branch was **not** run against a real vertical V-Shell layout. Change `enter-shortcut` in the
preferences if that is your setup.

Consequences for the design:

* No default accelerator conflict with Tiling Shell or desktop-cube.
* desktop-cube makes any use of GNOME's own animation look like a cube. The extension therefore **skips** GNOME's
  animation for its own enter/return switch (one-shot instance override of `animateSwitch`, restored immediately)
  and plays its own vertical slide. Left/Right keep GNOME's (cube) animation.
* With V-Shell's vertical layout the keys are passed through to the neighbour switch instead of entering.

## 3. Design (current)

**Direction.** The agent desktop is ON TOP of the personal ones. Ctrl+Alt+Up goes up to it, Ctrl+Alt+Down comes back to the
personal workspace you came from. GNOME binds both accelerators to `switch-to-workspace-up/down` (no-ops in the horizontal
layout); the extension removes exactly those two accelerators from GNOME's keys while it is enabled, records them in its own
`backup` setting before changing anything, and merges them back on disable, on a shortcut change and at the next start after a
crash. Both shortcuts are GSettings (`enter-shortcut`, `return-shortcut`) and editable in the preferences. Ctrl+Alt+Left/Right
and every other key are never touched. Any other binding on the same accelerator (another GNOME key, another extension) is
listed under `conflicts`, never edited.

**The stacked slide, also with Desktop Cube on.** Going up, the personal desktop slides DOWN and out while the agent desktop
slides down into place from above; coming back is the reverse. Two full-screen faces move together: the live window group
(which already carries the real wallpaper, because the shell keeps its background group inside it) and a snapshot of the face
you leave with its own wallpaper (a fresh `BackgroundManager`), clones of its windows and of your top bar and dock, which belong
to your personal world and leave with it (the real ones are hidden for the 250 ms and fade back in, 160 ms, when you arrive on
a personal workspace). 250 ms, ease-out-cubic, like GNOME's own workspace slide: no black gap, no flash (the first and last
frames equal the live desktop). GNOME's animation is skipped for this one switch (a one-shot instance override of
`animateSwitch`), so Desktop Cube's `_prepareWorkspaceSwitch` / `_finishWorkspaceSwitch` are never entered half-way and its
left/right rotation is untouched. An earlier design turned the cube; it was dropped: the agent desktop is not a cube face.

**Left/Right stay personal.** The agent desktop is a GNOME workspace too (it holds the viewer). `Meta.Workspace.get_neighbor` is
wrapped: the agent workspace is skipped in the row (the trailing empty workspace GNOME keeps stays reachable, so new
workspaces can still be started) and Left/Right cannot sidestep out of it. Our own moves bypass the wrapper. Known: GNOME's
workspace-switcher popup still draws the agent workspace as a dot.

**Silent unless there is something to go up to.** Ctrl+Alt+Up asks the service (bounded start, below) for its workspaces and does
**nothing at all** unless at least one is running: no viewer window, no overlay, no notification, no animation, nothing in the
overview. The same for Ctrl+Alt+Up from the overview, and for a service that cannot be started (the reason goes to the shell
log only). Nothing is created at login or when the extension is enabled; the overview strip is built the first time the overview
opens after start-up. The only message the extension ever shows on its own is when you did go up (workspaces exist) but the
viewer could not be opened: it goes back down and says so.

**The service.** If the socket does not answer: `systemctl --user start agent-workspaces.service` (5 s), then, if that did not
bring the socket up, `setsid -f <agent-workspaces> daemon` (the `daemon-command` setting, else `~/.local/share/agent-workspaces/bin`,
`~/.local/bin`, PATH), waiting at most 6 s. The viewer does the same for itself and shows a quiet message with a "Start the
service" button instead of hiding. `install.sh --enable-service` enables and starts the unit (`uninstall.sh` disables and removes
it): without it the daemon only runs while an agent or the CLI has started it, and exits when idle.

**From the Super overview.** Ctrl+Alt+Up and Ctrl+Alt+Down are registered for the overview too: it closes first, then the slide
runs. Pressing the key again during a slide queues it (only the last request counts); cancelling would make the desktops jump.
A locked session ends everything queued or sliding (GNOME also disables extensions on the lock screen, which does the same).

**Agent desktops in the overview** (`overviewStrip.js`, `stylesheet.css`). A strip above everything: one compact tile per RUNNING
agent workspace, a live preview, the workspace name, the agent's name and a thin orange line under the preview (the same orange as
the agent cursor and taskbar); nothing when none runs (no hint either: a hint would be an element with nothing to say). Click a tile
to go up to that workspace (the overview closes, then the slide). Built as its own widget, not by patching GNOME's thumbnails: the
overview's controls (search, personal thumbnails, workspaces, dash) are moved down and scaled uniformly to make room, because they
lay themselves out against the monitor so shrinking their box does not work; this behaves the same with Just Perfection, Blur my
Shell, Dash to Dock and Tiling Shell (checked with the developer's own settings). The agent workspace's own GNOME thumbnail and its
peek at the edge of the workspace view are hidden: one place for it, not two. Cost: nothing while the overview is closed; when it
opens one `workspace.list` (only if the service is already up: the overview never starts it) and one 320 px `screen.screenshot`
(`quiet`, so it is not activity) per tile every 1.5 s, one at a time, until it closes. Known: hiding GNOME's own agent thumbnail
leaves a one-thumbnail gap in the personal thumbnail row.

**The viewer is the UI** (docs/viewer.md): full-screen environment (setting `environment-mode`), its own orange-lined taskbar,
the agent's orange pointer; "Back to desktop" calls the extension's session-bus method `Return`. If the viewer window is closed by
hand while you are up there, you are taken back down (an empty agent desktop would be a trap); pressing Up on an empty agent
desktop reopens the viewer.

Settings (`org.gnome.shell.extensions.agent-workspaces`): `enter-shortcut` (default `<Control><Alt>Up`), `return-shortcut`
(`<Control><Alt>Down`), `create-on-demand`, `environment-mode`, `daemon-command`. The earlier `use-cube-transition` key is gone.

## 4. Defects found while testing entering and leaving (and fixed)

* Ctrl+Alt+Right from the last personal workspace walked into the agent desktop (it is a workspace in the row): now skipped.
* Entering with no workspace running left you on an empty, invisible desktop and showed a notification: now a silent no-op.
* Pressing Ctrl+Alt+Up while sitting on an empty agent desktop (viewer closed by hand) did nothing; closing the viewer stranded you
  there: now reopened / taken back down.
* The overview strip built while the shell was still starting up (it starts in the overview) crashed the shell on the next
  login in the crash-restart test: now never built during start-up.
* Entering from the overview, rapid repeated keys, bar and dock opacity left at a mid-fade value, keyboard focus after leaving,
  and the viewer's attach count under per-request credentials were checked (the last one was a real leak, fixed earlier).

## 5. Test evidence (isolated headless GNOME Shell 50.1; private buses; the harness aborts if the D-Bus address is missing, the real one, or outside the isolated session; nothing runs on or touches the real session)

| suite | what | result |
|---|---|---|
| `gnome-extension/tests/run_isolated.py` | stand-in daemon and viewer: bindings taken over / restored exactly (including after a SIGKILL and a restart), silent no-op without a service, slide direction and samples (-720 to 0 going up, +720 to 0 coming back), Left/Right skip the agent desktop, desktop-cube + Tiling Shell coexistence, prefs write | 27/27 |
| `gnome-extension/tests/run_cube.py --personal 2` and `--personal 4` | the real stack with Desktop Cube, Tiling Shell, Blur my Shell, Just Perfection, User Themes, Ubuntu Dock (settings read into a private keyfile): slide (never a cube) both ways, nothing left behind, Super overview strip (tile, live preview, controls scaled, agent thumbnail hidden), click a preview, Up/Down from the overview, rapid keys, focus returns, Left/Right walk the personal workspaces with the cube's own turn and never land on the agent desktop, viewer closed by hand, no JS errors, disable restores the keys | 25/25 and 25/25 |
| `gnome-extension/tests/run_silent.py` | (a) daemon up and no workspace, (b) daemon down and no workspace, (d) empty overview, nothing at enable time: no window actor, overlay, animation or notification; the service is started by the extension and re-found after a restart; (c) with one running workspace it enters | 12/12 |
| `gnome-extension/tests/run_real.py` | the viewer as an ordinary window: control, approvals, grants, create/stop/close, taskbar, agent pointer | see docs/testing.md |
| `tests/unit/test_install_service.py` | `install.sh --enable-service` and `uninstall.sh` against a fake `systemctl` in a throw-away HOME | 3/3 |

Demo: `docs/media/demo-top-desktop.mp4` (37 s, 60 fps, made by `gnome-extension/tests/demo_video.py`): personal desktop with two windows;
Ctrl+Alt+Right/Left turn the Desktop Cube; Super shows two agent desktops above the personal ones with live previews; Ctrl+Alt+Up
from the overview closes it and the agent desktop slides down into place from above while the personal desktop slides down and out;
the agent focuses the calculator and the editor and glides its chunky orange pointer to buttons (clicks 7 x 6 =) with the
taskbar highlight following; Ctrl+Alt+Down slides back; the work has progressed on the second visit (Ctrl+Alt+Up from the
normal desktop); the cube still turns. The "agent" in it is a **scripted AgentWorkspaces client** (the same daemon API an MCP
agent uses, with the workspace credential), **not a model-driven agent**. Frames: `gnome-extension/media/overview-agent-desktops.png`,
`overview-animation-frames.png`, `slide-up-frames.png`, `slide-down-frames.png`, `agent-cursor-glide-frames.png`,
`cube-left-right-frames.png`.

## 6. Not tested

* The real session, a physical display, multi-monitor, fractional scaling; V-Shell with a vertical layout; Desktop Cube's skybox
  (off in the developer's settings).
* The lock screen itself (GNOME disables extensions there; the cleanup in `disable()` is covered, a real lock is not driven).
* A model-driven agent in the demo.
* `systemctl --user start` of the real unit (the isolated sessions cannot reach any systemd; the start path that was exercised is
  the daemon fallback, the unit path is the same call the CLI uses).
