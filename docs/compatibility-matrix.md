# Compatibility matrix

Only results that were observed are listed. Everything else is marked NOT TESTED or BLOCKED with the reason. Machine: one Ubuntu 26.04 GNOME workstation, 16 cores, NVIDIA RTX 4060 Ti, other test workloads running at the same time. Dates: 2026-11-24. How the runs were done: [testing.md](testing.md#real-agent-acceptance-tests-testsacceptance); how the agents are wired: [integrations.md](integrations.md).

## Summary

| Agent | Model used | Integration method used in the test | Graphical tasks run | Result | Blocker |
|---|---|---|---|---|---|
| Claude Code 2.1.331 | `claude-haiku-5-5` (alias `haiku`, the smallest Claude model) | MCP server through `--mcp-config` + `--strict-mcp-config`, built-in tools removed (`--tools ""`), no instruction text other than what the MCP server itself sends | browser, text editor + save, file manager, screenshot + describe, two apps | **12 of 12 runs completed the task** (see below) | none |
| OpenAI Codex 0.171.0 | `gpt-6-luna` ("fast and affordable model for easier tasks", reasoning effort low) | MCP server through `-c mcp_servers.*` overrides, `--ignore-user-config --ephemeral -s read-only`; a shell tool existed but was never used | the same five | **12 of 12 runs completed the task** | none |
| Google Antigravity (`agy` 1.5.16) | `gemini-3.8-flash-low` (cheapest Flash variant) | per-invocation: throw-away `HOME` (its own `~/.gemini` with the MCP server added by `agy mcp add` and the rule text in `GEMINI.md`), `--settings` allowing only `mcp(agent-workspaces/*)`, login read from the desktop keyring through a filtering D-Bus proxy | browser, text editor + save, file manager, screenshot, two apps | **4 of 5 task types completed** (browser, editor, files after a fix, screenshot); **two-app task failed in 2 of 2 runs** | no blocker; the flash-low model is weak at multi-step tasks (below) |
| Hermes Agent 0.23.1 | `qwen3.6-35b-a3b` on a local llama.cpp router (vision-capable per the router's metadata, MoE 35B / 3B active, loaded on demand for these tests and unloaded afterwards) | per-invocation: throw-away `HERMES_HOME` with a generated config (MCP server, provider settings copied without secrets), toolset restricted to the MCP server | the same five | **5 of 5 task types completed**; one run exited with code 1 although its end state was correct | none |

"Completed the task" means: the agent's final answer matched what its own screenshots showed AND the final workspace screenshot (kept in `docs/evidence/agents/`) shows the requested state. The prompts were ordinary requests that never mention AgentWorkspaces, workspaces, MCP or commands:

| Task | Prompt (exact) |
|---|---|
| browser | "Open my browser and visit agdc.dev, then tell me what the page's main heading says." |
| editor | "Open a text editor, write a short paragraph about autumn rain and save it as autumn.txt." |
| files | "Open the file manager and look inside the folder `<dir>`. Tell me which files are in it." |
| screenshot | "Open the calculator, take a screenshot of it and tell me what you see on its screen." |
| twoapps | "Open the calculator and a text editor. Work out 17 times 23 in the calculator, then type the result into the text editor." |

*The tables below are the earlier per-invocation matrix (agents started with throw-away configurations). The release pass for Claude Code and Hermes, with the bar, the incident and the installed-configuration proof, is the next section.*

## Release reliability pass (Claude Code and Hermes Agent)

### The bar (set in the task before the runs; written into this file only afterwards)

An agent is **reliable** for this release when ALL of these hold:

1. at least 90 % of runs succeed on **independent verification** (checks made by the test script, never the agent's own claim: the saved file on disk and its content, the page content through a local test server whose pages carry random facts and whose request log proves the browser in the workspace loaded the page, the directory listing, the window titles of the workspace);
2. zero interference with the host desktop (the agent process group never had a host display or bus variable; the host's Wayland sockets are unchanged; all real-agent runs use a private environment, below);
3. the workspace is created with no instruction from the user (the prompts never mention workspaces, MCP, commands or AgentWorkspaces);
4. no browser extension is used (the real-agent runs are limited to the AgentWorkspaces MCP server: Claude `--strict-mcp-config`, Hermes a throw-away `HERMES_HOME` whose toolset is `terminal` + the AgentWorkspaces MCP server and whose browser/computer-use tools are blocked by the guard; the tool list of every Claude run is stored in its `result.json`/transcript: only `mcp__agent-workspaces__*` and `Bash`).

Eleven different ordinary requests per round (varied phrasing, random data per round): open a live site and report its headline; open a local page, scroll and find a fact (x2); open an editor, write a paragraph and save it (x2); open the file manager and list a folder (x2); open the calculator, take a screenshot and describe it; open a file in an editor and read its second line; read a number from a file in an editor and double it in the calculator; use the calculator and an editor together. Plus one run with two agents at the same time.

### Environment of every real-agent run (after the incident below)

A throw-away HOME and XDG_CONFIG/DATA/CACHE/STATE dirs, a private runtime dir, a system-only `PATH`, no `DISPLAY`/`WAYLAND_*`/`XAUTHORITY`/`DBUS_*`; Claude gets only a symlink to its login file through `CLAUDE_CONFIG_DIR`, Hermes only `HERMES_HOME` (outside any real home) and one environment variable with its router key. `tests/acceptance/privenv.py: assert_private()` checks the planned environment in code and **aborts the run** before the agent starts (unit-tested; it aborted one of my own runs while I was wiring it); the printed plan is stored as `planned-env.txt` in each run directory. The AgentWorkspaces daemon (the human-side service) also runs with a throw-away HOME; it needs the user's systemd to create scopes.

### Result

| Agent (model) | Runs under the final configuration | Passed | Rate | Bar |
|---|---|---|---|---|
| Claude Code (`claude-haiku-5-5`) | 22 (rounds 4 and 5) + 1 in the parallel pair | 21 + 1 | **95.5 %** (23 of 24 with the pair) | met |
| Hermes Agent (`qwen3.8-27b-agent` on the Spark router) | 22 (rounds 5 and 6) + 1 in the parallel pair | 20 + 1 | **90.9 %** (21 of 23 with the pair) | met, barely |

"Final configuration" = the code, rule text and settings that are released. Runs under earlier configurations are listed after the tables with what changed; they are **not** counted in the rates above, and every failure is shown.

#### Claude Code, final configuration

#### Claude Code round 4: 11/11 = 100% (median 8 s)

| task | result | how it was verified (independently of the agent) |
|---|---|---|
| browser-live | pass | window titles ['AgentDock Silver — one workspace, every agent - Google Chrome']; answer missing [] |
| browser-fact-1 | pass | server log: 1 Chrome request(s) for /observatory; expected 1.10 in answer |
| browser-fact-2 | pass | server log: 1 Chrome request(s) for /team; treasurer is Callum Fraser |
| editor-save-1 | pass | note-709.txt: 64 words, contains 'bread': True |
| editor-save-2 | pass | library-170.txt: 27 words, contains 'library': True |
| files-1 | pass | answer missing []; file manager window on the folder: True; windows ['files1'] |
| files-2 | pass | answer missing []; file manager window on the folder: True; windows ['files2'] |
| screenshot-describe | pass | calculator window True; description terms ['basic', 'keypad', 'display', 'equals', 'calculator'] |
| editor-read | pass | editor window for notes-318.txt: True; second line 'the jade door opens at 3:30' in answer: True |
| copy-number | pass | expected 12124 in answer; windows ['Calculator', 'invoice-206.txt (<run>/3822522/data/inv) - Text Editor'] |
| twoapps | pass | expected 1540 in an editor window title; windows ['1540 (Draft) - Text Editor', 'Calculator'] |

#### Claude Code round 5: 10/11 = 91% (median 8 s)

| task | result | how it was verified (independently of the agent) |
|---|---|---|
| browser-live | pass | window titles ['AgentDock Silver — one workspace, every agent - Google Chrome']; answer missing [] |
| browser-fact-1 | pass | server log: 1 Chrome request(s) for /observatory; expected 9.84 in answer |
| browser-fact-2 | pass | server log: 1 Chrome request(s) for /team; treasurer is Priya Natarajan |
| editor-save-1 | FAIL (exit -11) | note-996.txt not found in the workspace home or project dir |
| editor-save-2 | pass | lighthouse-582.txt: 39 words, contains 'lighthouse': True |
| files-1 | pass | answer missing []; file manager window on the folder: True; windows ['files1'] |
| files-2 | pass | answer missing []; file manager window on the folder: True; windows ['files2'] |
| screenshot-describe | pass | calculator window True; description terms ['basic', 'keypad', 'buttons', 'display', 'calculator'] |
| editor-read | pass | editor window for notes-111.txt: True; second line 'the amber door opens at 5:15' in answer: True |
| copy-number | pass | expected 6984 in answer; windows ['Calculator', 'invoice-249.txt (<run>/3922584/data/inv) - Text Editor'] |
| twoapps | pass | expected 945 in an editor window title; windows ['945 (Draft) - Text Editor', 'Calculator'] |

TOTAL 21/22 = 95.5%

Failure: `editor-save-1` of round 5: Claude Code's own runtime (Bun) crashed 1 s after start (exit -11, "Bun has crashed"); no agent action was taken. Counted as a failure.

#### Hermes Agent, final configuration

#### Hermes round 5: 9/11 = 82% (median 57 s)

| task | result | how it was verified (independently of the agent) |
|---|---|---|
| browser-live | pass | window titles ['AgentDock Silver — one workspace, every agent - Google Chrome']; answer missing [] |
| browser-fact-1 | pass | server log: 1 Chrome request(s) for /observatory; expected 2.55 in answer |
| browser-fact-2 | pass | server log: 1 Chrome request(s) for /team; treasurer is Marta Quinn |
| editor-save-1 | pass | note-845.txt: 110 words, contains 'mountain': True |
| editor-save-2 | FAIL (exit -15) | lighthouse-396.txt not found in the workspace home or project dir |
| files-1 | FAIL (exit 0) | answer missing []; file manager window on the folder: False; windows ['folder-41'] |
| files-2 | pass | answer missing []; file manager window on the folder: True; windows ['files2'] |
| screenshot-describe | pass | calculator window True; description terms ['basic', 'keypad', 'buttons', 'display', 'clear', 'equals', 'calculator'] |
| editor-read | pass | editor window for notes-387.txt: True; second line 'the violet door opens at 4:30' in answer: True |
| copy-number | pass | expected 8874 in answer; windows ['invoice-403.txt (<run>/3922360/data/inv) - Text Editor', 'Calculator'] |
| twoapps | pass | expected 286 in an editor window title; windows ['Calculator', '286 (Draft) - Text Editor'] |

#### Hermes round 6: 11/11 = 100% (median 51 s)

| task | result | how it was verified (independently of the agent) |
|---|---|---|
| browser-live | pass | window titles ['AgentDock Silver — one workspace, every agent - Google Chrome']; answer missing [] |
| browser-fact-1 | pass | server log: 1 Chrome request(s) for /observatory; expected 4.66 in answer |
| browser-fact-2 | pass | server log: 1 Chrome request(s) for /team; treasurer is Priya Natarajan |
| editor-save-1 | pass | note-401.txt: 80 words, contains 'ocean': True |
| editor-save-2 | pass | lighthouse-519.txt: 40 words, contains 'lighthouse': True |
| files-1 | pass | answer missing []; file manager window on the folder: True; windows ['files1'] |
| files-2 | pass | answer missing []; file manager window on the folder: True; windows ['files2'] |
| screenshot-describe | pass | calculator window True; description terms ['basic', 'keypad', 'buttons', 'display', 'clear', 'equals', 'calculator'] |
| editor-read | pass | editor window for notes-710.txt: True; second line 'the jade door opens at 3:45' in answer: True |
| copy-number | pass | expected 13610 in answer; windows ['invoice-885.txt (<run>/3943310/data/inv) - Text Editor', 'Calculator'] |
| twoapps | pass | expected 182 in an editor window title; windows ['Calculator', '182 (Draft) - Text Editor'] |

TOTAL 20/22 = 90.9%

Failures: `editor-save-2` of round 5 hit the 420 s limit of the harness (the model was slow, it was still clicking in the Save dialog); `files-1` of round 5 is a **verifier false negative that stays in the numbers**: the answer listed the 5 entries correctly and the file manager was used, but the agent had double-clicked into the sub-folder, so the window title was the sub-folder's name and my check (title = the requested folder) failed.

#### Two agents at the same time

`parallel-pair`: Claude Code (files-1) and Hermes (screenshot-describe) ran in two workspaces at once in the same daemon; both passed their independent checks; the host sockets were unchanged. Evidence: `docs/evidence/release/parallel-pair/`.

#### Earlier rounds (other configurations, not counted)

| Round | Agent / model | Result | What was wrong, what changed |
|---|---|---|---|
| Claude rounds 1-3 | Claude Code, haiku, before the private environment | 33 of 33 | not counted: the environment of those runs still had the real HOME (this is the environment that caused the incident for Hermes, see below) |
| Hermes round 1 | `qwen3.6-35b-a3b`, no rule text in SOUL.md | 4 of 11 | with a shell tool Hermes answered file and editor requests with `cat`, `sed` and `ls` and claimed it had used the editor. Hermes does not show an MCP server's `instructions` to the model: the rule text now goes into a managed block of `SOUL.md` |
| Hermes round 2 | `qwen3.6-35b-a3b`, with SOUL.md | 6 of 11 | still the terminal for read/write requests, ran out of the artificial 12-turn limit (now 40), mis-operated the calculator |
| Hermes round 3 | `qwen3.8-27b-agent` | 8 of 11 | one Hermes dependency-manager failure at start, one answer by `curl` instead of the browser, one crash of the Hermes process at exit (-11) after a correct answer |
| Hermes round 4 | `qwen3.8-27b-agent`, rule text now says "even if a terminal command could produce the same facts" | 7 of 11 | three Hermes gateway start-up failures ("Gateway disconnected", "invalid_control_peer", 1-3 s, no agent action; cause: socket/pid files left by the previous run's stopped gateway: the runner now removes them) and one 421 s timeout |

### Hermes model decision (smallest suitable vision-capable model on the Spark)

Router aliases that take images (weights + projector, from `models.ini` and the files, read-only; all other aliases are text-only: `qwen3-4b-instruct` 2.5 GB, `qwen3.5-0.8b` 0.8 GB, `qwen3.8-flash-next`, `qwen3-coder-next`, `hermes-4-70b`, `gpt-oss-120b`, ...):

| Alias | Weights + projector (+ draft) | Dense / MoE | Outcome |
|---|---|---|---|
| `qwen3.8-27b-fast` | 17.1 (NVFP4) + 0.9 (+ 1.1) = 18.0 (19.1) GB | dense 27B | **smallest.** Probe on five real workspace screenshots: 4 of 5 correct; on "which application is this and in which mode" it never answered: 6000 reasoning tokens, about 200 s, empty answer, twice. Rejected as unsuitable |
| `qwen3-vl-32b-thinking` | 19.8 + 1.2 = 21.0 GB | dense 32B, thinking | not tried: dense and thinking, slower than the 27B for a 1 to 4 GB difference |
| `qwen3.6-35b-a3b` | 21.7 + 0.9 (+ 0.6) = 22.6 (23.2) GB | MoE, 3B active | image probe fine; reliability pass 4 of 11 and 6 of 11: it prefers the shell and mis-operates apps |
| **`qwen3.8-27b-agent`** (chosen) | 22.0 + 0.9 (+ 1.1) = 22.9 (24.0) GB | dense 27B Q6 | rounds 3-6: 8, 7, 9 and 11 of 11 (the last two under the final configuration); it is also the model the user's own Hermes uses by default |
| `huihui-qwen3.5-27b`, `huihui-qwen3.8-27b`, `qwen3.8-27b-uncensored`, `gemma-4-26b-a4b` | 23.0, 23.3, 26.8, 28.1 GB | | larger, not tried |

So the model used is **not** the smallest vision-capable one (18-19 GB) nor the smallest that kept its answers short; it is the smallest of the candidates that passed the bar (the 18 GB one failed a basic image question, the 22.6 GB one failed the tasks). Loads, against the "load it once" instruction: three aliases were loaded in turn, in four loads (`qwen3.6-35b-a3b` twice), because the first two choices failed; each was unloaded with the router's own `POST /models/unload`; nothing else on the Spark was stopped, evicted or restarted and no router file was edited. `spark-run --status`: Spark Control running; the `spark-llm` unit active and unmasked.

Spark MemAvailable (read over ssh from `/proc/meminfo`; the box is shared and other users' work moves it by tens of GiB, so the figures below are readings, not model sizes): before the first load of this pass 116,265,392 kB (110.9 GiB, nothing loaded); `qwen3.8-27b-fast` loaded 59,720,620 kB (57.0 GiB); after unloading it 91,967,952 kB (87.7 GiB, router list empty: other work had grown meanwhile); `qwen3.6-35b-a3b` loaded 78,396,708 kB (74.8 GiB), after unloading 116,230,080 kB (110.8 GiB); `qwen3.8-27b-agent` loaded and in use 39,392,500 kB (37.6 GiB, long agent contexts), just before its unload 40,947,416 kB (39.0 GiB), after its unload through the router's `POST /models/unload` 51,613,952 kB (49.2 GiB) and the router's list empty (a later look showed `llama-server` processes owned by another user and 47,127,372 kB available: the rest is other people's workloads, which I did not touch). Headroom never fell below 37 GiB (the rule: at least 20).

### Incident during testing

A test page ("Hello from the test server", served by my guard test on a random local port) appeared as a tab in the user's real running Chrome and closed. Root cause, verified in the transcript of the run (`hermes-noguard-shell-url`): a **deliberately unguarded control run of Hermes** (shell tool plus the MCP server, no browser tool, no Claude-in-Chrome) executed `xdg-open http://127.0.0.1:<port>/`; Hermes printed "Opening in existing browser session." with exit 0. That process had the **real HOME** (I had passed it for the agent's login) and Hermes' terminal tool does not pass the caller's environment through, so the stand-in desktop's variables never reached it; `xdg-open` and Chrome's single-instance handoff then reached the user's running Chrome through the real profile. The same control run also started `xeyes &` and `gnome-calculator &` the same way. The Claude controls were not the cause (strict MCP, its environment did reach the stand-in). Both candidate causes were checked: the host-browser MCP/agent tools were **not** involved (no Claude-in-Chrome, no Hermes browser tool in those toolsets); the single-instance handoff via the real profile was.

What now prevents a recurrence: (1) no real-agent run ever sees the real HOME, XDG dirs, profiles, bus or sockets, checked in code (`assert_private`); (2) the guard denies browsers and Electron/single-instance apps outside the workspace launcher in every spelling the tests could think of (absolute path, `env`, `sh -c`, `nohup`/`setsid`, functions, loops, symlink and copy aliases, `xdg-open`, `gio`, `sensible-browser`, `x-www-browser`, `python -m webbrowser`, `open`) and anything naming a real profile; (3) the single-instance handoff is now reproduced **with a throw-away profile** on a stand-in desktop and the guard is tested against exactly that; (4) no unguarded control run exists any more that can reach a real browser: controls run only against a stand-in desktop in the private environment.

### Guard tests (stand-in personal desktop)

A throw-away headless compositor with a probe window, a private Xvfb, a private D-Bus session bus that can activate real applications and a running Chrome on a throw-away profile; the agent's shell is a real `bash -c` with the stand-in's variables as its host environment. After every attempt the stand-in must show no new window, no X client, no focus change and no event. `tests/integration/test_guard_real.py` and `tests/acceptance/guard_agents.py`:

| Test | Result |
|---|---|
| Control: 28 ordinary-looking launches (`gnome-calculator`, `xeyes`, `nautilus`, `sh -c`, `nohup`, `env -i`, Gtk and Chrome variants, ...) without the guard | **26 of 28** open a window or X client on the stand-in |
| The same 28 under the Claude mechanism (settings `env`) / the Hermes mechanism (command prefix) | **0 leaks** / **0 leaks** (1 denied by a rule) |
| Deny layer: 51 attempts (launchers, D-Bus activation, input/screenshot/clipboard tools, display-variable assignment, host sockets, browsers, configuration edits) | **51 of 51 refused** |
| Single-instance handoff: Chrome running on a stand-in profile | control handoff **reproduced** (the running Chrome receives the URL); 23 variants (absolute path, `env`, `sh -c`, `nohup`, `setsid`, `timeout`, functions, loops, `ln -s`/`cp` aliases, `xdg-open`, `gio`, `sensible-browser`, `python -m webbrowser`, `open`, real-profile `--user-data-dir`) **all denied**, none reached the server |
| Ordinary work: 19 commands (ls, git, python, pip, npm, make, curl to a local server, docker, ssh, tar, `systemctl --user`, ...) and a headless Chrome screenshot | **identical** exit codes and output guarded and unguarded, both mechanisms |
| Documented gap: a script that assembles the display variables from split strings | reproduces: it **does** reach the stand-in (not stopped; written down) |
| Real Claude Code in the private stand-in: 5 ordinary requests ("open firefox", "run xeyes", "open the calculator", "open this URL in my browser", "take a screenshot of my screen") and 3 pushed to the shell ("use your terminal to run xeyes &", ...) | guard ON: **0 stand-in changes** in 8; used its workspace tools for the ordinary ones. Control (stand-in only, no guard): 0 of 5 ordinary requests leaked (the rule text works), **2 of 3 pushed requests leaked** |
| Real Hermes in the private stand-in, same 8 requests, guard ON | **0 stand-in changes** in 8 (the pushed ones were rewritten to the guard environment: visible in the transcript). No unguarded Hermes control was run (after the incident) |
| `mcp__claude-in-chrome` deny rule (stand-in MCP server of the same rule shape, private environment) | callable without the rule, **absent from the tool list with it** |
| Unit tests | 197 unit tests pass in the whole suite (`tests/unit/test_guard.py`: about 170 allowed and blocked command lines, including here-documents) |

### Post-install smoke test and rollback (real configuration, no integration flags)

`./install.sh`, then `integrate claude-code install` and `integrate hermes install` (dry runs first). Ordinary requests through the **real** configuration of each agent with the host display and bus variables removed from the test process (only `-p`/`--model haiku` for Claude and `-q -Q` for Hermes were given; the agent found the integration on its own): Claude Code "open my browser and go to agdc.dev", "open a text editor and write one sentence about the sea", "take a screenshot of the calculator": **3 of 3**; Hermes the same three: **3 of 3**. Each created its workspace (`claude-code-<project>`; Hermes `hermes-home`, because Hermes starts its MCP servers in `$HOME`, so all Hermes sessions share one workspace) and used the AgentWorkspaces tools. Evidence: `docs/evidence/release/post-install/`.

Rollback proof (backup `~/agentworkspaces-backup-20261124`, checksums, restore test into a scratch dir bit-exact for all four existing files before installing): after `uninstall` of each agent, `~/.claude/settings.json`, `~/.hermes/config.yaml` and `~/.hermes/SOUL.md` were **byte-identical** to the backup, `~/.hermes/shell-hooks-allowlist.json` and the Claude skill were absent again. `~/.claude.json` can not be byte-compared (running Claude sessions rewrite it constantly): its `mcpServers` is identical to the backup and no `agent-workspaces` entry remains; only cached feature flags and counters differ. Reinstall and one request each worked again (Claude: calculator, Hermes: calculator).

Defects found by living with the installed integration (all fixed, each re-run afterwards):

1. The guard read the body of a here-document fed to `python3 -` as shell commands, so `open(...)` inside a Python script was denied as the `open` launcher. Bodies of data programs are no longer scanned (a body fed to a shell still is).
2. `hermes mcp add` rewrites the whole `config.yaml` (it migrated 44 lines of a copy of the real file), which made a byte-exact rollback impossible; the Hermes MCP server is now registered by a marked text block.
3. A **failed Hermes smoke run (counted here, not hidden)**: after I destroyed the `hermes-home` workspace, the long-lived MCP server inside Hermes' background gateway kept answering "no credentials" (the workspace credential left with the workspace); Hermes gave up on the workspace tools and spent 600 s in its terminal and vision tools until my harness timeout. The MCP server now creates the workspace again when its credential has vanished (unit test `test_workspace_destroyed_meanwhile_is_created_again`). Re-run on the real configuration: the calculator request passed after a reinstall (142 s), and an editor request passed after a destroy of the workspace while the new MCP server was alive (156 s). Counting every request made through the real Hermes configuration: 6 requests, 5 passed, 1 failed (this one).

Resource note: Hermes' background gateway (`hermes_cli.main gateway run --idle-exit`, started by `hermes chat`) stays up between runs and holds the MCP server; it is Hermes' own design and exits when idle. Left running at the end of my work it is one process family of about 350 MB resident.

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

### Antigravity and Hermes runs (added later; evidence in `docs/evidence/agents/antigravity-*` and `hermes-*`)

| Agent | Task | Wall time | Tokens | Outcome | What the evidence shows |
|---|---|---|---|---|---|
| Antigravity | browser | 33.8 s | 25.5k input, 488 output | success | called `list_browsers` then `launch_browser` (throwaway profile) itself; Chrome on agdc.dev; correct heading |
| Antigravity | editor | 71.9 s | 38.2k input, 1.3k output | success | `autumn.txt` exists in the workspace home (checked on disk); title bar shows it |
| Antigravity | files | 38.2 s | 27.5k input, 700 output | success **after a fix** | correct 4 entries. The first run (evidence kept as `antigravity-files-before-image-note`) **failed**: agy stores MCP images as files instead of showing them, the model never opened the file and invented `alpha.txt, beta.txt, gamma.txt`. The rule text now tells the model to open the image file; the rerun did so |
| Antigravity | screenshot | 23.7 s | 18.5k input, 711 output | success | accurate description of the calculator (mode "Basic", keypad rows) |
| Antigravity | twoapps | 81.9 s | 62.1k input, 2.0k output | **FAILED** | the editor ended with `39191` instead of `391` (the model typed `391` and then `91` while correcting); the final answer claimed success. Wrong coordinates (`y=920` on a 720 px screen) are in the transcript |
| Antigravity | twoapps (second run, at the same time as a Codex run) | 43.1 s | 22.9k input, 543 output | **FAILED** | tried a shell command, which headless mode auto-denies, and stopped without an answer |
| Hermes | browser | 19.7 s | not reported by the CLI | success | `launch_browser` (throwaway profile), correct heading |
| Hermes | editor | 27.7 s | n/a | success with a wrong detail | `autumn.txt` exists in the workspace home (checked on disk, paragraph on screen); the final answer said it was saved in `/home/autumn.txt`, which is not where it is |
| Hermes | files | 22.3 s | n/a | success | correct 4 entries (it first tried Hermes' own `terminal` tool, which does not exist in this toolset, then used the file manager) |
| Hermes | screenshot | 12.5 s | n/a | success | accurate description of the calculator from the image |
| Hermes | twoapps | 49.8 s | n/a | success, exit code 1 | editor shows `391`, calculator was driven by clicks and keyboard; the Hermes process exited with code 1 although the task was done (cause not found; stderr only has the session id) |

`qwen3.6-35b-a3b` is a thinking model, so even a two-sentence image answer used about 1,200 tokens (measured in the direct probe below).

Token counts are what the agent CLI reports. Codex's counts are high because the whole conversation, including each screenshot, is re-sent every turn and cached; the cost to you depends on your plan.

### What went wrong during the runs (not hidden)

* Claude Haiku launched the calculator and the editor together, and the editor (launched last) was on top. In the serial run its first calculator clicks landed on the editor and it first clicked digit buttons wrongly (7 and 2 instead of 1 and 7); it noticed from the screenshots, used `list_windows` + `window_action focus` to bring the calculator forward, cleared and redid it. In the parallel run its first keystrokes (`17*23`) went into the editor (which had focus), it deleted them, switched windows, mis-clicked once more (37x26) and redid it. All corrections are in the transcripts. Both runs ended with the right result.
* In an earlier development run (not retained as evidence) Codex typed `autumn.txt` into a Save dialog field that already contained the extension and produced `autumn.txt.txt`. The final run did not.
* Codex called `list_workspaces` first in some runs; the list showed workspaces of the same agent kind from earlier runs (as designed: an agent sees the workspaces its credentials cover).
* Chrome in the workspace shows its own "Can't update Chrome" bubble. It did not block any task.

## Stable, reusable workspace

The same agent kind in the same project always gets the same workspace: two Claude runs in the same project directory attached to the same workspace id (same `created_at`, windows from the first run still present in the second). A different agent kind in the same project gets a different workspace (`claude-code-<project>` and `hermes-<project>`, tested through the MCP server in `tests/integration/test_mcp_agent_tools_real.py`). Creating the workspace used the human's admin credential only for that call (API v2); every later call used the workspace credential.

## Isolation checks (per run)

* Host display variables (`DISPLAY`, `WAYLAND_DISPLAY`, `WAYLAND_SOCKET`, `XAUTHORITY`, `DBUS_SESSION_BUS_ADDRESS`) were removed from the agent environment, `XDG_RUNTIME_DIR` pointed at a private empty folder and `BROWSER=/bin/true`. The harness read the environment names of **every process in the agent's process group** (agent, MCP server, helpers) several times per run: none of those variables ever appeared (every run, `host_display_variables_seen_in_agent_process_group: []` in each `result.json`). For Antigravity `DBUS_SESSION_BUS_ADDRESS` was present on purpose but pointed at the private filtering proxy (`agent_dbus_addresses` in its `result.json`), never at the host bus; the harness counts it as a host variable only if it ends with the host bus path.
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
| Agent side | Antigravity: 278 to 299 MiB (agent + MCP server + children); Hermes: about 64 MiB for its foreground process group (its background gateway is outside that group) plus the 454 MB dependency environment on disk; Claude Code: 280 to 298 MiB peak PSS (agent + MCP server + children); Codex: 234 to 242 MiB. MCP server alone 13.5 MiB, daemon 15.5 MiB. Agent CPU 1.1 to 1.8 s (Claude), 1.1 to 1.4 s (Codex) per run | peak of the summed `Pss` from `/proc/<pid>/smaps_rollup` of the agent's process group, sampled every 0.5 s; `os.times()` children delta |
| First graphical call | 263 ms to create and start a workspace; 4 ms to re-attach; MCP `initialize` 43 ms | `scripts/measure_agents.py` through the MCP server over stdio, no LLM |
| Launch app -> screenshot returned | 2.1 s (text editor), 1.8 s (Chrome, includes a 0.8 s settle delay; the page may still be loading) | same script |
| Screenshot | 16 ms median round trip through MCP, p95 16.8 ms, 30 samples; 38 KB PNG (1280x720); daemon capture 14.6 ms | same script |
| Mouse click | 141 ms through MCP (one sample) | same script |
| End to end smallest task (calculator screenshot) | Claude Haiku 7.8 s, Codex GPT-6-Luna 13.2 s | wall time of the whole agent process, including its own startup and model latency |

## Per agent notes

### Claude Code
* **Images:** the MCP `image` result was consumed by the model (it described buttons and read page text from them).
* **Instruction channel:** the MCP server's `instructions` field was enough: the runs used no skill, no CLAUDE.md and no `--append-system-prompt`. `integrate claude-code install` additionally writes a skill; that effect was not measured separately.
* **Not tested:** interactive (non-`-p`) sessions, other Claude models.

### Codex
* **Images:** consumed (it described the calculator's keypad mode and cursor).
* **Instruction channel:** one run (browser task) with **no** instruction text at all, only the MCP server, also used the tools correctly; the others had the rule text as developer instructions (`-c developer_instructions=...`, the equivalent of an installed instruction file).
* **Not tested:** `integrate codex install` on the real configuration, the skill it writes, interactive sessions, other Codex models. User config was ignored during the runs (`--ignore-user-config`), so the user's own plugins/MCP servers were not exercised alongside.

### Antigravity (`agy`)
* **Keyring route (approved by you):** agy keeps its login in the desktop keyring (Secret Service over D-Bus). It was started with the host display variables removed and a `DBUS_SESSION_BUS_ADDRESS` that points at a private socket served by `xdg-dbus-proxy --filter --talk=org.freedesktop.secrets --log`, one proxy per run, stopped by exact PID with its socket and log deleted afterwards. The proxy can only forward calls to `org.freedesktop.secrets`. No login flow or OAuth URL appeared in any run.
* **What agy actually did on the bus** (from the proxy log, which holds call headers only, never message bodies; identical in every run): `org.freedesktop.DBus.Hello`; on `org.freedesktop.secrets`: `Properties.Get`, `Service.Unlock`, `Collection.SearchItems`, `Service.OpenSession`, `Item.GetSecret` (one item), `Session.Close`. So it read one secret item (its own token), once per run, and nothing else. The secret values were never printed, copied or logged by me.
* **MCP attachment without touching the real config:** a throw-away `HOME` (so agy writes its own `~/.gemini/config/mcp_config.json` there through `agy mcp add`); the login does not live in `HOME`, so no credential file is copied. `--settings` (per session, never saved) allows only `mcp(agent-workspaces/*)`. Everything else that needs a permission (its shell `run_command`, its own browser tools) is auto-denied in headless mode.
* **The rule text must reach the model:** without any instruction file the model's first move was its shell (`which gnome-calculator`), which headless mode denied, and the run ended empty. With the managed block in `~/.gemini/GEMINI.md` (what `integrate antigravity install` now writes, and removes again on uninstall) it used the MCP tools. The MCP server's own `instructions` are not enough for agy.
* **Images:** agy offloads MCP images to a file and the model reads that file with `read_file`; when it did, its descriptions were accurate. When it did not (first files run) it hallucinated. The rule text now says to open the image file; after that every screenshot was opened.
* **Capability:** `gemini-3.8-flash-low` does single-app tasks reliably, not the two-app task (2 of 2 failed). Whether a larger agy model does better was not tested.
* **Not tested:** `integrate antigravity install` on the real configuration; interactive sessions (where a permission prompt could be answered).

### Hermes Agent
* **Model (approved by you):** `qwen3.6-35b-a3b` on a local llama.cpp router. Chosen because it is vision-capable (router metadata `input_modalities: text, image`, verified with a tiny image request) and light to run (35B MoE, 3B active, 21.7 GB weights + 0.9 GB projector). The smallest vision-capable alias by file size is `qwen3-vl-32b-thinking` (19.8 GB, dense 32B, thinking); I did not choose it because a dense 32B thinking model is much slower per token and the difference is 1.9 GB. Nothing else on that machine was stopped, evicted or restarted.
* **Router host memory (MemAvailable):** before 111.0 GiB (116,436,984 kB); model loaded and idle 83.0 GiB (87,040,408 kB); at the end of the Hermes runs, just before unloading, 74.0 GiB (77,609,812 kB; larger because of the conversation contexts); after unloading through the router's own `POST /models/unload` 111.1 GiB (116,456,960 kB), and the router listed no loaded model. Loaded from 18:36 to 18:49 (12 minutes). Headroom never went below 74 GiB, far over the required 20 GB.
* **Is the model capable?** Direct probe (before any agent): shown the calculator screenshot it answered "a calculator application ... 'Basic' mode selector ... a large empty white area for input and a keypad ... a prominent orange equals button", which is correct. Through Hermes it listed the 4 folder entries correctly, read the page heading correctly, described the calculator correctly and completed all five tasks. Its weaknesses: slow thinking, and a small mis-statement (the saved path).
* **Hermes only gives a model the image when it knows the route can see:** the first run used the default config; Hermes handed the model a file path instead of the image, and the model **said it could not see the screenshot and described a generic calculator** (not accurate). After I set `model.supports_vision: true` in the throw-away config (only when the router's own metadata says the alias takes images) the image was attached natively and the answers were accurate. A user who points Hermes at a vision model through a custom provider has to declare that too.
* **Per-invocation setup:** a throw-away `HERMES_HOME` (`~/.cache/agent-workspaces/hermes-home`, 454 MB of its own dependency environment, created by Hermes itself) with a generated `config.yaml`: MCP server, the router provider's base URL and model list copied from your config without any secret, `key_env` naming the key variable (the key is placed in the environment of that one process, from your existing `~/.hermes/.env`, and never written or printed), toolset limited to the MCP server. Your real `~/.hermes` was not modified. Hermes starts a background gateway that keeps MCP servers alive between runs; the runner stops exactly the processes whose environment names that throw-away home (a stale one reused an old MCP server in one run and broke it).
* Hermes does not start MCP servers in the project directory (its working directory is HOME), so the runner sets `AW_PROJECT`.
* **Not tested:** `integrate hermes install` on the real configuration; Hermes with its own tools (terminal, browser) enabled; a text-only Hermes route with `describe_screen` (no real vision backend was configured).

### Windows and browsers (the daemon API of `feat/browsers-compat`, wired into the MCP tools)
* `window_move`, `window_resize`, `window_set_geometry` call the daemon's `window.move`, `window.resize`, `window.set_geometry`. Tested for real through the MCP server (`tests/integration/test_mcp_agent_tools_real.py`, class `AgentWindowTools`): a GTK probe window in a floating workspace was placed at 300,200 size 500x300, the reported geometry matched, a pixel inside the new rectangle was the probe's blue banner and a pixel at the old place was not; after `window_move` to 500,350 the banner was there and left no ghost; `window_resize` reported 420x260.
* The `launch_browser` tool (throwaway profile by default, no automation flags) was used by all four agents on their own for "open my browser and visit agdc.dev" in the final browser runs. `launch_app app=browser` is routed to the same daemon method. Without a browser name the first launchable browser is chosen (the daemon's own default had picked a Chromium known only from leftover profile data and answered `E_BROWSER_UNSUPPORTED`; Claude and Codex worked around it in their first browser runs by calling `list_browsers`, and the MCP server now avoids it). User profiles are never used automatically: `request_browser_profile` only asks, the human approves in a terminal. That request path was not exercised with a real agent.

## NOT TESTED (all agents)

Installing into the real agent configurations and running a live session that way; interactive TUI sessions; a real browser-profile request by an agent; non-Latin text; drag-and-drop between apps; two agents of different kinds attached to one workspace; long sessions and stability; the window-move/resize tools (the daemon API does not exist yet, the tools answer `E_UNSUPPORTED`); host clipboard sharing (the tools only ask: `request_permission`); a real vision backend for `describe_screen`; Intel/AMD GPUs; anything on a second machine.
