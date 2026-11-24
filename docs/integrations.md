# Agent integrations

AgentWorkspaces never takes over an agent's existing screenshot or computer-use tools, and never sets `DISPLAY`/`WAYLAND_DISPLAY` globally. Each agent is added on its own, on request, with that agent's official `mcp add` command, and removed again with `mcp remove`.

```bash
agent-workspaces create --name research --agent claude-code      # the workspace the agent will use
agent-workspaces integrate claude-code install --workspace research
agent-workspaces integrate claude-code status
agent-workspaces integrate claude-code uninstall
agent-workspaces integrate codex show        # prints the instruction text and the MCP command, changes nothing
```

`--workspace` is optional; without it the MCP server uses the single workspace assigned to that agent kind and refuses to guess when there are several.

| Agent | Mechanism (official CLI used) | Extra | Tested |
|---|---|---|---|
| Claude Code | `claude mcp add --scope user agent-workspaces -e AW_AGENT_KIND=claude-code -- <server>` | also writes `~/.claude/skills/agent-workspaces/SKILL.md` (removed by `uninstall` only if unchanged) | install, status, uninstall, in a throw-away HOME |
| OpenAI Codex | `codex mcp add --env ... agent-workspaces -- <server>` | `show` prints text for `AGENTS.md` | same |
| Google Antigravity (`agy`) | `agy mcp add --env ... agent-workspaces -- <server>` | `show` prints text for your memory file | same |
| Hermes Agent | `hermes mcp add agent-workspaces --command ... --env ... --args ...` (answers its "enable all tools" prompt) | `show` prints the text | same |

"Tested" means the real CLIs on the development machine accepted the registration and removal and listed the server accordingly, with `env -i`, a throw-away `HOME` and no display variables, and the real configuration files were unchanged (`tests/integration/test_agent_integrations_real.py`). It does **not** mean an agent was run end to end through the MCP server inside its own agent loop; the MCP server itself is tested over stdio with the JSON-RPC handshake in `test_mcp_stdio_drives_assigned_workspace`.

## What the agent is told

The MCP server sends this in its `initialize` result and in tool descriptions, and `integrate <agent> show` prints it:

> Graphical interaction targets your ASSIGNED AgentWorkspace, never the personal desktop. Do not use xdotool, ydotool, wtype, grim or any other computer-use tool against the host session, and do not change DISPLAY / WAYLAND_DISPLAY. Start with list_workspaces. Take a screenshot before clicking. If a tool returns E_PAUSED the user has taken the workspace over: stop sending input and wait.

## Tools

`list_workspaces`, `workspace_status`, `screenshot`, `display_info`, `list_windows`, `window_action`, `launch_app`, `mouse_move`, `mouse_click`, `mouse_drag`, `scroll`, `keyboard_type`, `keyboard_key`, `get_clipboard`, `set_clipboard`, `wait`, `request_human_input`.

`request_human_input` creates an approval request that Gnotch or `agent-workspaces approvals list` can show. AgentWorkspaces does not intercept an agent's own permission prompts.

## Environment of the MCP server process

`AW_AGENT_KIND` (who is calling), `AW_WORKSPACE` (optional default), `AW_BASE_DIR` (optional, to use a different daemon instance). The server is lazy: it does not start the daemon until the first tool call.

## Other agents

Any MCP client can run `python3 ~/.local/share/agent-workspaces/bin/agent-workspaces mcp` over stdio. Agents without MCP support can use the CLI (`agent-workspaces screenshot NAME`, `click`, `type`, ...) following the same instruction text.
