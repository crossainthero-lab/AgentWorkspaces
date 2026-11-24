# AgentWorkspaces

Give AI agents their own graphical workspace, so computer-use, screenshots and app driving never touch your real mouse, keyboard focus or windows.

Each workspace is a separate headless Wayland compositor (wlroots/labwc) on the same kernel and GPU as your desktop. An agent's input goes only through that compositor's own virtual-input protocols. It has no code path to your personal session.

Status: early development (Ubuntu GNOME on Wayland only). See `STATUS.md` for what has been built and measured, and `docs/` for the design.

License: MIT.
