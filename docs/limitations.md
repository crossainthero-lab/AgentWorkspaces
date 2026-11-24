# Limitations (what is not done or not claimed)

* **Not a security sandbox.** See [security-model.md](security-model.md). No API authentication; the workspace socket is reachable by same-user processes; no filesystem/network/syscall confinement.
* **Ubuntu GNOME on Wayland only**, developed on Ubuntu 26.04 / GNOME Shell 50.1. Other distributions and desktops are untested.
* **labwc 0.9.3 specifics**: X11 windows are not announced through wlr-foreign-toplevel, so `windows` and `window-action` do not see XWayland windows (they do render and take input). There is no window move/resize API; windows are maximized by default (`--window-policy floating` lets them keep their own size, and an agent can drag with the mouse).
* **Typing** uses the `us` keymap. Characters not in that layout are pasted through the workspace's own clipboard (needs the focused app to support Ctrl+V); not tested with non-Latin text in real apps.
* **No audio** inside a workspace.
* **The viewer** forwards pointer, buttons, scroll and keys. Not implemented: touch, pointer lock, high-resolution scroll, IME composition, drag and drop between your desktop and the workspace, file transfer. Clipboard bridging is plain text only and the host-clipboard side of the viewer was not tested automatically. The viewer copies each frame through shared memory in Python: about 38 % of one core in a software-rendered stand-in desktop under continuous motion (see measurements). It has not been run on a real GNOME session by me.
* **The GNOME extension** was tested only in an isolated headless GNOME Shell 50 with a private session bus and private stand-in system bus: not on the live session, not with multiple monitors, ubuntu-dock or blur-my-shell. It uses private Shell API and is GNOME 50 only. See [gnome-extension.md](gnome-extension.md).
* **Agent integrations** were verified at the level of registering/removing the MCP server with the real agent CLIs (in a throw-away HOME). No agent was run end to end through them.
* **Electron**: tested with the stock Electron binary from npm (a minimal test app), not with any other application. **Chromium**: tested with Google Chrome with `--ozone-platform=wayland`. **GTK4**: tested. **XWayland**: tested with xeyes. GTK3, Qt and Firefox: not tested.
* **GPU**: verified on NVIDIA (driver 595.99.02) only. Intel/AMD paths, multi-GPU selection (`--render-node` exists but is not exercised) and GPU memory limits are untested.
* **Manual takeover** blocks agents that go through the daemon/MCP/CLI. It cannot stop a process that talks to the workspace socket directly.
* **Daemon restarts** keep workspaces running and re-adopt them (metadata is under `$XDG_RUNTIME_DIR`, so they do not survive logout or reboot). Adoption was exercised in a unit test, not in a real compositor restart test.
* **Gnotch**: the API (docs/api.md) was written to the stated needs; I did not run Gnotch against it.
* **The second (remote) test machine** was unreachable when I tried to run the unit tests there (SSH connection timeout); all test runs were on the workstation.
