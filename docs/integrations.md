# Agent integrations

The goal: tell Claude Code, Codex, Antigravity or Hermes "open my browser and visit example.com" or "open a text editor, write a paragraph and save it", and the agent does it **in its own AgentWorkspace**, without you naming AgentWorkspaces, a command or a workspace. Real, model-driven results are in [compatibility-matrix.md](compatibility-matrix.md); this page is how it is wired.

AgentWorkspaces never takes over an agent's existing computer-use tools and never sets `DISPLAY`/`WAYLAND_DISPLAY` globally. Each agent is added on its own, on request, and removed again.

```bash
agent-workspaces integrate claude-code install --dry-run   # print exactly what would change, change nothing
agent-workspaces integrate claude-code install
agent-workspaces integrate claude-code status
agent-workspaces integrate claude-code uninstall

# try it once WITHOUT installing anything (per-invocation flags only, your agent config is not edited):
agent-workspaces integrate claude-code run "open a text editor, write a short paragraph and save it as note.txt"
agent-workspaces integrate codex run "open the calculator and tell me what it shows"
```

## What happens on the first graphical request

1. The agent has the `agent-workspaces` MCP server. Its `initialize` result carries the rule text ([below](#what-the-agent-is-told)) and every tool description says what it is for.
2. The user's plain-language request needs a graphical app, so the model calls `launch_app` (or `list_apps`, `screenshot`, ...).
3. The server resolves **its own workspace**: `<agent kind>-<project>` (for example `codex-myproject`; the project is the directory the agent started the server in, `AW_PROJECT` overrides it). If it exists it is re-attached (and started if stopped), otherwise it is created, assigned to that agent kind, 1280x720, private home. A second session of the same agent in the same project gets the same workspace; another agent kind gets another one.
4. The app starts inside that workspace and the tool returns a screenshot as an MCP image.
5. The model then uses `screenshot`, `mouse_click`, `double_click`, `mouse_drag`, `scroll`, `keyboard_type`, `keyboard_key`, `window_action` and friends. Nothing is sent to your session.

Credentials (API v2, see [api.md](api.md)): all input, screenshot and launch calls use the **workspace credential** of that one workspace. Creating the workspace (and start/stop/resize of it) needs the human's admin credential; the MCP server uses it **only** for those few calls on **its own** workspace, as delegated by installing the integration. `AW_PROVISION=0` turns that off: the workspace must then be created by you (`agent-workspaces create --name codex-myproject --agent codex`) and the agent can only use it. The model can never destroy a workspace, take over or resume, approve its own requests, or touch another workspace.

## Tools

| Tool | Purpose |
|---|---|
| `screenshot` | the screen as an image (+ the same PNG saved under the workspace's `shots/` for agents that read files); `region`, `max_width`, `cursor` |
| `describe_screen` | text description by a separate vision model, for agents whose own model cannot see images. Needs `AW_VISION_URL`/`AW_VISION_MODEL`; says so when it has none |
| `list_apps`, `launch_app` | installed graphical apps from the `.desktop` files (defaults per role: browser, text editor, file manager, calculator); `launch_app app="browser" args=["https://..."]` or `command=[...]`; returns a screenshot once the window appeared |
| `mouse_move`, `mouse_click` (button, count), `double_click`, `mouse_drag`, `scroll` | pointer |
| `keyboard_type`, `keyboard_key` | text and shortcuts (`ctrl+s`, `alt+F4`, `Return`) |
| `list_windows`, `window_action` | focus (to switch between apps), close, maximize, minimize, fullscreen |
| `window_move`, `window_resize` | call `window.move` / `window.resize` of the daemon; until the daemon has them the tool says `E_UNSUPPORTED` and suggests a drag (**not available yet**) |
| `get_clipboard`, `set_clipboard` | the **workspace's own** clipboard only |
| `request_permission` | ask the human for `host_clipboard` or `browser_profile`; the agent only asks, the human grants (see [security-model.md](security-model.md)) |
| `display_info`, `configure_display` | screen size/scale |
| `list_workspaces`, `ensure_workspace`, `workspace_status`, `workspace_control` (start/stop/restart) | workspace management (own workspace only) |
| `request_human_input`, `wait` | ask the user something; let the UI settle |

## What the agent is told

> AgentWorkspaces gives you your OWN graphical computer: a separate headless desktop assigned to you. It is never the user's personal desktop, mouse, keyboard focus or windows. Whenever the user asks for anything that needs a graphical app or a screen (open a browser or a website, open/edit/save in a text editor, use the file manager, take or inspect a screenshot of an app, click or type in a window, switch between apps), do it with these tools. The user does not have to create a workspace, give a command or mention AgentWorkspaces: your workspace is created or re-attached automatically on your first graphical tool call. [...] Never use xdotool, ydotool, wtype, grim, gnome-screenshot, xdg-open or any other computer-use tool or GUI launch against the host session, and never change DISPLAY / WAYLAND_DISPLAY. If a call fails with E_PAUSED the human has taken over the workspace: stop sending input.

The full text is `aw/mcp.py: INSTRUCTIONS` and is printed by `agent-workspaces integrate <agent> show`. This is a request to the model, not an enforcement; see [security-model.md](security-model.md).

## Per agent

| Agent | `install` does | `run` (no install) uses | Verified |
|---|---|---|---|
| Claude Code | `claude mcp add --scope user agent-workspaces -e AW_AGENT_KIND=claude-code -- <server>` and writes `~/.claude/skills/agent-workspaces/SKILL.md` (removed by `uninstall` only if unchanged) | `--mcp-config <file> --strict-mcp-config --tools "" --allowedTools mcp__agent-workspaces --setting-sources ""` | real model-driven runs (matrix); install/status/uninstall in a throw-away HOME |
| OpenAI Codex | `codex mcp add --env ... agent-workspaces -- <server>` and `~/.codex/skills/agent-workspaces/SKILL.md` | `-c mcp_servers.agent-workspaces.*` overrides, `--ignore-user-config --ephemeral -s read-only`, no config file written | real runs; in a run **without** any instruction text the model still used the tools (tool descriptions were enough), so the skill is belt and braces |
| Google Antigravity (`agy`) | `agy mcp add --env ... agent-workspaces -- <server>` | not available: no per-invocation MCP flag was found (`agy --settings` rejects `mcpServers`), and `agy mcp add` writes `~/.gemini/config/mcp_config.json`, which is the user's real config | install/status/uninstall in a throw-away HOME only. **No model-driven run: blocked**, see the matrix |
| Hermes Agent | `hermes mcp add agent-workspaces --command ... --env ... --args ...` (its "enable all tools" prompt is answered yes) | not implemented (needs a throw-away `HERMES_HOME` with its own dependency environment, about 440 MB, and a loaded model) | install/status/uninstall in a throw-away HOME; `hermes mcp test` against a throw-away `HERMES_HOME` connected and listed 25 tools. **No model-driven run: no model is loaded on the Spark router** |

"Throw-away HOME" means `env -i`, no display or browser variables, and the real configuration files were byte-identical afterwards (`tests/integration/test_agent_integrations_real.py`).

`install` edits the agent's real configuration through the agent's own `mcp add` command. It was **not** run on the real configurations during development; the tested command and its rollback are `integrate <agent> install` / `uninstall`.

### Images

Tool results contain an MCP `image` block. Verified consumed by a real model: Claude Haiku 5.5 through Claude Code and GPT-6-Luna through Codex. Hermes converts MCP images to native `image_url` parts only when the active route accepts images in tool results, otherwise it hands the model a file path (read from its source, not run). If an agent or model cannot use images, `describe_screen` sends the screenshot to a vision model you configure and returns text. It has been tested against a stand-in HTTP server only (`tests/unit/test_vision.py`); no real vision backend was available to test it.

## Environment of the MCP server process

`AW_AGENT_KIND` (who is calling), `AW_WORKSPACE` (always use this workspace), `AW_PROJECT` (workspace name suffix), `AW_AUTO_CREATE=0` (never create), `AW_PROVISION=0` (never use the admin credential), `AW_BASE_DIR` (another daemon instance), `AW_APP_DENY` (comma list of apps never offered), `AW_VISION_*` (for `describe_screen`), `AW_SAVE_SHOTS=0`. The server is lazy: it does not start the daemon until the first tool call.

**Credentials (API v2).** The MCP server holds a workspace credential, not the human's: it takes `AW_TOKEN` / `AW_TOKEN_FILE` if set (give a session a short-lived one with `agent-workspaces token issue NAME --ttl 3600`), otherwise the token file of `AW_WORKSPACE`, otherwise the token files of the workspaces assigned to `AW_AGENT_KIND`; with none it reports `E_NO_CREDENTIALS`. It can therefore only see and drive its own workspace(s) (`E_FORBIDDEN` otherwise), cannot resume a takeover, change the clipboard policy or approve anything. The registration commands above do not contain a token (nothing secret goes into the agent's config or argv). Order and details: [api.md](api.md). Asking for permission to use a signed-in browser profile or to share the host clipboard is `grant.request` (not yet an MCP tool); only the human can answer it.

## Other agents

Any MCP client can run `python3 ~/.local/share/agent-workspaces/bin/agent-workspaces mcp` over stdio. Agents without MCP support can use the CLI (`agent-workspaces screenshot NAME`, `click`, `type`, ...) following the same instruction text.
