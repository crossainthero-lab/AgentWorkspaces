# GNOME Shell extension: investigation, design, evidence

The extension is optional. Everything it does can be done with `agent-workspaces enter NAME`; the extension adds
Ctrl+Alt+Down / Ctrl+Alt+Up navigation. Files: `gnome-extension/` (install, rollback and behaviour in its
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

Consequences for the design:

* No default accelerator conflict with Tiling Shell or desktop-cube.
* desktop-cube makes any use of GNOME's own animation look like a cube. The extension therefore **skips** GNOME's
  animation for its own enter/return switch (one-shot instance override of `animateSwitch`, restored immediately)
  and plays its own vertical slide. Left/Right keep GNOME's (cube) animation.
* With V-Shell's vertical layout the keys are passed through to the neighbour switch instead of entering.

## 3. Design

* **One dedicated workspace** for the viewer window(s). Found by window (app id `dev.agentworkspaces.Viewer` or
  title prefix `AgentWorkspaces:`), else created: the trailing empty workspace GNOME keeps (dynamic workspaces) or
  a new one. Entering with no viewer asks the daemon (`workspace.enter`, `docs/api.md`) to open it. The extension
  does not need the daemon to load; without it, Enter still switches and shows a notice.
* **Slide**: snapshot the old windows as Clutter clones (they keep painting after GNOME hides the real actors),
  switch with GNOME's animation suppressed, then ease the clones out and the live `window_group` in along Y over
  250 ms (GNOME's own duration), `EASE_OUT_CUBIC`. Skipped if animations are off or the Overview is open. The top
  bar and dash stay in place, like in a normal workspace change.
* **Keys**: the extension registers its own `enter-shortcut` / `return-shortcut` (own schema, editable in prefs).
  `Main.wm.setCustomKeybindingHandler` on GNOME's `switch-to-workspace-up/down` was considered (no settings
  change, nothing to restore) and rejected: it replaces the handler for *all* accelerators of that action, and it
  would make the shortcuts follow GNOME's settings instead of the extension's.
* **No silent clobbering**: two bindings with the same accelerator are resolved by Mutter in no defined order, so
  the extension removes from `switch-to-workspace-up/down` exactly the accelerators equal to its own. The change is
  recorded in the extension's `backup` setting (JSON, before and removed lists) *before* it is made, and merged
  back on `disable()`, on a shortcut change, and at the next enable if `backup` is not empty (the stale-backup case
  after a crash). Conflicts with any other binding (other `org.gnome.*` keys, schemas of the other enabled
  extensions) are listed in `conflicts` and shown in prefs; they are never edited.
* Left/Right, `move-to-workspace-*` and every other key are never touched.

## 4. Test evidence

Method: `gnome-extension/tests/run_isolated.py` starts one `gnome-shell --headless --wayland --no-x11
--virtual-monitor 1280x720` under `env -i` with private HOME / XDG dirs, `GSETTINGS_BACKEND=keyfile`, a private
session bus and a private bus standing in for the system bus. The shell log shows
`Error registering session with GDM: ... org.gnome.DisplayManager was not provided by any .service files`, i.e.
the real GDM/logind/polkit were not reachable (an earlier unisolated attempt had reached the real system bus; that
is why the private system bus is mandatory). The extension is installed only into the private `XDG_DATA_HOME`.
Key events are injected with a `Clutter.VirtualInputDevice` inside that shell, so they go through Mutter's real
keybinding dispatch of that shell and cannot reach any other session. A stand-in GTK window with the viewer's app
id and a stand-in daemon (JSON-lines, only `workspace.list` / `workspace.enter`) are used; the real viewer and
daemon are not involved. Machine: the workstation (one shell at a time, short runs).

Last run, full suite: **29/29 PASS**. `--coexist` (desktop-cube + Tiling Shell loaded from private copies):
**7/7 PASS**. `--prefs`: **5/5 PASS** plus screenshots.

| check | result |
|---|---|
| private env contains no host display/bus; GNOME Ctrl+Alt+Up/Down removed from `wm.keybindings` while enabled; Left/Right unchanged; backup recorded before the change | PASS |
| Ctrl+Alt+Down (injected key events) moves 0 -> 1 with the daemon absent, shell stays up, no JS error | PASS |
| slide: `window_group.translation_y` samples run 720 -> 0, monotone, 17 frames; return runs -720 -> 0 | PASS |
| Ctrl+Alt+Up returns to workspace 0 | PASS |
| daemon present: calls were `workspace.list` then `workspace.enter`; the stand-in viewer window lands on the agent workspace; origin recorded | PASS |
| Down while already on the agent workspace is a no-op; re-enter reuses the workspace and starts no second viewer | PASS |
| Ctrl+Alt+Right/Left still move 0 -> 1 -> 0; Ctrl+Alt+Up on a personal workspace is passed through | PASS |
| `DisableExtension` restores both keys exactly in the private keyfile; `backup` cleared; re-enable takes them over again | PASS |
| SIGKILL of the shell while overridden leaves keys removed and `backup` set; the restart logs "restored GNOME keybindings (stale backup...)"; a binding added meanwhile (`<Super>Up`) survives; a foreign key on the same accelerator is reported and unchanged; disable afterwards gives `up = ['<Control><Alt>Up', '<Super>Up']`, `down` as original | PASS |
| with desktop-cube + Tiling Shell enabled: enter/return still slide vertically (not the cube), Right still switches; a Tiling Shell key set to Ctrl+Alt+Down is reported as `org.gnome.shell.extensions.tilingshell.move-window-up (not modified)` and left unchanged | PASS |
| changing `enter-shortcut` (the write the prefs window makes) re-binds live: Ctrl+Alt+E enters, GNOME's Ctrl+Alt+Down is restored; setting it back takes it over again | PASS |

Screenshots (from the isolated shell): `gnome-extension/media/shell-enter.png` (agent workspace with the stand-in
viewer, workspace indicator showing three workspaces) and `gnome-extension/media/prefs.png` (preferences window; the
explanatory text is the conflict/replace notice produced by a deliberately added foreign binding).

## 5. Limitations (not tested or not solved)

* **Not run on the live session** (forbidden before approval). Behaviour with the real Tiling Shell layouts,
  ubuntu-dock, blur-my-shell, multi-monitor setups and the real viewer window is untested. The coexist test covered
  desktop-cube and Tiling Shell only, in a single 1280x720 virtual monitor.
* The preferences window was rendered and its setting writes tested, but clicking the shortcut button / reset /
  switch with real pointer or keyboard events was **not** automated: injected pointer clicks did not reach the GTK
  prefs window in the headless shell (hover worked), so capture-by-keypress in that window is verified by reading
  the code and by the setting-change test only.
* The agent workspace is an ordinary GNOME workspace in the horizontal row: with dynamic workspaces the row is
  `[personal..., agent, empty]`, so Ctrl+Alt+Right from the last personal workspace goes through it. Nothing
  stops personal windows being put on it.
* If the original workspace has no windows GNOME removes it while you are away; Return then goes to a
  neighbour. The extension adopts an existing viewer window's workspace only if it is not the one you are on.
* Uses private Shell API (`Main.wm._workspaceAnimation`, `Main.extensionManager`); GNOME 50 only. If those
  disappear the slide is skipped (GNOME's own switch runs) and the log says so.
* The slide moves everything in `window_group` on every monitor (workspaces span monitors here).
* Sticky (all-workspaces) windows appear in both the snapshot and the live content during the 250 ms slide.
* The test-only D-Bus interface (`test-hooks` setting, off by default) exists in the extension code; it is only
  exported when that setting is on.
