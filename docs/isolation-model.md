# Isolation model

"Graphical isolation" here means: an agent can use a graphical environment while you keep using yours, and neither side's pointer, keyboard focus, windows or clipboard leak into the other. It does **not** mean the agent's programs are confined (see [security-model.md](security-model.md)).

## Per-workspace independence

Each row names where the property lives and which test exercises it (real integration tests in `tests/integration/test_workspaces_real.py`, run against real compositors and real apps).

| Property | Where it lives | How it is independent | Test |
|---|---|---|---|
| Pointer position | the workspace's `wl_seat` | agent input goes to `zwlr_virtual_pointer_v1` of that compositor only | `TwoWorkspacesAreIndependent.test_pointer_keyboard_focus_are_independent` (A at 300,300 and B at 700,200 at the same time; B's app only ever saw its own pointer) |
| Keyboard input | virtual keyboard of that compositor | typed text appears only in that workspace's app | same test (text typed in A, B's entry log stays empty) |
| Window focus | that compositor's focus state | each workspace has its own focused window | same test (both report a focused window) |
| Screen capture | screencopy of that compositor's output | the session object is bound to one socket path | `test_screenshots_come_from_the_right_workspace` (distinct banner colours sampled from interleaved captures) |
| App lifecycle | one systemd slice per workspace | `destroy` stops the slice | `Lifecycle.test_create_launch_destroy_leaves_nothing` (no app, no scope left) |
| Clipboard | that compositor's data-control selection | the daemon never touches the host clipboard | `test_clipboards_are_separate` (set in A, B unaffected, UTF-8 safe) |
| Display configuration | that compositor's output | resolution/scale are set with wlr-output-management on that compositor | `Lifecycle.test_resolution_and_scale` |
| Session bus, runtime dir, HOME | private per workspace | see below | `LauncherSafeguards.*` |

## What keeps agent input away from your desktop

1. The only way input is generated is `aw.session.WorkspaceSession`, whose constructor takes an explicit absolute socket path. It does not read `WAYLAND_DISPLAY`, `DISPLAY` or `XDG_RUNTIME_DIR`.
2. The sockets it can be given are the workspace compositors' sockets, which live in `$XDG_RUNTIME_DIR/agent-workspaces/<id>/rt/`.
3. The MCP server and the CLI do not open a display connection at all; they send JSON to the daemon.
4. There is no xdotool, ydotool, uinput, `wtype` or `grim` in any code path.

Verified by: unit tests of the launcher environment (`tests/unit/test_appenv.py`), the real test `test_workspace_processes_hold_no_connection_to_host_sockets` (every unix socket of every process in a workspace's cgroup is checked against the host's Wayland, session-bus and X sockets via `ss -xp`), and `test_enter_view_takeover_close_and_agent_continues`, which runs a second compositor as a stand-in personal desktop with a probe window and asserts that after the agent's moves, clicks and typing the stand-in desktop's pointer, focus and probe event log are unchanged.

## The environment of apps started in a workspace

`appenv.build_env` builds it from scratch (allow-list, not a copy minus a few names):

* `XDG_RUNTIME_DIR` = the workspace's private runtime dir; `WAYLAND_DISPLAY=wayland-0` (relative, so it resolves only inside that dir).
* `DISPLAY` = that workspace's own XWayland, or unset when the workspace has `xwayland` off.
* `DBUS_SESSION_BUS_ADDRESS` = the workspace's own bus, which has **no service directories**, so nothing is D-Bus-activated behind the app.
* `HOME` = a private directory by default (`home=private`), so apps do not read your profiles and cannot hand work to your running browser or editor (Chromium, Firefox and Electron single-instance locks live in the profile). `--home shared` opts out.
* `GSETTINGS_BACKEND=memory`: the app does not read your GNOME settings.
* Dropped: `SSH_AUTH_SOCK`, `XAUTHORITY`, `SESSION_MANAGER`, `XDG_SESSION_*`, `PIPEWIRE_REMOTE`, tokens and every variable not on the allow-list.
* **Fail closed**: if the workspace socket does not exist, no environment is produced and nothing is started (`E_NOT_RUNNING`). It never falls back to the host. Covered by unit tests and `test_stopped_workspace_launch_fails_closed_and_spawns_nothing`.

## Clipboard bridging

Default `isolated`: the workspace clipboard is its own. The viewer (not the daemon) can bridge, controlled by the workspace's `clipboard` policy: `to-workspace` (your clipboard is pasted into the workspace when the viewer window gains focus), `from-workspace` (the workspace's text is copied to your clipboard while the viewer is open), `both`. The daemon itself never reads or writes the host clipboard. Only plain text is bridged. The bridge code is in `aw/viewer.py`; the policy storage and its effect on the daemon API are tested, the host-clipboard side of the viewer bridge is **not** covered by an automated test (the stand-in desktop has no clipboard UI).

## Display configuration

`create --size WxH --scale S` and `configure` use wlr-output-management on the workspace output (headless output, custom mode). Verified for resolution and for scale 2.0 (logical coordinates are halved, screenshots stay in physical pixels).

## Optional sandbox for workspace apps

