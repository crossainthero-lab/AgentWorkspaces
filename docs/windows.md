# Windows: listing and management

`window.list` returns **one list** for a workspace: the Wayland toplevels announced by its compositor and the
X11 windows of **that workspace's own** Xwayland, merged, each with a stable id.

```
agent-workspaces windows NAME
 1 x11     [FM  ] 1280x694+0+26    Xmessage     xmessage
 2 x11     [    ] 500x300+200+150  XEyes        xeyes
 3 wayland [F   ] ?                dev.agentworkspaces.GtkProbe  probe ALPHA
agent-workspaces window-move NAME 3 300 200
agent-workspaces window-resize NAME 3 640 420
agent-workspaces window-geometry NAME 3 300 200 640 420
agent-workspaces window-action NAME 3 minimize        # focus close maximize unmaximize minimize unminimize fullscreen unfullscreen
```

## Fields

`id` (stable for the life of the window while the compositor runs; the same number is used by every call),
`kind` (`wayland` | `x11`), `title`, `app_id` / `class` / `instance` (X11: `WM_CLASS`), `pid` (X11
`_NET_WM_PID`, when the app sets it), `app` (the AgentWorkspaces app number `workspace.launch` returned, when
the pid descends from it), `focused` (exactly one window), `maximized`, `minimized`, `fullscreen`, `geometry`
`{x, y, width, height}` and `geometry_source`:

| `geometry_source` | meaning |
|---|---|
| `x11` | read from the X server: the exact content rectangle in workspace pixels |
| `tracked` | a Wayland window this daemon placed with `window.move/resize/set_geometry`; the rectangle it was told to have (content area, assuming labwc's server-side decoration of 1 px border and 26 px titlebar) |
| `derived` | fullscreen: the whole output |
| `bounds` | a maximized Wayland window: the output rectangle. Whether the window also has a labwc titlebar (server-side decoration) or draws its own (Chrome, libadwaita) cannot be known for a Wayland window, so this is only the bounding box |
| `unknown` | a Wayland window nobody placed and that is not maximized/fullscreen: `x/y/width/height` are `null`. labwc has no way to report it. Place it once (`window.set_geometry`) to make it known |

The geometry of a window that you move with the mouse (takeover) is not tracked for Wayland windows.

## How windows are found

* labwc 0.9.3 announces **some** X11 windows through wlr-foreign-toplevel and not others (measured: `xmessage`
  and Chrome-on-X11 are announced, `xeyes` and `xclock` are not; the cause was not determined). The earlier
  "X11 windows are not announced" statement was therefore too strong, and the old `window.list` missed the
  ones that are not.
* `aw/x11.py` is a small stdlib X11 client. It connects only to `/tmp/.X11-unix/X<n>` for the display recorded for
  that workspace, never to an abstract socket and never to `$DISPLAY` of the daemon, and checks with
  `SO_PEERCRED` that the socket was created by this workspace's compositor process (a listening socket's
  credentials are those of the process that called `listen()`). A display served by anything else is refused
  (unit test with a fake server; real test shows the host's `:0` is rejected).
* The X side is only consulted while the workspace's Xwayland is running (labwc starts it lazily and a connection
  would start it, about 90 MiB).
* An announced X11 window is the same window as the Wayland toplevel labwc announced for it: they are paired on
  title and `app_id == WM_CLASS instance` (labwc's choice), at most one toplevel per X11 window, and reported
  once as `x11`. A native Wayland window with the same title but another app id is not hidden (tested with Chrome
  on Wayland and on X11 at the same time).
* Enumeration uses EWMH (`_NET_CLIENT_LIST`, `_NET_ACTIVE_WINDOW`, `_NET_WM_STATE`, `WM_CLASS`,
  `_NET_WM_PID`, `GetGeometry` + `TranslateCoordinates`).

## How windows are managed (what was investigated, what works)

labwc 0.9.3 has no IPC. Options looked at:

| approach | result |
|---|---|
| wlr-foreign-toplevel requests | focus, close, maximize, minimize, fullscreen: used for Wayland windows. No move/resize request exists in the protocol |
| EWMH client messages to the X server | `_NET_ACTIVE_WINDOW`, `_NET_CLOSE_WINDOW`, `_NET_WM_STATE` (maximize, fullscreen), `WM_CHANGE_STATE` (minimize): used for X11 windows; labwc/wlroots honour them |
| X11 `ConfigureWindow` on another client's window | labwc applies it, but does not update the window's remembered ("natural") geometry, so the next reconfigure, or an unmaximize, snapped the window back. Fixed: after the configure the daemon does one maximize/unmaximize cycle, which makes labwc store the new geometry (tested: it survives a compositor reconfigure and later maximize/fullscreen cycles) |
| labwc `MoveTo` / `ResizeTo` actions | the only way to place a Wayland window. They are bound to a key in the workspace's private `rc.xml`; to carry the coordinates the daemon rewrites that file, sends `SIGHUP` to **that workspace's compositor pid only** (`labwc -r` would signal every labwc of the user), waits for an acknowledgement (a second key runs `touch <ack file>`), focuses the window and presses the key through the workspace's own virtual keyboard. About 0.3 to 0.5 s per placement. The placement focuses the window |
| Alt+drag with the virtual pointer | works for relative moves (labwc's default mouse bindings), but needs the current geometry, which Wayland windows do not give |
| `xdotool`, `wmctrl`, host tools | not used |

Coordinates of `window.move/resize/set_geometry` are the **content rectangle** in workspace (logical) pixels.
`MoveTo` positions the frame, so the daemon subtracts the labwc decoration (1 px border + 26 px titlebar) for
Wayland windows; X11 windows are placed by their content rectangle directly. A Wayland window that draws its own
decoration (client-side) ends up offset by that decoration, because the compositor does not tell us. Move
and resize need both values of a pair (`x`+`y`, `width`+`height`); a maximized or fullscreen window is
un-maximized first.

## What was verified

In `tests/integration/test_windows_audio_real.py` with a real GTK4 window (Wayland) and a real GTK3 window on
XWayland, each with a solid banner colour: after `window.set_geometry`, `window.move`, `window.resize` the
bounding box of the banner colour in the screenshot is the reported rectangle (x and width to the pixel, y to
2 px for the sampling): `x11` and `tracked` agree with the pixels. Also: maximize/unmaximize, fullscreen
(reported 1400x800 output rectangle), minimize/unminimize, focus (exactly one focused window), close for both
kinds; the X11 geometry is restored after those cycles; announced and unannounced X11 windows (`xmessage`,
`xeyes`) are listed once each; the workspace's `activity.window_count` agrees with the list; the host's X
display `:0` is refused by the X client.

## Limits

* Wayland geometry is not readable (see `geometry_source`). Windows are `unknown` until placed or maximized.
* Placing a Wayland window rewrites the workspace's `rc.xml` and reconfigures labwc, which briefly re-applies
  the layout of all windows; X11 windows keep their geometry (tested).
* Client-side-decorated Wayland windows (Chrome, libadwaita) are placed with a decoration offset of
  (1, 27) px.
* Only the first labwc output is considered (workspaces have one).
