# Testing

Tests are split on purpose. Unit tests need no compositor and cannot fail because of the machine. Real integration tests start real compositors and real apps and are labelled as such in their module docstrings.

## Unit tests (176, `tests/unit/`)

```bash
python3 -m unittest discover -s tests/unit -t .
```

Cover: launcher environment (host variables never reach apps, allow-list, fails closed), PNG encoding and pixel-format conversion, keymap/character mapping (including a Linefeed regression found by a real app), the Wayland wire codec against a fake server on a socket pair, the daemon API (validation, takeover `E_PAUSED` for every input method, assignment `E_ASSIGNMENT`, event filters, approvals, framing, adoption of persisted workspaces), the MCP server with a fake daemon (handshake, workspace resolution, no guessing when ambiguous, no shell splitting).

New with API v2 (authentication): `test_auth.py` (credentials, token files and permissions, rotation/revocation/expiry, scopes, workspace binding, foreign and unknown ids, human-only methods, grants, audit log without secrets, credential lookup order, a real socket with login / per-request tokens / event scoping), `test_routing.py` (the routing identity checks against fabricated stale, reused-pid, symlinked and imposter sockets; static audit that no module reads the host display variables or contains an input-injection tool), `test_sandbox.py` (mount validation and the bubblewrap argument list).

`test_policy_coverage.py` fails when an API method that the daemon implements, or that the CLI, viewer, MCP server or GNOME extension calls, has no entry in the authorisation table (`aw/auth.py` plus `aw/policy_ext.py`), or when a table entry has no implementation.

