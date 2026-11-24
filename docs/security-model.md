# Security model

**AgentWorkspaces gives graphical isolation, not a security boundary.** It prevents a well-meaning agent from moving your pointer, stealing your focus or typing into your windows. It does not stop a hostile program.

## What is implemented

* Agent-generated input has no route to your session: it is only ever sent to a workspace compositor's own virtual-input protocols (see [isolation-model.md](isolation-model.md)).
* Apps launched in a workspace get a clean environment with no host display, bus or agent-socket variables, and the launcher refuses to start when the workspace socket is missing.
* The API socket is `0600` in a `0700` directory; the daemon runs as you.
* Cooperative scoping: an agent that identifies its kind can only drive workspaces assigned to that kind or unassigned ones (`E_ASSIGNMENT`). While you are in **manual takeover**, agent input is rejected (`E_PAUSED`).
* Agent MCP integrations are opt-in per agent, through each agent's own `mcp add` command, and reversible.

## What is not implemented (do not rely on it)

* **No authentication on the API.** Any process running as your user can connect, say it is the human, and drive any workspace. `agent_kind` is a claim, not a credential.
* **The workspace compositor socket is reachable by any process of your user.** A malicious program can connect to it directly and bypass the daemon's takeover and assignment checks.
* **No filesystem, network, process or syscall confinement.** Apps in a workspace run as you, see your files (unless you use the private `HOME`, which is a convention, not a barrier), your network and `/proc`.
* **Agents can still do harm in their own workspace**: whatever the apps they launch can do. If an agent can run commands, it can also use `xdotool`/`ydotool`-style tools against your real session. The integration instructions tell it not to, but that is a request, not an enforcement.
* **Screenshots may contain secrets** shown in the workspace; they are returned to the calling agent and may be stored by it.
* **The GNOME extension** (optional) runs inside GNOME Shell with Shell privileges; read its source before enabling it.
* Hardening you can add yourself, not provided here: run agents in containers/VMs, use `systemd-run -p` sandboxing properties for the workspace slice (the compositor slice already supports `--memory-max`), separate user accounts.

## Reporting

Open an issue on the repository. Do not include screenshots with private data.
