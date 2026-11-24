# Compatibility matrix

Only results that were observed are listed. Everything else is marked NOT TESTED or BLOCKED with the reason. Machine: one Ubuntu 26.04 GNOME workstation, 16 cores, NVIDIA RTX 4060 Ti, other test workloads running at the same time. Dates: 2026-11-24. How the runs were done: [testing.md](testing.md#real-agent-acceptance-tests-testsacceptance); how the agents are wired: [integrations.md](integrations.md).

## Summary

| Agent | Model used | Integration method used in the test | Graphical tasks run | Result | Blocker |
|---|---|---|---|---|---|
| Claude Code 2.1.331 | `claude-haiku-5-5` (alias `haiku`, the smallest Claude model) | MCP server through `--mcp-config` + `--strict-mcp-config`, built-in tools removed (`--tools ""`), no instruction text other than what the MCP server itself sends | browser, text editor + save, file manager, screenshot + describe, two apps | **12 of 12 runs completed the task** (see below) | none |
| OpenAI Codex 0.171.0 | `gpt-6-luna` ("fast and affordable model for easier tasks", reasoning effort low) | MCP server through `-c mcp_servers.*` overrides, `--ignore-user-config --ephemeral -s read-only`; a shell tool existed but was never used | the same five | **12 of 12 runs completed the task** | none |
| Google Antigravity (`agy` 1.5.16) | planned `gemini-3.8-flash-low` | none: no per-invocation way to attach an MCP server was found | none | **BLOCKED, not run** | its login is stored in the desktop keyring, which a process without a D-Bus session cannot reach; started that way it printed an OAuth URL and waited. The test was stopped at once (rule: a CLI that wants a login flow is stopped), nothing was opened. Details below |
| Hermes Agent 0.23.1 | none | MCP wiring only (`hermes mcp test`) | none | **NOT TESTED with a model** | the Spark llama.cpp router has no model loaded (checked read-only through `/v1/models`, several times); loading one is not allowed from here |

"Completed the task" means: the agent's final answer matched what its own screenshots showed AND the final workspace screenshot (kept in `docs/evidence/agents/`) shows the requested state. The prompts were ordinary requests that never mention AgentWorkspaces, workspaces, MCP or commands:

| Task | Prompt (exact) |
|---|---|
| browser | "Open my browser and visit agdc.dev, then tell me what the page's main heading says." |
| editor | "Open a text editor, write a short paragraph about autumn rain and save it as autumn.txt." |
| files | "Open the file manager and look inside the folder `<dir>`. Tell me which files are in it." |
| screenshot | "Open the calculator, take a screenshot of it and tell me what you see on its screen." |
| twoapps | "Open the calculator and a text editor. Work out 17 times 23 in the calculator, then type the result into the text editor." |

## Runs (final matrix run; evidence in `docs/evidence/agents/<agent>-<task>/`)

Each row: one real run. "Workspace" is the one the agent created for itself on its first graphical call (`<agent>-<task>`, the project name is the working directory name); no viewer was attached to any of them. Wall time is start of the agent process to exit.

| Agent | Task | Wall time | Turns / tokens | Outcome | What the evidence shows |
|---|---|---|---|---|---|
| Claude Code | browser | 7.3 s | 2 turns, $0.0008 | success | Chrome in the workspace on agdc.dev; answer "One workspace. Every agent." matches the screenshot |
| Claude Code | editor | 15.9 s | 12 turns, $0.0044 | success | `autumn.txt` exists in the workspace home (checked on disk before teardown; the paragraph starts "Autumn rain falls softly on the golden leaves"); title bar shows it saved |
| Claude Code | files | 5.9 s | 2 turns, $0.0008 | success | Files window on the folder; answer listed archive, holiday.jpg, invoice-2024.pdf, notes.txt = the 4 entries |
| Claude Code | screenshot | 7.8 s | 2 turns, $0.0009 | success | Calculator, Basic mode, empty display, as described |
| Claude Code | twoapps | 23.8 s | 22 turns, $0.0051 | success | editor shows `391`; the calculator computed it; the agent first clicked the wrong digits and then clicked into the wrong window and corrected both (see below) |
| Codex | browser | 14.1 s | 78k input tokens (52k cached), 152 output | success | same page and answer |
| Codex | editor | 32.4 s | 294k input (260k cached), 682 output | success with a caveat | document titled `autumn.txt` with the paragraph; the agent passed its own project folder as the app's working directory, so the file went there (the temp folder was removed at teardown, so its existence on disk was **not** checked; the title bar shows the name and folder) |
| Codex | files | 30.7 s | 218k input (189k cached), 485 output | success | Files on the folder, 4 entries named correctly |
| Codex | screenshot | 13.2 s | 79k input, 168 output | success | Calculator, empty display, as described |
| Codex | twoapps | 31.3 s | 219k input (190k cached), 535 output | success | editor shows `391` |
| Claude Code + Codex **at the same time**, each in its own workspace | twoapps | 43.5 s / 28.8 s | 44 turns, $0.0085 / 200k input | both succeeded | the two workspaces were reused from the earlier runs (see "Stable workspace"): each agent saw its own earlier windows and not the other's; in the Claude workspace the agent noticed an existing `391` tab, restored what it had typed into it, and used a new document |

Token counts are what the agent CLI reports. Codex's counts are high because the whole conversation, including each screenshot, is re-sent every turn and cached; the cost to you depends on your plan.

### What went wrong during the runs (not hidden)

* Claude Haiku launched the calculator and the editor together, and the editor (launched last) was on top. In the serial run its first calculator clicks landed on the editor and it first clicked digit buttons wrongly (7 and 2 instead of 1 and 7); it noticed from the screenshots, used `list_windows` + `window_action focus` to bring the calculator forward, cleared and redid it. In the parallel run its first keystrokes (`17*23`) went into the editor (which had focus), it deleted them, switched windows, mis-clicked once more (37x26) and redid it. All corrections are in the transcripts. Both runs ended with the right result.
* In an earlier development run (not retained as evidence) Codex typed `autumn.txt` into a Save dialog field that already contained the extension and produced `autumn.txt.txt`. The final run did not.
* Codex called `list_workspaces` first in some runs; the list showed workspaces of the same agent kind from earlier runs (as designed: an agent sees the workspaces its credentials cover).
* Chrome in the workspace shows its own "Can't update Chrome" bubble. It did not block any task.

## Stable, reusable workspace

The same agent kind in the same project always gets the same workspace: two Claude runs in the same project directory attached to the same workspace id (same `created_at`, windows from the first run still present in the second). A different agent kind in the same project gets a different workspace (`claude-code-<project>` and `hermes-<project>`, tested through the MCP server in `tests/integration/test_mcp_agent_tools_real.py`). Creating the workspace used the human's admin credential only for that call (API v2); every later call used the workspace credential.

## Isolation checks (per run)

* Host display variables (`DISPLAY`, `WAYLAND_DISPLAY`, `WAYLAND_SOCKET`, `XAUTHORITY`, `DBUS_SESSION_BUS_ADDRESS`) were removed from the agent environment, `XDG_RUNTIME_DIR` pointed at a private empty folder and `BROWSER=/bin/true`. The harness read the environment names of **every process in the agent's process group** (agent, MCP server, helpers) several times per run: none of those variables ever appeared (12 of 12 runs, `host_display_variables_seen_in_agent_process_group: []` in each `result.json`).
* The list of the host's Wayland sockets (`/run/user/<uid>/wayland-*`) was identical before and after each run.
* No viewer was attached to any workspace (`workspace_viewers_attached: 0`): the agents worked hidden, and two agents worked simultaneously in two workspaces.
* What this does **not** prove: I cannot observe the real desktop's pointer, focus or windows from an unprivileged process on GNOME Wayland. "No effect on the personal desktop" rests on the structure (no host display reachable by the agent, input only through the workspace compositor's own virtual devices) plus the checks above; nothing on screen was seen to change, but nobody watched it either.

