# Status

* Done and tested: workspace manager (daemon, CLI, API v1), headless labwc workspaces with GPU acceleration on NVIDIA, MCP server, GTK4 viewer with take-over, per-agent opt-in integrations, installers, unit tests and real integration tests, measurements, docs.
* Viewer (branch feat/gnome-viewer): one window for all workspaces (sidebar, overview, Agent / Paused / You control, request and grant bar, create / stop / close). Tested end to end in an isolated headless GNOME Shell with the real daemon and workspaces (53 checks), including with Desktop Cube, Tiling Shell and the other extensions of the developer's session. See docs/viewer.md.
* GNOME Shell extension: enabled and exercised only in isolated headless shells; never enabled in the real session.
* Not done: see `docs/limitations.md` and `docs/testing.md` ("Not tested").
