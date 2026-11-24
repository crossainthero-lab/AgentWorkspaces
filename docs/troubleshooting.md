# Troubleshooting

**`E_NO_DAEMON`** The CLI starts the daemon itself unless `AW_NO_AUTOSTART` is set. Run `agent-workspaces daemon` in a terminal to see errors. Log: `$XDG_RUNTIME_DIR/agent-workspaces/daemon.log`.

**Workspace fails to start (`E_START_FAILED`)** Read `$XDG_RUNTIME_DIR/agent-workspaces/<id>/compositor.log`. Common causes: `labwc` not installed; no `/dev/dri/renderD*` (create with `--gpu off`); socket path longer than 107 bytes (only when `AW_BASE_DIR` is set to a long path).

**Apps start but nothing appears** Check `<id>/app-N.log`. Apps need native Wayland or X11: with `--no-xwayland` X11-only apps cannot start. Chromium-based apps: pass `--ozone-platform=wayland` if auto-detection picks X11.

**A browser/editor opened on my real desktop** It must not, because the workspace has a private HOME and bus. If it happens, the app was launched outside `agent-workspaces launch`, or with `--home shared` and a single-instance app that handed the request to your running instance. Use `--home private` (default).

**`E_PAUSED`** The workspace is in manual takeover (the viewer's "Take over" button or `agent-workspaces pause`). Use `agent-workspaces resume NAME` or untoggle the button.

**`E_ASSIGNMENT`** The calling agent kind differs from the workspace's assigned kind. `agent-workspaces assign NAME <kind>`.

**X11 windows missing in `windows`** Expected with labwc 0.9.3: X11 windows are not announced through wlr-foreign-toplevel. They still render and take input. `agent-workspaces windows` lists Wayland windows only.

**Clicks land slightly off** Coordinates are logical pixels of the workspace output. With `--scale 2` the screenshot is in physical pixels (twice the logical size); divide by the scale.

**Viewer window is blank** The workspace may be stopped (`agent-workspaces list`). The viewer needs `python3-gi` and GTK4.

**Leftover processes** Everything of a workspace lives in the systemd slice `aw-<id>.slice`: `systemctl --user stop aw-<id>.slice`.

**GNOME extension does not capture Ctrl+Alt+Up/Down** See [gnome-extension.md](gnome-extension.md): another extension may own the accelerator; the extension reports conflicts instead of overriding them.

## `E_UNAUTHENTICATED`, `E_NO_CREDENTIALS`, `E_FORBIDDEN`

* `E_NO_CREDENTIALS` (client side): nothing to authenticate with. As the human, run the CLI as yourself (it reads `admin.token` next to the socket). For an agent, set `AW_TOKEN_FILE` to the workspace's token file (`agent-workspaces token path NAME`), or `AW_TOKEN` to a token from `agent-workspaces token issue NAME --ttl 3600`.
* `E_UNAUTHENTICATED`: the token is unknown, was rotated or revoked. Re-read the token file; an API v1 client (no credential) gets this too.
* `E_FORBIDDEN`: the credential is valid but bound to another workspace, lacks the scope, or the method is for the human only. The message names the workspace it is bound to.
* `E_INSECURE_CREDENTIAL_FILE`: a token file is group/world accessible or not yours; `chmod 600` it or rotate (`agent-workspaces token rotate NAME`).

