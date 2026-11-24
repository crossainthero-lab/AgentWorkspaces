# Status

* Done and tested: workspace manager (daemon, CLI, API v2), headless labwc workspaces with GPU acceleration on NVIDIA, MCP server, GTK4 viewer with take-over, per-agent opt-in integrations, installers, unit tests (107) and real integration tests (workspaces 17, reliability 21, sandbox 12, agent CLIs 2, not re-run on the security branch), measurements, docs.
* Done and tested in isolation only: GNOME Shell extension (headless isolated shell, stand-ins for daemon/viewer).
* Not done: see `docs/limitations.md` and `docs/testing.md` ("Not tested").
* Security branch (`feat/security`): API v2 with per-workspace credentials, human-only grants, routing identity checks, optional bubblewrap sandbox, reliability tests. See `docs/security-model.md`.