## Resource use (method)

Measured on the same machine, other workloads running, so treat as order of magnitude.

| What | Value | Method |
|---|---|---|
| Workspace memory | 208 to 219 MiB with one GTK app (editor, calculator or file manager); 264 to 278 MiB with two; 737 to 838 MiB with Chrome showing agdc.dev; 564 MiB with editor + Chrome on `about:blank` | `workspace.usage` (cgroup memory of the whole workspace slice, compositor + bus + XWayland + apps) at the end of each run |
| Workspace CPU | 1.6 % of one core, idle, with Chrome and the editor open; 1.2 to 6.0 CPU-seconds in total per run | `workspace.usage` cpu_ns delta over 5 s idle / total at the end of the run |
| GPU | the workspace compositor is listed as a Graphics process by `nvidia-smi pmon` with 2 MiB framebuffer; apps render through it | `nvidia-smi pmon -c 1 -s um` during the measurement script |
| Agent side | Claude Code: 280 to 298 MiB peak PSS (agent + MCP server + children); Codex: 234 to 242 MiB. MCP server alone 13.5 MiB, daemon 15.5 MiB. Agent CPU 1.1 to 1.8 s (Claude), 1.1 to 1.4 s (Codex) per run | peak of the summed `Pss` from `/proc/<pid>/smaps_rollup` of the agent's process group, sampled every 0.5 s; `os.times()` children delta |
| First graphical call | 263 ms to create and start a workspace; 4 ms to re-attach; MCP `initialize` 43 ms | `scripts/measure_agents.py` through the MCP server over stdio, no LLM |
| Launch app -> screenshot returned | 2.1 s (text editor), 1.8 s (Chrome, includes a 0.8 s settle delay; the page may still be loading) | same script |
| Screenshot | 16 ms median round trip through MCP, p95 16.8 ms, 30 samples; 38 KB PNG (1280x720); daemon capture 14.6 ms | same script |
| Mouse click | 141 ms through MCP (one sample) | same script |
| End to end smallest task (calculator screenshot) | Claude Haiku 7.8 s, Codex GPT-6-Luna 13.2 s | wall time of the whole agent process, including its own startup and model latency |