Graphical isolation (everything above) does not limit what an app can read or do. `create --sandbox` (off by default, per workspace) runs every app launched into that workspace under bubblewrap (`bwrap`, found installed here: 0.11.1; it runs here on Ubuntu 26.04 with `kernel.apparmor_restrict_unprivileged_userns=1`, where the distribution also ships an AppArmor profile named `bwrap-userns-restrict`; other systems were not tried). `--sandbox-mount PATH[:ro|:rw]` (repeatable, read-only by default) adds project directories; `--sandbox-no-network` also unshares the network. `workspace.get` reports `sandbox: {enabled, mounts, network}`. The choice is made at creation and cannot be changed afterwards.

| Inside the sandbox | Value | Measured by |
|---|---|---|
| a file outside the allowed set (a canary in `/tmp`) | not readable | `Containment.test_paths_outside_the_allowed_set_are_not_readable`; the same probe in an unsandboxed workspace *can* read it (control) |
| your home | only the path leading to a mounted project directory exists (an empty skeleton); `~/.ssh` and everything else absent | `test_real_home_shows_nothing_but_the_path_to_a_mounted_project` |
| `HOME`, `XDG_RUNTIME_DIR` | the workspace's own directories; the host runtime directory (host Wayland, session bus, PipeWire, API socket, token files, other workspaces) does not exist | `test_host_runtime_dir_shows_only_the_workspaces_own_path`, `test_environment_inside_is_the_workspaces_own` |
| `/usr`, `/opt`, a short list of `/etc` entries, `/sys` | read-only | `test_write_permissions` (`/usr` and a read-only mount refuse writes; a `:rw` mount, `HOME` and `/tmp` accept them; the `/tmp` write never appears on the host) |
| processes | own PID namespace: a bare probe sees 2 PIDs where the host has about 600 (the test asserts fewer than 12); the daemon's and your other processes are not listed | `test_process_namespace_hides_host_processes` |
| network | **shared with the host by default**, so abstract unix sockets of the host (for example the host X server's) are reachable and the network works; with `--sandbox-no-network` the abstract socket becomes unreachable (measured with a stand-in socket owned by the test) | `test_abstract_unix_sockets_of_the_host_stay_reachable_unless_network_is_unshared` |
| the workspace's X socket | bound in (only that workspace's `/tmp/.X11-unix/X<n>`, never the host's) | xeyes test |

What the sandbox does **not** do: no seccomp filter; GPU devices are passed through; the compositor and the workspace session bus are outside and trusted; `--cap-drop ALL` and `--new-session` only. Mounts that contain the base directory, your runtime directory, `/`, `/etc`, `/run`, `~/.ssh`, `~/.gnupg`, browser profiles, `~/.claude`, `~/.codex` and similar, or your whole home, are refused (`E_BAD_SANDBOX`); a working directory outside the sandbox is refused. A sandboxed workspace needs `home=private`.

Measured overhead (`scripts/measure_sandbox.py`, this workstation, a GTK4 app with Vulkan, 5 runs, medians): process start `1.6 ms` plain vs `9.5 ms` wrapped (about +8 ms per launch, 200 runs each); launch to first frame 313 ms plain vs 273 ms sandboxed (within run-to-run noise of about 50 ms, no measurable cost); memory of the app's systemd scope 74.2 vs 74.8 MiB; processes per app 1 vs 3 (bubblewrap and its init).

Compatibility actually run inside the sandbox: a GTK4 window with input and screenshots (GPU accelerated), an X11 app on the workspace's XWayland (xeyes), Google Chrome. **Chrome only starts with `--no-sandbox`** (its own setuid/user-namespace sandbox cannot start inside bubblewrap here: measured, "No usable sandbox"), so inside this sandbox the isolation Chrome would add itself is replaced by bubblewrap's. Firefox, Qt, Electron: not tried.

## Failures and recovery (tested with real compositors)

* **Compositor killed** (`kill -9` of one workspace's labwc): every route to that workspace (screenshot, click, type, windows, launch, clipboard) answers `E_NOT_RUNNING`, the workspace is recorded `stopped` within a second, its apps, private session bus and systemd scopes are removed, a `workspace.state` event with `reason: compositor_exited` is sent; other workspaces are not touched (same compositor pid, input and screenshots keep working). `workspace.start` brings it back with the same credential. Automatic restart is **not** done.
* **App killed**: the workspace and its other apps are unaffected, `app.exited` is sent, the app can be launched again.
* **Daemon killed (SIGKILL) and restarted**: workspaces, apps and credentials survive and are re-adopted; a compositor that dies after adoption is still noticed (a watcher thread, no `Popen` needed) and cleaned up; a clean daemon stop and start adopts again; `destroy` leaves no process, scope or directory.
* **Client vanishes** mid-request (closed, reset, half-written line, event subscriber, viewer): the daemon stays up, viewer counts return to zero, nothing keeps a lock. The Python client re-sends only read-only calls after a lost connection; a state-changing call raises `E_DISCONNECTED` (outcome unknown) instead of being repeated.
* **Browser profile conflicts**: see the security model; launching an app with a `--user-data-dir`/`-profile` directory that a running app of another workspace uses is refused (`E_PROFILE_BUSY`).

## Honest edges

* Same user, same kernel, same filesystem (unless `--sandbox`), same network. Any process of your user can read `/proc`, the workspace sockets and the API socket and token files.
* The workspace has no audio (no PipeWire/Pulse socket is exposed).
* GPU memory and engines are shared with your desktop; there is no GPU partitioning.
* Window listing uses wlr-foreign-toplevel, which labwc 0.9.3 does not populate for X11 windows (observed): XWayland apps render, take input and appear in screenshots, but not in `window.list`.
