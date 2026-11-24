# Testing

Tests are split on purpose. Unit tests need no compositor and cannot fail because of the machine. Real integration tests start real compositors and real apps and are labelled as such in their module docstrings.

## Unit tests (49, `tests/unit/`)

```bash
python3 -m unittest discover -s tests/unit -t .
```

Cover: launcher environment (host variables never reach apps, allow-list, fails closed), PNG encoding and pixel-format conversion, keymap/character mapping (including a Linefeed regression found by a real app), the Wayland wire codec against a fake server on a socket pair, the daemon API (validation, takeover `E_PAUSED` for every input method, assignment `E_ASSIGNMENT`, event filters, approvals, framing, adoption of persisted workspaces), the MCP server with a fake daemon (handshake, workspace resolution, no guessing when ambiguous, no shell splitting).

These are logic tests. They prove nothing about a real compositor.

## Real integration tests (`tests/integration/`)

```bash
python3 -m unittest tests.integration.test_workspaces_real tests.integration.test_agent_integrations_real -v
# optional Electron test (needs an Electron binary and a minimal app; not run otherwise):
AW_TEST_ELECTRON_BIN=/path/to/electron AW_TEST_ELECTRON_APP=/path/to/app-dir python3 -m unittest ...ElectronApp
```

Requirements: labwc, python3-gi (GTK4), a systemd user session; GPU tests skip without an NVIDIA driver; Chrome/xeyes tests skip when not installed.

Last full run on the development workstation: 19 tests, all passing (17 workspace tests including Electron, 2 agent-CLI tests), 57 s.

| Test class | What it really does | Maps to validation item |
|---|---|---|
| `Lifecycle` | creates a workspace, runs an app, destroys it: no process, directory or systemd scope remains; resolution 1024x640 to 1600x900, scale 2.0 | display configuration |
| `TwoWorkspacesAreIndependent` | two workspaces at once with distinctly coloured apps: interleaved screenshots always show the right colour; separate pointer positions, typed text only in one app, separate focus, separate clipboards, cross-agent control rejected | concurrent, independent pointer/focus; screenshots from the correct workspace |
| `ManualTakeover` | after `pause` every input method is rejected and the app's event log stays empty; human input still works; `resume` restores agent input | takeover blocks conflicting agent input |
| `LauncherSafeguards` | launched app's environment printed and checked (private runtime dir, no host sockets/vars, private HOME); launch on a stopped workspace spawns nothing; socket audit of every process in the workspace cgroup against the host's wayland/bus/X sockets | host untouched; fail closed |
| `ViewerAndHostStandIn` | the viewer window runs in a second headless compositor that plays "your desktop" with its own probe window. Agent moves/clicks/types while the viewer is open, the viewer is closed through its window, the agent keeps working; the stand-in desktop's pointer, focus and probe event log are unchanged. A second test takes over from the viewer and forwards pointer and keyboard through it into the workspace | agent keeps working after the viewer closes; enter/observe; agent input does not affect the (stand-in) host |
| `XWaylandApp` | xeyes on the workspace's own XWayland: `DISPLAY` is the workspace's, the window exists in that X server, pixels appear in the screenshot | XWayland |
| `ChromiumApp` | Google Chrome (`--ozone-platform=wayland`) renders a page with a known colour, seen in the screenshot | Chromium |
| `ElectronApp` | stock Electron (npm) test app, not any other application | Electron |
| `GpuAcceleration` | `nvidia-smi pmon` must list the workspace compositor and a Vulkan app inside it | GPU |
| `McpEndToEnd` | `agent-workspaces mcp` over stdio: initialize, screenshot (image content), click reaches the app, `E_PAUSED` after takeover | MCP |
| `test_agent_integrations_real` | the real `claude`, `codex`, `agy`, `hermes` CLIs register, list and remove the MCP server in a throw-away HOME (no display/browser variables); real config files unchanged | per-agent adapters |

The test harness (`tests/harness.py`) provides **HostSim**, the stand-in desktop. It exists so that no test opens a window on, or sends input to, your real session.

## Not tested

* Anything on a real GNOME session: the viewer window as a GNOME Wayland client, the GNOME extension on the live shell, host-clipboard bridging by the viewer, viewer latency under a GPU compositor.
* A real read-only probe of the live desktop's pointer is not possible without privileges on GNOME Wayland (no unprivileged pointer-position API; `org.gnome.Shell.Introspect.GetWindows` is denied; `/dev/input` needs the `input` group). The host-untouched claim therefore rests on the structural audit and the stand-in desktop test, not on watching your real pointer.
* Qt, GTK3, Firefox; Intel/AMD GPUs; multi-monitor viewer placement; long-running stability; audio.
* Running Gnotch against the API.
* Second test machine: unreachable when attempted (SSH connection timeout), so no run there.

## Isolated GNOME Shell, real stack

`python3 gnome-extension/tests/run_real.py [--plain] [--media DIR]`: 53 checks with the real daemon, real workspaces, the real
viewer and the developer's extension set in a private headless GNOME Shell; `demo_video.py` records the demo video;
`measure_gs.py` prints the numbers in [measurements.md](measurements.md). Details: [gnome-extension.md](gnome-extension.md).
They use the user's systemd manager only to create workspace scopes (as the integration tests do), nothing on the real
screen, no write to the real dconf (the user's extension settings are only *read* with `dconf dump`).

## GNOME extension tests (earlier, with stand-ins)

`gnome-extension/tests/run_isolated.py`: an isolated headless GNOME Shell with a private session bus and a private stand-in system bus (so it cannot reach the real GDM/logind). Results are in [gnome-extension.md](gnome-extension.md): 29/29 main checks, 7/7 co-existence with copies of desktop-cube and Tiling Shell, 5/5 prefs checks, with stand-in daemon and viewer rather than the real ones.
