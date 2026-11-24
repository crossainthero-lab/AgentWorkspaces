# Status

Version 0.1 (branch `feat/integration`). Everything below was run on one Ubuntu 26.04 / GNOME Shell 50 workstation; details and the exact commands are in [docs/testing.md](docs/testing.md).

## Done and tested

* **Core**: daemon, CLI, API v2 (every request authenticated, per-workspace credentials, admin credential for the human), headless labwc workspaces with GPU acceleration on NVIDIA, MCP server, installers. Unit tests (176) and real integration tests (workspaces 17, reliability 22, sandbox 13, windows + audio 8, browsers 10 + 1 skipped, MCP tool surface 3) pass on the integrated branch. See [docs/security-model.md](docs/security-model.md), [docs/api.md](docs/api.md).
* **Security**: human-only grants and approvals, routing identity checks (SO_PEERCRED before the first Wayland byte), optional bubblewrap sandbox (`create --sandbox`), crash/disconnect/restart behaviour ([docs/isolation-model.md](docs/isolation-model.md)).
* **Browsers, windows, audio**: browser discovery and launch (throwaway, agent and human-authorised profile copies), one window list for Wayland and the workspace's private XWayland with move/resize, private PipeWire sink per workspace with capture and human-only host routing. Chrome was tested for real; Firefox was not run in the final pass ([docs/browsers.md](docs/browsers.md), [docs/windows.md](docs/windows.md), [docs/audio.md](docs/audio.md)).
* **Viewer and GNOME extension**: one GTK4 window for all workspaces (sidebar, overview, Agent / Paused / You control, approvals and grants, thick orange agent pointer, agent taskbar), and a full-screen environment mode. With Desktop Cube enabled, Ctrl+Alt+Down/Up turn the cube onto and off the agent workspace. Run only inside isolated headless GNOME Shells with the real daemon, workspaces and viewer: `run_real.py --plain` 50/50, `run_isolated.py` 29/29, `run_cube.py` 21/21 with 2 and with 4 personal workspaces. [docs/viewer.md](docs/viewer.md), [docs/gnome-extension.md](docs/gnome-extension.md).
* **Agent integrations**: Claude Code (Haiku 5.5), Codex (GPT-6-Luna), Antigravity (Gemini 3.8 Flash low) and Hermes (Qwen3.6-35B-A3B on a local llama.cpp router) were run with real models and ordinary prompts, on the `feat/agents-integration` branch before the final merge. Claude Code, Codex and Hermes completed all five task types (browser, text editor, file manager, screenshot, two apps); Antigravity completed four and failed the two-app task. [docs/compatibility-matrix.md](docs/compatibility-matrix.md).

## Release state (branch `feat/release-agents`)

Claude Code and Hermes are installed on the development machine's real configuration with the graphical-isolation guard ON; reliability 95.5 % (21 of 22) and 90.9 % (20 of 22) on independently verified runs, post-install smoke tests and a byte-exact rollback proof. See [docs/RELEASE.md](docs/RELEASE.md) and [docs/compatibility-matrix.md](docs/compatibility-matrix.md#release-reliability-pass-claude-code-and-hermes-agent). Codex and Antigravity are parked.

## Fixed during integration

* `window.set_geometry` could wait out a 5 s Wayland roundtrip timeout without an error (the window service dispatched on the session's connection without the session lock, so the session's own thread could consume the reply). Locked, and the roundtrip now polls in 0.2 s slices. Regression tests: `tests/unit/test_session_pure.py` (deterministic) and the stall test in `tests/integration/test_windows_audio_real.py`. A 30 s stall reported with a viewer attached was not reproduced separately; it is the same mechanism with more event traffic.
* The GNOME extension finds the admin credential like the other clients (`AW_ADMIN_TOKEN`, `AW_ADMIN_TOKEN_FILE`, `<base>/admin.token`, base `AW_BASE_DIR` or the runtime directory).
* New unit test: every API method, and every method any bundled client calls, has an authorisation entry.

## Not tested

See [docs/limitations.md](docs/limitations.md) and the "Not tested" list in [docs/testing.md](docs/testing.md). In short: the viewer and the extension on a real, live GNOME session (never enabled in one); any real browser profile of yours and real speakers; Firefox in the final pass; Qt apps, Intel/AMD GPUs, multi-monitor and fractional scaling, the Desktop Cube skybox option, a real display, a model-driven agent in the viewer (the first cube-face visit shows the wallpaper for about 0.4 s); the real agent CLIs and models on the integrated branch (not re-run after the merge); a hostile program running as your own user (not defended against).
