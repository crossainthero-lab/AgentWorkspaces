# Status

Milestone log. Updated as work progresses.

* Architecture prototype done: one headless labwc (wlroots, GLES2 on the host GPU render node) per workspace,
  driven by a pure-Python Wayland client (virtual pointer, virtual keyboard, screencopy, foreign-toplevel,
  data-control, output-management). Daemon, CLI and API v1 (`docs/api.md`) exist.