## Per agent notes

### Claude Code
* **Images:** the MCP `image` result was consumed by the model (it described buttons and read page text from them).
* **Instruction channel:** the MCP server's `instructions` field was enough: the runs used no skill, no CLAUDE.md and no `--append-system-prompt`. `integrate claude-code install` additionally writes a skill; that effect was not measured separately.
* **Not tested:** `integrate claude-code install` on the real configuration (only in a throw-away HOME, with `claude mcp add/list/remove`), interactive (non-`-p`) sessions, other Claude models.

### Codex
* **Images:** consumed (it described the calculator's keypad mode and cursor).
* **Instruction channel:** one run (browser task) with **no** instruction text at all, only the MCP server, also used the tools correctly; the others had the rule text as developer instructions (`-c developer_instructions=...`, the equivalent of an installed instruction file).
* **Not tested:** `integrate codex install` on the real configuration, the skill it writes, interactive sessions, other Codex models. User config was ignored during the runs (`--ignore-user-config`), so the user's own plugins/MCP servers were not exercised alongside.

### Antigravity (`agy`): BLOCKED
* Checked: `agy mcp add/list/remove` work in a throw-away HOME (`integrate antigravity install/status/uninstall` round-trip passes). `agy models` lists `gemini-3.8-flash-low` as the cheapest Flash variant.
* **Why no run:** a per-invocation MCP attachment does not exist (`--settings` refuses `mcpServers`), and the CLI authenticates through the desktop keyring over D-Bus. Started with no D-Bus session (as required here) it said it was not logged in, printed an OAuth URL and waited 60 s ("Print mode: triggering interactive OAuth" in its log). The browser was a no-op and no display was available, so nothing opened. I stopped there.
* **To unblock (needs your decision):** either run it with the host session bus but through `xdg-dbus-proxy --filter --talk=org.freedesktop.secrets` (only the secret service reachable; `xdg-dbus-proxy` is installed), or run `integrate antigravity install` on the real configuration and test from a session of yours. Neither was done.

### Hermes Agent: NOT TESTED with a model
* Checked without a model: `hermes mcp add/list/remove` round-trip in a throw-away HOME; `hermes mcp test agent-workspaces` against a throw-away `HERMES_HOME` connected in 454 ms and listed all 25 tools. (The throw-away home needed its own dependency environment, about 440 MB.)
* **Why no run:** the router at the Spark lists 21 models, all `unloaded` (read-only query, repeated). Vision-capable ones exist (`gemma-4-26b-a4b`, `qwen3.6-35b-a3b`, `qwen3.8-27b`, `qwen3.8-27b-agent`, `qwen3-vl-32b-thinking` and others, per their `input_modalities`), but using any of them would make the router load a model, which is a Spark workload I may not start.
* From Hermes' source (read, not run): MCP images are cached and attached as native `image_url` parts when the active route accepts images inside tool results, otherwise the model only gets a file path. With a text-only route `describe_screen` (needs a vision backend, see integrations.md) is the supported way; it was tested only against a stand-in HTTP server.
* **To unblock:** load a vision-capable model on the router (your decision), then `agent-workspaces integrate hermes install` and a run like the ones above.

## NOT TESTED (all agents)

Installing into the real agent configurations and running a live session that way; interactive TUI sessions; non-Latin text; drag-and-drop between apps; two agents of different kinds attached to one workspace; long sessions and stability; the window-move/resize tools (the daemon API does not exist yet, the tools answer `E_UNSUPPORTED`); host clipboard sharing (the tools only ask: `request_permission`); a real vision backend for `describe_screen`; Intel/AMD GPUs; anything on a second machine.
