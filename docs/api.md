# AgentWorkspaces local API (version 2)

The daemon `agent-workspacesd` (started on demand by the CLI, or `agent-workspaces daemon`) exposes a
versioned JSON API on a user-only unix socket. This is the interface for the CLI, the MCP server, and
for other programs such as Gnotch. It does not depend on GNOME Shell and does not need Gnotch.

* Socket: `$XDG_RUNTIME_DIR/agent-workspaces/api.sock` (directory mode 0700, socket mode 0600). Override the
  base directory with `AW_BASE_DIR` (tests, side-by-side instances).
* Framing: one JSON object per line (`\n` terminated), UTF-8.
* Version: every message carries `"v": 2`. Additive changes keep `v`; breaking changes bump it.
  `hello` returns `api_version`. **Version 2 (breaking vs 1): every request except `hello` and `auth.login` must
  be authenticated** (below). A v1 client that sends no credential gets `E_UNAUTHENTICATED`.
  Other changes in 2: events match on workspace ids only; `app.started`/`app.exited` carry `id`;
  `workspace.configure|pause|resume|start|stop|assign|enter`, `approval.resolve` and `viewer.attach` are no longer
  callable with an agent credential; `workspace.list` with an agent credential lists only its own workspace.

## Request / response

```json
{"v":2,"id":7,"method":"workspace.list","params":{},"token":"awt1_..."}
{"v":2,"id":7,"ok":true,"result":{"workspaces":[ ... ]}}
{"v":2,"id":7,"ok":false,"error":{"code":"E_NOT_FOUND","message":"no workspace named 'x'"}}
```

`id` is any JSON value; it is echoed back. A workspace is addressed by `workspace` = its **name** or **id**.

## Authentication

The socket is `0600` in a `0700` directory and the daemon refuses any peer whose kernel-reported uid
(`SO_PEERCRED`) is not its own. On top of that every request carries a credential (a token):

| credential | file | bound to | scopes |
|---|---|---|---|
| workspace credential (default) | `<base>/<workspace id>/token` (0600) | one workspace id | `read screenshot input launch windows clipboard approval` |
| issued / exchanged credential | printed once (`credential.issue`, `auth.exchange`); only its SHA-256 is stored | one workspace id | a subset you choose; optional expiry (1 s .. 24 h) |
| admin credential (the human: CLI, viewer, panel extension) | `<base>/admin.token` (0600) | all workspaces | everything |

* **Sending it.** Either put the token in the `token` field of each request
  (`{"v":2,"id":1,"method":"workspace.list","params":{},"token":"awt1_..."}`), or send `auth.login {token}` once and the
  connection stays authenticated. The token is never logged and never echoed back.
