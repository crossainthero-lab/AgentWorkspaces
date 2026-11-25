# The viewer

`python3 -m aw.viewer [NAME] [--takeover] [--maximized]` (or `agent-workspaces enter NAME`, or Ctrl+Alt+Up with the
GNOME extension) opens one window for all workspaces. A second call reuses the open window. It is a plain GTK4 client
of your desktop; it needs no extension and uses the daemon API with the admin credential (docs/api.md).

![overview](media/viewer-overview.png)

## What is on screen

* **Sidebar**: one row per workspace with its agent (label and icon hint from the API), what the agent is doing
  ("Claude Code · typing" for five seconds after its last input), who has input ("paused", "you are in control") and the
  number of requests waiting for you. The button at the top left collapses it to an icon rail. "New workspace" and, when
  the GNOME extension is present, "Back to desktop" sit at the bottom.
* **Overview** (grid button, top left): every workspace as a live thumbnail, refreshed every 1.5 s only while this view is
  visible. Click one to open it.
* **Workspace view**: the live screen. The header names the workspace and says what happened last, from the daemon's own
  activity record ("Claude Code clicked 3 s ago").
* **Agent | Paused | You**: one setting, who has input. *Paused* rejects the agent's input (`E_PAUSED`) and you only watch.
  *You* rejects it too and forwards your mouse and keyboard; a white frame marks it. *Agent* gives control back and a
  waiting agent continues. Keys and buttons held when you leave *You* or lose focus are released inside the workspace.
* **Environment mode** (`--environment`, what the GNOME extension starts): full screen, no window chrome. The header
  is an overlay that appears when the mouse moves and hides again; the taskbar stays; the workspace list opens on demand.
  Without the flag the viewer is an ordinary window with title bar and sidebar.
* **The agent's pointer** (orange): the agent has full mouse and keyboard control of its workspace, so while it works the
  viewer draws a chunky orange arrow (about 2.7 times an ordinary cursor, solid orange with a dark edge and a white halo so it reads on light and dark content) where *its* pointer is, with a ring when it clicks, and a quiet "Agent typing" note in
  the corner while it types. Positions are the real pointer positions the daemon reports with each pointer action
  (`workspace.activity` `x`,`y`); only the frames in between are interpolated (about 55 ms ease). It fades out after 4 s
  without movement and is hidden while the workspace is Paused or You control it. It is drawn by the viewer on top of the
  picture, so it is **never part of the workspace's pixels** and cannot appear in an agent's screenshot (the viewer also
  captures without the workspace's own cursor). `screen.screenshot` keeps its own `cursor` option for the workspace's
  cursor. Your own pointer is the one on your desktop.
* **The agent's taskbar** (bottom, orange line): the windows inside *this* workspace only, Wayland and its private
  XWayland (the daemon's window service, `window.list`), with the agent's icon hint and name. Your own dock never lists
  them and this bar never lists yours. It follows `window.changed` events (plus a 4 s safety refresh while visible for
  X11-only windows). Clicking an entry focuses that window through the window API **as you** (`agent_kind: "human"`):
  it works in every mode, moves no pointer and does not pause the agent. If the agent is typing into another window it
  will find the focus changed, so choose Paused first when that matters. The agent can focus windows through the same API
  and the bar follows.
* **Request bar** above the workspace: approvals (`approval.request`) with Deny / Approve, and human-only grants
  (`grant.request`: `browser_profile`, `host_clipboard`) with Deny / "Allow for 15 minutes". Grants are accepted by the
  daemon from the viewer because it is a human channel. To show another kind of request, register a renderer:
  `register_approval_view(kind, fn)` in `aw/viewer.py`; `fn(viewer, request, workspace_name)` returns a widget and calls
  `viewer.resolve_approval` or `viewer.resolve_grant`.
* **Clipboard** button: the existing per-workspace clipboard policy (off by default).
* **Menu**: fit to window, fullscreen, start / stop / **close** the workspace. Close asks first: its apps quit and its
  private files are deleted. Closing the *window* is different: it only detaches, every workspace keeps running (a
  workspace left in Paused or You stays paused until someone chooses Agent; the sidebar shows it).
* **New workspace**: name, agent, screen size. The name is validated before the daemon is asked.

Demo: `docs/media/demo-enter-leave-agent-workspace.mp4` (made by `gnome-extension/tests/demo_video.py`; the agent in it is a
scripted AgentWorkspaces client, not a model). Stills: `docs/media/viewer-agent-cursor.png`, `viewer-agent-taskbar.png`.

Remembered between runs (`~/.config/agent-workspaces/viewer.json`): last workspace, sidebar state, fit.

## Cost

* An idle workspace costs the viewer about 0 % CPU (damage-driven screencopy).
* The viewer captures without the cursor, so a moving pointer alone does not cost a repaint; the orange marker is a small widget.
* Frames are capped at 30 per second while you only watch, uncapped while you control (input latency).
* While GNOME marks the window suspended (another GNOME workspace, minimised) the viewer stops capturing and painting
  completely; the workspace is unaffected. Measured numbers: [measurements.md](measurements.md).
* Detaching never touches a workspace: the daemon only counts viewers per connection.

Screenshots of every state, taken in the isolated headless GNOME Shell: `gnome-extension/media/viewer-*.png`
(entered, overview, collapsed, takeover, paused, approval, grant, create, menu, close dialog, stopped).