Tests that call `Daemon.handle(method, params, state)` directly must pass `daemon.admin_state()` (the human) or `daemon.workspace_state("name")` (an agent holding that workspace's credential) as `state`; an empty dict is an unauthenticated caller and every method except `hello` fails with `E_UNAUTHENTICATED`.

These are logic tests. They prove nothing about a real compositor.

## Real integration tests (`tests/integration/`)

```bash
python3 -m unittest tests.integration.test_workspaces_real tests.integration.test_agent_integrations_real -v
# optional Electron test (needs an Electron binary and a minimal app; not run otherwise):
AW_TEST_ELECTRON_BIN=/path/to/electron AW_TEST_ELECTRON_APP=/path/to/app-dir python3 -m unittest ...ElectronApp
```

Requirements: labwc, python3-gi (GTK4), a systemd user session; GPU tests skip without an NVIDIA driver; Chrome/xeyes tests skip when not installed.

```bash
python3 -m unittest tests.integration.test_reliability_real tests.integration.test_sandbox_real -v   # 35 tests, about 75 s
```

Last runs of the integrated branch (`feat/integration`) on the development workstation, one module at a time, no host display variables: `test_workspaces_real` 17 tests (1 skipped: Electron binary not provided), `test_reliability_real` 22, `test_sandbox_real` 13, `test_windows_audio_real` 8, `test_browsers_real` 11 (10 run, 1 skipped: Firefox, no non-snap build provided), `test_mcp_agent_tools_real` 3, all passing; `gnome-extension/tests/run_real.py --plain` 50/50, `run_isolated.py` 29/29, `run_cube.py --personal 2` and `--personal 4` 21/21 each (the first `--personal 4` run lost its shell (D-Bus `NoReply`) after 12 checks, cause unknown, log not kept; it passed in 6 of 6 reruns). `test_agent_integrations_real` (real agent CLIs) and the real-model acceptance runs were not repeated on the integrated branch (the acceptance results in [compatibility-matrix.md](compatibility-matrix.md) were measured on `feat/agents-integration`). Each module starts its own daemon under `/run/user/UID/awt-*`, uses at most two compositors at a time and removes everything it started (processes are killed only by exact pid).

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

### Reliability and security tests (`test_reliability_real.py`)

| Test class | What it really does |
|---|---|
| `CompositorCrash` | two workspaces with GTK4 apps; `kill -9` of one compositor's exact pid: screenshot, click, type, windows, launch, clipboard all answer `E_NOT_RUNNING`; state `stopped` within seconds with a `compositor_exited` event; the app and the private bus are gone and no systemd scope is left; the neighbour keeps working with the same compositor pid; `workspace.start` recovers with the same credential and input works again |
| `ApplicationCrash` | `kill -9` of an app's exact pid: `app.exited` event, workspace and screenshots fine, app launchable again |
| `AgentDisconnect` | 4 kinds of vanishing clients (closed mid-typing, reset mid-screenshot, half a request, event subscriber and viewer attach that never say goodbye); the daemon log has no traceback, the viewer count returns to 0, the next input works within 3 s; the client library refuses to repeat a state-changing call after a lost connection and retries read-only ones |
| `DaemonRestart` | SIGKILL of the daemon's exact pid, restart: both workspaces, apps and agent tokens survive; an adopted compositor killed later is noticed and cleaned; clean SIGTERM restart adopts again; destroy leaves no process, scope or directory |
| `ConcurrentAgents` | two threads, two credentials, two workspaces: 25 interleaved move+screenshot rounds each, every screenshot shows only its own workspace's colour, every cross-workspace attempt is `E_FORBIDDEN`, each app saw only its own pointer positions |
| `UnauthorisedAndWrongIds` | raw socket: no/garbage/tampered/empty/foreign/expired/revoked tokens; wrong, empty, foreign and unknown workspace ids; agents cannot call human methods; revocation and rotation hit open connections; throttling closes the connection; socket modes |
| `RoutingFailures` | a stand-in personal desktop (HostSim with a probe window) while the daemon's environment points at it: unknown/stopped/crashed workspaces never reach it; a different process listening at the workspace's socket path gets `E_ROUTING` and receives zero bytes; a symlink to the stand-in's socket is refused; the stand-in's event log, pointer and windows stay unchanged |
| `TakeoverAndRelease` | takeover blocks every agent input route with `E_PAUSED`; claiming to be human, `resume` and `pause` with an agent credential are refused; the human works the workspace; release lets the agent resume |
| `HumanGrants` | agent requests; admin credential without a terminal is refused; the real CLI on a real pseudo-terminal approves (cancels on a wrong word), the grant expires/revokes; the audit log has every step and no secret; CLI credential commands |
| `BrowserProfileConflicts` | real Google Chrome in two workspaces: the same profile directory is refused (`E_PROFILE_BUSY`) and the first workspace's browser receives nothing; separate directories and the default private homes coexist |

### Sandbox tests (`test_sandbox_real.py`)

A probe program runs the same checks in an unsandboxed control workspace and in sandboxed ones (files, sockets, processes, writes, environment, abstract sockets); then a GTK4 app (input + screenshot), an X11 app and Chrome run inside the sandbox. `scripts/measure_sandbox.py` produces the overhead figures in [isolation-model.md](isolation-model.md#optional-sandbox-for-workspace-apps).

The test harness (`tests/harness.py`) provides **HostSim**, the stand-in desktop. It exists so that no test opens a window on, or sends input to, your real session.

## Not tested

* Anything on a real GNOME session: the viewer window as a GNOME Wayland client, the GNOME extension on the live shell, host-clipboard bridging by the viewer, viewer latency under a GPU compositor.
* A real read-only probe of the live desktop's pointer is not possible without privileges on GNOME Wayland (no unprivileged pointer-position API; `org.gnome.Shell.Introspect.GetWindows` is denied; `/dev/input` needs the `input` group). The host-untouched claim therefore rests on the structural audit and the stand-in desktop test, not on watching your real pointer.
* Qt, GTK3, Firefox; Intel/AMD GPUs; multi-monitor viewer placement; long-running stability; audio.
* Running Gnotch against the API.
* A real connection from another user account to the API socket (only the injected-uid unit test and the 0600/0700 modes were checked); other distributions' behaviour of bubblewrap under user-namespace restrictions; Firefox, Qt and Electron inside `--sandbox`; GPU use of Chrome inside `--sandbox`; the viewer, the GNOME extension and the agent MCP registrations against API v2 on a live session (they were run against API v2 only in the isolated headless shell and the integration tests).
* A hostile same-user process (see [security-model.md](security-model.md): not defended against).
* Second test machine: unreachable when attempted (SSH connection timeout), so no run there.

## Isolated GNOME Shell, real stack

`python3 -I gnome-extension/tests/run_real.py [--plain] [--media DIR]`: 53 checks (50 with `--plain`) with the real daemon, real workspaces, the real
viewer and the developer's extension set in a private headless GNOME Shell; `demo_video.py` records the demo video;
`measure_gs.py` prints the numbers in [measurements.md](measurements.md). Details: [gnome-extension.md](gnome-extension.md).
`python3 -I gnome-extension/tests/run_cube.py --personal N` (N = 2 and 4): 25 checks each of the stacked agent desktop with Desktop Cube enabled (slide both ways, overview strip, rapid keys, Left/Right with the cube, focus, bar and dock). `run_silent.py`: 12 checks that nothing is visible without a running workspace and that the service is started and re-found. `run_isolated.py`: 27 checks with stand-ins. All use a private D-Bus; the harness aborts when the address is missing, empty or the real one.

## GNOME extension tests (earlier, with stand-ins)

`gnome-extension/tests/run_isolated.py`: an isolated headless GNOME Shell with a private session bus and a private stand-in system bus (so it cannot reach the real GDM/logind). Results are in [gnome-extension.md](gnome-extension.md): 29/29 main checks, 7/7 co-existence with copies of desktop-cube and Tiling Shell, 5/5 prefs checks, with stand-in daemon and viewer rather than the real ones.

## Windows, audio, browsers and application compatibility

New unit tests (no compositor, no browser, no PipeWire): `tests/unit/test_x11_windows.py` (X11 client against a
fake X server incl. refusal of a display not served by the workspace's compositor; merged window list rules),
`test_browsers.py` (discovery in a fake home, argument/URL policy, host-lock states, allow-list copy that never
modifies the original, shredding, ownership of writable profiles, auth policy of the new methods),
`test_audio_pure.py`, `test_mcp_ext.py`.


New REAL integration tests (real compositors, real apps; a throw-away fake HOME for the daemon and CLI, so no real
browser profile is ever read):

```bash
python3 -m unittest tests.integration.test_windows_audio_real -v     # windows + audio
AW_TEST_FIREFOX=/path/to/non-snap/firefox python3 -m unittest tests.integration.test_browsers_real -v
AW_TEST_FIREFOX=... AW_TEST_ELECTRON_BIN=/path/to/stock/electron AW_COMPAT_REPORT=report.json \
  python3 -m unittest tests.integration.test_compat_real -v          # the matrix in compatibility.md
```

| Test class | What it really does |
|---|---|
| `Windows` | a real GTK4 window (Wayland) and a real GTK3 window on XWayland with solid banner colours: after `window.set_geometry/move/resize` the banner's bounding box in the screenshot equals the reported rectangle; maximize/fullscreen/minimize/focus/close for both kinds; X11 geometry survives compositor reconfigure; announced (`xmessage`) and unannounced (`xeyes`) X11 windows listed once; the host X display is refused |
| `Windows` (stall test) | three real windows in a floating workspace placed 5 times each: no `window.set_geometry` call takes 3 s or more (regression test for a Wayland-connection race that made calls wait 5 s) |
| `Audio` | tone from `pw-cat` and from `ffmpeg -f pulse` captured at the right frequency from the workspace sink; host PipeWire graph identical before/after and no `/dev/snd` fd in the workspace; audio off = no socket; a workspace credential may only record into its own dir; routing to a stand-in "host" PipeWire is refused without a terminal and works through the CLI in a real pty |
| `ChromeProfiles` | throwaway profile inside the workspace dir, native Wayland, no debug flag, no TCP listener, real clicks/typing reach the page, profile deleted with the workspace; agent profile keeps a cookie across restarts and is refused (`E_PROFILE_BUSY`) in a second workspace; forbidden arguments/URLs |
| `AuthorizedProfile` | fixture "host" profile built by real Chrome; agent can only request; `profile authorize` refuses without a terminal, with a wrong confirmation, while the host profile is locked (live and stale lock, lock untouched, original byte-identical); authorised copy has no `Login Data`, is 0700, the session works in the workspace; revoke and workspace destroy remove it; the generic grant flow (request, denial, approval, expiry, revocation, wildcard refusal) with the CLI in real pseudo-terminals |
| `FirefoxProfiles`, `SnapFirefox` | Firefox 160 vendor tarball: agent profile + cookie persistence; the Ubuntu snap is detected and refused |
| `Matrix` | see compatibility.md |

## Real agent acceptance tests (`tests/acceptance/`)

These run the **real agent CLIs with real models** against a real daemon and real headless workspaces. They cost tokens (a few cents per run with the cheapest models) and need the agents to be signed in, so they are not part of the normal test run.

```bash
python3 -m tests.acceptance.run_scenarios claude-code:browser codex:files "claude-code:twoapps+codex:twoapps"
python3 -m tests.acceptance.summarise            # table of the newest run per scenario
python3 tests/acceptance/transcript.py .scratch/runs/<run> claude   # sanitised transcript
```

The Hermes runner reads the provider block named by `AW_HERMES_PROVIDER` (default `spark`) from `~/.hermes/config.yaml` and the key variable named by `AW_HERMES_KEY_ENV`; it never writes to your Hermes configuration.

Tasks (`harness.PROMPTS`) are ordinary requests that do not mention AgentWorkspaces, commands or workspaces. Each agent runs with an environment built from scratch (no `DISPLAY`, `WAYLAND_DISPLAY`, `WAYLAND_SOCKET`, `XAUTHORITY`, `DBUS_SESSION_BUS_ADDRESS`; private empty `XDG_RUNTIME_DIR`; `BROWSER=/bin/true`), its own shell tool removed where the agent allows it, and a time limit; processes are stopped by exact process group. The harness audits the environment names of every process in the agent's group, and compares the host's Wayland sockets before and after. A `+` runs scenarios at the same time in separate workspaces. Results, screenshots and transcripts are in [compatibility-matrix.md](compatibility-matrix.md) and `docs/evidence/agents/`.

Also: `tests/integration/test_mcp_agent_tools_real.py` drives every agent-facing MCP tool (no LLM) against a real compositor and a probe app that logs the events it receives; `scripts/measure_agents.py` measures latency and resource use through the MCP server.