* **Authorisation** happens before anything runs: credential -> workspace -> scope. A workspace credential used on
  any other workspace, or on a reference that does not exist, gets `E_FORBIDDEN` ("this credential is bound to
  workspace X"): it is never redirected to another workspace, a "default" or the first one, and cannot be used to
  find out which other workspaces exist. `workspace` must always be given explicitly.
* **Scopes.** `read` (workspace.get/usage, app.list, events, approvals/grants of its workspace), `screenshot`
  (screen.*), `input` (input.*), `launch` (workspace.launch, app.kill), `windows` (window.list/action), `clipboard`
  (the workspace's own clipboard), `approval` (approval.request, grant.request), `admin` (never in a default
  credential: start/stop/configure/pause/resume/rename of its own workspace).
* **Human-only methods** (admin credential only): `workspace.create|ensure_default|destroy|assign|enter`, `approval.resolve`,
  `viewer.attach`, `credential.*`, `grant.resolve|revoke`, `audit.tail`. An agent cannot resume a takeover,
  approve its own request, change the clipboard policy or grant itself anything. A workspace credential cannot
  claim `agent_kind: "human"` to bypass manual takeover (only the admin credential can act as the human).
* Methods that are not in the policy table (`aw/auth.py: POLICY`) are denied to every non-admin credential. Add a
  new method to that table to make it reachable with an agent credential.
* Failed authentication is slowed down and the connection is closed after 10 failures. Credentials can be rotated
  (`credential.rotate`; the old token stops working at once, also on connections that are already open) and
  revoked.

### Credential lookup order (Python client `aw.client.Client`, used by the CLI, the MCP server and the viewer)

Callers do not handle sockets or tokens: `Client()` finds the credential itself.

1. role **agent** (`Client(role="agent")`, the MCP server): the `token=` argument; `AW_TOKEN`; the file named by
   `AW_TOKEN_FILE`. If any of these is set it is final (nothing else is tried, and the admin credential is never
   used). Otherwise: the token file of the workspace being addressed (`workspace` parameter of the call, else
   `AW_WORKSPACE`) under `AW_BASE_DIR` (default `$XDG_RUNTIME_DIR/agent-workspaces`); otherwise the token files of
   the workspaces assigned to `AW_AGENT_KIND`.
2. role **admin** (the viewer): `token=`; `AW_ADMIN_TOKEN`; the file `AW_ADMIN_TOKEN_FILE`; `<base>/admin.token`.
3. role **auto** (default, the CLI): as agent tiers `token=`/`AW_TOKEN`/`AW_TOKEN_FILE` if one is set (a configured
   agent never silently becomes admin); otherwise as admin; otherwise the agent token files.

Token files that are not owned by you with mode 0600 are refused (`E_INSECURE_CREDENTIAL_FILE`). With no credential
at all the client raises `E_NO_CREDENTIALS` before talking to the daemon. Other languages (GJS, ...): read the
token file and send the `token` field.

Same-user limit: a process of yours can read your 0600 files, so these credentials separate agents that were only
given a workspace token from each other and from the human's powers; they do not stop a hostile process that runs
as you. See `docs/security-model.md`.

### Error codes

| code | meaning |
|---|---|
| `E_NOT_FOUND` | no such workspace / app / approval |
| `E_NOT_RUNNING` | workspace exists but its compositor is not running |
| `E_PAUSED` | agent input rejected because the workspace is in **manual takeover** |
| `E_ASSIGNMENT` | caller identified as agent kind X, workspace is assigned to Y |
| `E_EXISTS`, `E_BAD_NAME`, `E_BAD_SIZE`, `E_BAD_AGENT`, `E_BAD_GPU`, `E_BAD_CLIPBOARD`, `E_BAD_POLICY`, `E_BAD_HOME` | validation |
| `E_BAD_ARGS`, `E_BAD_REQUEST`, `E_UNKNOWN_METHOD` | malformed call |
| `E_UNAUTHENTICATED`, `E_TOKEN_EXPIRED` | no / unknown / revoked / expired credential |
| `E_FORBIDDEN` | the credential is not valid for that workspace, scope or method |
| `E_GRANT_REQUIRED`, `E_NOT_HUMAN_CHANNEL`, `E_BAD_GRANT` | human-only grants (below) |
| `E_ROUTING` | the compositor socket of the workspace failed the identity check; nothing was sent |
| `E_BAD_SCOPE`, `E_BAD_TTL` | credential issue/exchange validation |
| `E_PROFILE_BUSY` | `workspace.launch` with a `--user-data-dir`/`-profile` directory that a running app of another workspace uses |
| `E_NO_SANDBOX`, `E_BAD_SANDBOX` | bubblewrap missing / a requested sandbox mount was refused |
| `E_NO_CREDENTIALS`, `E_INSECURE_CREDENTIAL_FILE`, `E_DISCONNECTED` | client side |
| `E_BROWSER_UNSUPPORTED`, `E_NO_BROWSER`, `E_BAD_BROWSER_ARG`, `E_BAD_URL`, `E_PROFILE_BUSY`, `E_PROFILE_OPEN_ON_HOST`, `E_UNSAFE_PROFILE` | browsers ([browsers.md](browsers.md)) |
| `E_AUDIO_OFF`, `E_BAD_AUDIO`, `E_AUDIO_START`, `E_AUDIO_HOST` | audio ([audio.md](audio.md)) |
| `E_TIMEOUT` | the window placement was not applied in time |
| `E_START_FAILED`, `E_SESSION`, `E_IO`, `E_INTERNAL` | runtime failures |

## Caller identity (`agent_kind`)

With the **admin credential** (CLI) input/screenshot/launch methods accept an optional `agent_kind` claim. With a
workspace credential the claim is ignored: the credential decides.

* `"human"` (or `"actor":"human"`): the person. Allowed while the workspace is in manual takeover.
* an agent kind (`claude-code`, `codex`, `antigravity`, `hermes`, `other`): the call is rejected with
  `E_ASSIGNMENT` when the workspace is assigned to a *different* kind, and with `E_PAUSED` during takeover.
* omitted: treated as an agent (honours takeover), not assignment-checked.

`agent_kind` scoping is cooperative labelling for the human's CLI. Authentication and workspace binding (above) are
what separate agents.

## Workspace object (`summary`)

```json
{
  "id": "wabbf62", "name": "research", "state": "running",
  "agent": {"kind": "claude-code", "label": "Claude Code", "icon_hint": "claude"},
  "input_mode": "agent",
  "takeover_reason": "",
  "display": {"width": 1280, "height": 800, "scale": 1.0},
  "gpu": "auto", "xwayland": true, "audio": "private", "clipboard": "isolated", "window_policy": "maximized", "home": "private",
  "activity": {"last_action": "agent:click", "last_action_at": 1795497000.1, "idle_seconds": 12.4,
               "window_count": 1, "focused_window": "probe ALPHA"},
  "apps": [{"n": 1, "pid": 705125, "argv": ["/usr/bin/python3", "..."], "running": true}],
  "viewers": 0, "created_at": 1795496990.0, "pending_approvals": 0
}
```

* `state`: `starting | running | stopping | stopped | failed`.
* `agent.icon_hint`: `claude | openai | google-antigravity | hermes | robot | none`. A hint only; the
  consumer owns its icons.
* `input_mode`: `agent` or `manual` (human takeover; agent input is rejected).

## Methods

### Meta and authentication
* `hello` -> `{api_version, daemon_version, agent_kinds, uptime, systemd_scopes, auth}` (no credential needed)
* `auth.login {token}` -> `{principal, api_version}`; `auth.whoami` -> `{kind, workspace, scopes, credential, expires_at}`
* `auth.exchange {scopes?, ttl_seconds?, workspace? (admin only)}` -> `{token, credential, workspace, scopes, expires_at}`:
  a short-lived credential (default 15 min, max 24 h, never longer than its parent) for the same workspace with the
  same or fewer scopes and never `admin`. For handing to one MCP session / one sub-agent.
* `credential.list|rotate|revoke|issue {workspace, ...}` (admin): `list` -> credentials without tokens plus the
  token file path; `rotate` replaces the default token (`{admin:true}` rotates the admin token); `revoke {id?, all?}`;
  `issue {scopes?, ttl_seconds?, label?}` returns the token once.
* `audit.tail {n?}` (admin) -> `{events}`: who/what/which workspace/outcome, never parameters or contents.

### Human-only grants (sensitive operations)
Agents may only ask; a person decides through the CLI on a terminal, the viewer or GNOME Shell.
* `grant.request {workspace, kind, resource?, detail?}` (scope `approval`) -> grant `{id, status:"pending"}`.
  Kinds: `browser_profile` (use a copy of a signed-in browser profile; `resource` = profile name),
  `host_clipboard`. Idempotent per (workspace, kind, resource).
* `grant.check {workspace, kind, resource?}` -> `{granted, expires_at}`; `grant.list {workspace?, pending?}`.
* `grant.resolve {id, decision:"granted"|"denied", ttl_seconds?}` and `grant.revoke {id}`: admin credential **and** a
  human channel (peer has an interactive terminal, is the shipped viewer, or is GNOME Shell), else
  `E_NOT_HUMAN_CHANNEL`. A grant lasts 15 minutes by default (max 8 h) and is bound to the workspace and resource.
* Server code that performs a sensitive operation calls `Daemon.grants.require(ws_id, kind, resource)` first, which
  raises `E_GRANT_REQUIRED` and logs the refused attempt. Events: `grant.requested`, `grant.resolved`.

### Workspaces
* `workspace.list {agent_kind?}` -> `{workspaces:[summary]}` (with `agent_kind`, only workspaces assigned to
  that kind or unassigned).
* `workspace.get {workspace}` (alias `workspace.status`) -> summary + `usage` + `windows` + `pending_approvals`.
* `workspace.create {name, agent_kind?, agent_label?, width?, height?, scale?, gpu?("auto"|"off"),
  clipboard?("isolated"|"to-workspace"|"from-workspace"|"both"), window_policy?("maximized"|"floating"),
  home?("private"|"shared"), xwayland?(default true; false = apps get no DISPLAY), audio?("private" (default)|"off"), memory_max?,
  sandbox?(bool), sandbox_mounts?(["/path[:ro|:rw]"]), sandbox_net?(default true), start?}` -> summary + `token_file`
  (the path of the workspace's default credential; the token itself is never returned). With `sandbox` the apps run
  under bubblewrap (docs/isolation-model.md); `E_NO_SANDBOX`, `E_BAD_SANDBOX` on a refused mount.
* `workspace.ensure_default {name?}` (admin credential only, idempotent): make sure the default desktop for people
  exists and runs. `name` defaults to `agents`; the workspace is created with agent kind `none` and started when it
  is missing, started when it is stopped, and returned unchanged when it already runs. Returns the summary plus
  `created` (bool). Concurrent calls are serialised, so two shortcuts pressed together create one workspace. The
  GNOME extension calls it when Ctrl+Alt+Up is pressed with nothing running (docs/gnome-extension.md).
* `workspace.rename {workspace, name}`, `workspace.assign {workspace, agent_kind, agent_label?}`
* `workspace.configure {workspace, width?, height?, scale?, gpu?, clipboard?, window_policy?, memory_max?, audio?}`
  (size/scale apply live; gpu/window_policy/memory_max/audio restart the compositor and close its apps)
* `workspace.start|stop|destroy {workspace}`
* `workspace.pause {workspace, reason?}` (alias `workspace.takeover`) / `workspace.resume {workspace}`
* `workspace.usage {workspace}` -> `{memory_bytes, cpu_ns, tasks}` (cgroup accounting of the whole workspace)
* `workspace.enter {workspace?, takeover?, maximized?, environment?, display_env?}`: **enter request**. Emits
  `workspace.enter_requested` and opens the viewer window on the user's desktop (using the daemon's
  `WAYLAND_DISPLAY`, or `display_env` passed by the caller: `WAYLAND_DISPLAY`, `DISPLAY`, `XDG_RUNTIME_DIR`,
  `DBUS_SESSION_BUS_ADDRESS`; when its `XDG_RUNTIME_DIR` differs from the daemon's, the daemon's session bus address is
  not inherited). Without `workspace` the viewer opens on the workspace you used last, else the most recently active
  (what the GNOME extension does). `maximized` opens the window maximized; `environment` opens it full screen with the header as an overlay (the extension's "environment mode"). A viewer that is already open is reused.
  Returns `{launched, pid}`.
* `workspace.launch {workspace, argv:[...], cwd?, env?}` -> `{app, pid, log, argv}`. The program runs with a
  freshly built environment that only references the workspace (see `docs/isolation-model.md`).
* `app.list {workspace}`, `app.kill {workspace, app}`

### Screen
* `screen.screenshot {workspace, region?:[x,y,w,h], cursor?, max_width?, path?, inline?, quiet?}` ->
  `{width,height,bytes,format:"png",capture_ms,path?,base64?}`. `base64` is returned when `inline` is true
  (default when no `path`). `quiet: true` does not count as workspace activity (viewer thumbnails, Gnotch previews).
* `screen.info {workspace}` -> `{outputs:[{name,width,height,refresh_hz,scale,x,y}], pointer:{x,y}, x_display}`

### Windows
One merged list of the workspace's Wayland toplevels and the X11 windows of its own Xwayland; details,
fields and the geometry contract in [windows.md](windows.md). Scope `windows`.
* `window.list {workspace}` -> `{windows:[{id, kind:"wayland"|"x11", title, app_id, class, instance, pid, app,
  focused, maximized, minimized, fullscreen, geometry:{x,y,width,height}, geometry_source:
  "x11"|"tracked"|"derived"|"bounds"|"unknown", xid? | toplevel?}]}`. `id` is the number every call uses.
* `window.action {workspace, window, action}`: `focus close maximize unmaximize restore minimize unminimize
  fullscreen unfullscreen` -> `{ok, pointer, window}` (the window after the action; `{id, closing:true}` for close)
* `window.move {workspace, window, x, y}`, `window.resize {workspace, window, width, height}`,
  `window.set_geometry {workspace, window, x, y, width, height}` (content rectangle, workspace pixels) ->
  `{ok, pointer, window}`. They honour manual takeover like input, focus the window (Wayland), and take 0.1 s
  (X11) to about 0.7 s (Wayland). `E_BAD_ARGS` for a missing pair or a size below 20; `E_NOT_FOUND`;
  `E_TIMEOUT` if the compositor did not apply the placement configuration.

### Audio
Per-workspace private PipeWire with a null sink; see [audio.md](audio.md).
* `audio.info {workspace}` -> `{mode:"private"|"off", sink, default_sink, daemons, streams:[{id,class,app,
  media_name,state}], pulse_server, route:{to:"none"|"host", alive?}}` (scope `read`)
* `audio.record {workspace, seconds (0.1-120), path?}` -> `{path, seconds, rate, channels, peak, rms, rms_dbfs,
  tone_hz, frames}` (scope `screenshot`; a workspace credential may only write into `<workspace>/shots`).
  `E_AUDIO_OFF` when the workspace has audio off.
* `audio.route {workspace, to:"host"|"none", sink?}` **human only** (admin credential + terminal): play the
  workspace's audio on the host PipeWire. `E_NOT_HUMAN_CHANNEL` otherwise.

### Browsers
Details, profile modes and the authorisation flow in [browsers.md](browsers.md).
* `browser.list {workspace}` -> `{browsers:[{id,label,family,variant,launchable,reason,profiles:[{id,name}]}],
  agent_profiles:[{browser,name}], authorized:[{browser,profile,grant,status,expires_at}]}` (scope `launch`;
  names and ids only, no paths)
* `browser.launch {workspace, browser, profile?, url?, args?, backend?("wayland"|"x11")}` -> `{app, pid, log,
  browser, profile:{mode,name}, backend, url}`. `profile`: `"throwaway"` (default), `"agent:NAME"`,
  `"authorized:ID"` or `{mode, name}`; never a path. Errors: `E_BROWSER_UNSUPPORTED` (snap/flatpak),
  `E_BAD_BROWSER_ARG`, `E_BAD_URL`, `E_BAD_NAME`, `E_PROFILE_BUSY`, `E_GRANT_REQUIRED`, `E_NO_BROWSER`.
* `browser.profile.list {workspace}` (scope `read`), `browser.profile.request {workspace, browser, profile,
  reason?}` (scope `approval`; creates a pending `browser_profile` grant, returns it plus `how`).
* **Human only** (admin credential + terminal, `E_NOT_HUMAN_CHANNEL` otherwise):
  `browser.profile.authorize {workspace, browser, profile, ttl_seconds?, refresh?}` (copies the allow-listed
  session data, creates the grant), `browser.profile.revoke {workspace, browser, profile}` (shreds the copy).
  `E_PROFILE_OPEN_ON_HOST` when the user's own browser has the profile open (nothing is copied or unlocked).

### Input (all honour manual takeover)
* `input.move {workspace,x,y}`, `input.click {workspace,x?,y?,button?,count?}`,
  `input.drag {workspace,x1,y1,x2,y2,button?}`, `input.type {workspace,text}`,
  `input.key {workspace,combo}` (e.g. `ctrl+shift+t`, `Return`, `alt+F4`),
  `input.scroll {workspace,dy,dx?,x?,y?}` (positive `dy` scrolls down)

  Coordinates are logical pixels of the workspace output (top-left origin). Result: `{ok, pointer:{x,y}}`.

### Clipboard (that workspace only)
* `clipboard.get {workspace}` -> `{text}`; `clipboard.set {workspace,text}`

### Approvals (agent-initiated requests for the human)
Agents can ask the human for something through the MCP tool `request_human_input` / CLI
`agent-workspaces approvals request`. AgentWorkspaces does **not** intercept the permission prompts of the agent
CLIs themselves.
* `approval.request {workspace,title,detail?,kind?}` -> approval, `approval.list {workspace?,pending?}`,
  `approval.get {id}`, `approval.resolve {id,decision:"approved"|"denied"}`

### Viewer bookkeeping
* `viewer.attach {workspace}`: counts one viewer for the lifetime of **this connection**; the daemon emits
  `viewer.attached` / `viewer.detached` and updates `viewers`.

## Events

`events.subscribe {workspaces?:[name|id], types?:["workspace.*","approval.*"]}` turns the connection into an
event stream (you may still send requests on it). Each event:

```json
{"v":2,"event":"workspace.input_mode","ts":1795497001.2,"data":{"id":"wabbf62","name":"research","input_mode":"manual","reason":"entered by user"}}
```

| event | data |
|---|---|
| `workspace.created`, `workspace.configured` | summary |
| `workspace.state` | `{id,name,state}` |
| `workspace.renamed` | `{id,old,name}` |
| `workspace.assigned` | `{id,name,agent}` |
| `workspace.destroyed` | `{id,name}` |
| `workspace.input_mode` | `{id,name,input_mode,reason}` |
| `workspace.activity` | `{id,name,actor,action,at,x?,y?}` (actor `agent`/`human`; action `click`, `type`, `screenshot`, ...). `x`,`y`: the pointer position in logical output pixels after a pointer action (`move`, `click`, `drag`, `scroll`), so a viewer can draw where the agent's pointer is without polling |
| `window.changed` | `{id,name}`: a toplevel appeared, changed title or state, or closed (from the compositor's own events, debounced to one per 150 ms). Read the new list with `window.list`. X11-only windows that the compositor does not announce raise no event |
| `workspace.enter_requested` | `{id,name}` |
| `workspace.warning` | `{id,message}` |
| `workspace.audio_route` | `{id,name,to}` |
| `input.rejected` | `{id,name,reason:"manual_takeover"}` |
| `viewer.attached`, `viewer.detached` | `{id,name,viewers}` |
| `app.started`, `app.exited` | `{workspace,n,argv?,exit_code?}` |
| `approval.requested`, `approval.resolved` | `{id,name,approval}` |

## Gnotch quick reference

0. Read `<base>/admin.token` (API v2) and send it as the `token` field of every request (or `auth.login` once).
1. Connect to `api.sock`, send `events.subscribe`, then `workspace.list` to render the current state.
2. Show `name`, `agent.icon_hint`, `state`, `input_mode`, `activity.last_action`, `pending_approvals`.
3. "Enter" button: `workspace.enter {workspace}` (add `takeover:true` to pause the agent at the same time).
4. Takeover toggle: `workspace.pause` / `workspace.resume`.
5. Approvals: `approval.list {pending:true}`, `approval.resolve`.

D-Bus is intentionally not offered in v2: a socket needs no bus name ownership, works before the session bus
exists, and is trivial to consume from GJS (`Gio.SocketClient`) and from any language.
