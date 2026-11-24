# AgentWorkspaces local API (version 1)

The daemon `agent-workspacesd` (started on demand by the CLI, or `agent-workspaces daemon`) exposes a
versioned JSON API on a user-only unix socket. This is the interface for the CLI, the MCP server, and
for other programs such as Gnotch. It does not depend on GNOME Shell and does not need Gnotch.

* Socket: `$XDG_RUNTIME_DIR/agent-workspaces/api.sock` (directory mode 0700, socket mode 0600). Override the
  base directory with `AW_BASE_DIR` (tests, side-by-side instances).
* Framing: one JSON object per line (`\n` terminated), UTF-8.
* Version: every message carries `"v": 1`. Additive changes keep `v` at 1; breaking changes bump it.
  `hello` returns `api_version`.

## Request / response

```json
{"v":1,"id":7,"method":"workspace.list","params":{}}
{"v":1,"id":7,"ok":true,"result":{"workspaces":[ ... ]}}
{"v":1,"id":7,"ok":false,"error":{"code":"E_NOT_FOUND","message":"no workspace named 'x'"}}
```

`id` is any JSON value; it is echoed back. A workspace is addressed by `workspace` = its **name** or **id**.

### Error codes

| code | meaning |
|---|---|
| `E_NOT_FOUND` | no such workspace / app / approval |
| `E_NOT_RUNNING` | workspace exists but its compositor is not running |
| `E_PAUSED` | agent input rejected because the workspace is in **manual takeover** |
| `E_ASSIGNMENT` | caller identified as agent kind X, workspace is assigned to Y |
| `E_EXISTS`, `E_BAD_NAME`, `E_BAD_SIZE`, `E_BAD_AGENT`, `E_BAD_GPU`, `E_BAD_CLIPBOARD`, `E_BAD_POLICY`, `E_BAD_HOME` | validation |
| `E_BAD_ARGS`, `E_BAD_REQUEST`, `E_UNKNOWN_METHOD` | malformed call |
| `E_START_FAILED`, `E_SESSION`, `E_IO`, `E_INTERNAL` | runtime failures |

## Caller identity (`agent_kind`)

Input/screenshot/launch methods accept an optional `agent_kind`:

* `"human"` (or `"actor":"human"`): the person. Allowed while the workspace is in manual takeover.
* an agent kind (`claude-code`, `codex`, `antigravity`, `hermes`, `other`): the call is rejected with
  `E_ASSIGNMENT` when the workspace is assigned to a *different* kind, and with `E_PAUSED` during takeover.
* omitted: treated as an agent (honours takeover), not assignment-checked.

This is cooperative scoping, not authentication: any process of the same user can connect to the socket.
See `docs/security-model.md`.

## Workspace object (`summary`)

```json
{
  "id": "wabbf62", "name": "research", "state": "running",
  "agent": {"kind": "claude-code", "label": "Claude Code", "icon_hint": "claude"},
  "input_mode": "agent",
  "takeover_reason": "",
  "display": {"width": 1280, "height": 800, "scale": 1.0},
  "gpu": "auto", "xwayland": true, "clipboard": "isolated", "window_policy": "maximized", "home": "private",
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

### Meta
* `hello` -> `{api_version, daemon_version, agent_kinds, uptime, systemd_scopes}`

### Workspaces
* `workspace.list {agent_kind?}` -> `{workspaces:[summary]}` (with `agent_kind`, only workspaces assigned to
  that kind or unassigned).
* `workspace.get {workspace}` (alias `workspace.status`) -> summary + `usage` + `windows` + `pending_approvals`.
* `workspace.create {name, agent_kind?, agent_label?, width?, height?, scale?, gpu?("auto"|"off"),
  clipboard?("isolated"|"to-workspace"|"from-workspace"|"both"), window_policy?("maximized"|"floating"),
  home?("private"|"shared"), xwayland?(default true; false = apps get no DISPLAY), memory_max?, start?}` -> summary
* `workspace.rename {workspace, name}`, `workspace.assign {workspace, agent_kind, agent_label?}`
* `workspace.configure {workspace, width?, height?, scale?, gpu?, clipboard?, window_policy?, memory_max?}`
  (size/scale apply live; gpu/window_policy/memory_max restart the compositor and close its apps)
* `workspace.start|stop|destroy {workspace}`
* `workspace.pause {workspace, reason?}` (alias `workspace.takeover`) / `workspace.resume {workspace}`
* `workspace.usage {workspace}` -> `{memory_bytes, cpu_ns, tasks}` (cgroup accounting of the whole workspace)
* `workspace.enter {workspace, takeover?, display_env?}`: **enter request**. Emits
  `workspace.enter_requested` and opens the viewer window on the user's desktop (using the daemon's
  `WAYLAND_DISPLAY`, or `display_env` passed by the caller). Returns `{launched, pid}`.
* `workspace.launch {workspace, argv:[...], cwd?, env?}` -> `{app, pid, log, argv}`. The program runs with a
  freshly built environment that only references the workspace (see `docs/isolation-model.md`).
* `app.list {workspace}`, `app.kill {workspace, app}`

### Screen
* `screen.screenshot {workspace, region?:[x,y,w,h], cursor?, max_width?, path?, inline?}` ->
  `{width,height,bytes,format:"png",capture_ms,path?,base64?}`. `base64` is returned when `inline` is true
  (default when no `path`).
* `screen.info {workspace}` -> `{outputs:[{name,width,height,refresh_hz,scale,x,y}], pointer:{x,y}, x_display}`

### Windows
* `window.list {workspace}` -> `{windows:[{id,title,app_id,focused,maximized,minimized,fullscreen}]}`
* `window.action {workspace, window, action}`: `focus close maximize unmaximize restore minimize unminimize
  fullscreen unfullscreen`

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
{"v":1,"event":"workspace.input_mode","ts":1795497001.2,"data":{"id":"wabbf62","name":"research","input_mode":"manual","reason":"entered by user"}}
```

| event | data |
|---|---|
| `workspace.created`, `workspace.configured` | summary |
| `workspace.state` | `{id,name,state}` |
| `workspace.renamed` | `{id,old,name}` |
| `workspace.assigned` | `{id,name,agent}` |
| `workspace.destroyed` | `{id,name}` |
| `workspace.input_mode` | `{id,name,input_mode,reason}` |
| `workspace.activity` | `{id,name,actor,action,at}` (actor `agent`/`human`; action `click`, `type`, `screenshot`, ...) |
| `workspace.enter_requested` | `{id,name}` |
| `workspace.warning` | `{id,message}` |
| `input.rejected` | `{id,name,reason:"manual_takeover"}` |
| `viewer.attached`, `viewer.detached` | `{id,name,viewers}` |
| `app.started`, `app.exited` | `{workspace,n,argv?,exit_code?}` |
| `approval.requested`, `approval.resolved` | `{id,name,approval}` |

## Gnotch quick reference

1. Connect to `api.sock`, send `events.subscribe`, then `workspace.list` to render the current state.
2. Show `name`, `agent.icon_hint`, `state`, `input_mode`, `activity.last_action`, `pending_approvals`.
3. "Enter" button: `workspace.enter {workspace}` (add `takeover:true` to pause the agent at the same time).
4. Takeover toggle: `workspace.pause` / `workspace.resume`.
5. Approvals: `approval.list {pending:true}`, `approval.resolve`.

D-Bus is intentionally not offered in v1: a socket needs no bus name ownership, works before the session bus
exists, and is trivial to consume from GJS (`Gio.SocketClient`) and from any language.
