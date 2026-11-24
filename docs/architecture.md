# Architecture

## Decision

One **headless labwc** (wlroots, `WLR_BACKENDS=headless`) per workspace, rendering with GLES2 on the host GPU's DRM render node, supervised in its own systemd user slice, driven by a stdlib-only Python Wayland client.

```
 you (GNOME session)                       agent-workspacesd (user process)
 ┌──────────────────┐   unix socket API    ┌──────────────────────────────────────────────┐
 │ CLI / MCP / Gnotch│ ───────────────────▶ │ manager: create/stop/launch, takeover, events │
 │ viewer window     │ ◀─── events ──────── │ session: Wayland client per workspace         │
 └──────────────────┘                       └───────┬──────────────────────────────────────┘
        │ screencopy + virtual input                │ screencopy, virtual-pointer/keyboard,
        │ (viewer connects directly)                │ foreign-toplevel, data-control, output-mgmt
        ▼                                           ▼
   ┌──────────── workspace "research" ────────────┐   ┌── workspace "web" ──┐   (own slice, own seat,
   │ labwc (headless, GLES2 on /dev/dri/renderD128)│   │  labwc + apps       │    pointer, focus, clipboard,
   │ private XDG_RUNTIME_DIR / D-Bus / HOME        │   └─────────────────────┘    outputs)
   │ apps: GTK4, Chromium, Electron, XWayland …    │
   └───────────────────────────────────────────────┘
```

Why this is graphical isolation and not "another virtual desktop": each workspace is a separate Wayland server with its own `wl_seat`. Pointer position, keyboard focus, focused window, clipboard and output configuration live in that server. Nothing the agent does is a request to your GNOME Shell.

## What was built from what

* Compositor: labwc 0.9.3 (wlroots 0.19) from the Ubuntu archive. No patches.
* Client: `aw/wl.py` speaks the Wayland wire protocol directly (about 300 lines, protocol XML in `aw/protocols/`). No pip dependency, nothing to compile. `aw/session.py` implements screenshots, input, windows, clipboard, display config on top of it.
* Input is **only** `zwlr_virtual_pointer_v1` / `zwp_virtual_keyboard_v1` on the workspace socket. There is no xdotool, ydotool, uinput or `wtype` against anything, and `WorkspaceSession` can only be constructed from an explicit absolute socket path (it never reads `WAYLAND_DISPLAY`).
* Daemon: `aw/daemon.py` (API), `aw/manager.py` (lifecycle). CLI: `aw/cli.py`. MCP: `aw/mcp.py`. Viewer: `aw/viewer.py` (GTK4).
* Process supervision: `systemd-run --user --scope --slice=aw-<id>.slice` for the compositor, the private bus and every app. Destroying a workspace stops the slice, which removes every process in it, and the slice gives per-workspace memory/CPU accounting and an optional `MemoryMax`.

## Evaluated alternatives and what was measured

All numbers below were measured on the development workstation (16 cores, NVIDIA RTX 4060 Ti, Ubuntu 26.04, GNOME Shell 50.1). "PSS" is proportional set size from `/proc/<pid>/smaps_rollup`.

| Candidate | Result | Why rejected / kept |
|---|---|---|
| **labwc headless (chosen)** | idle compositor 81 to 100 MiB PSS with the NVIDIA GL stack loaded, 18 MiB PSS with `gpu=off` (pixman), 0.0 % CPU idle, up in 0.16 to 0.26 s | All required protocols present: virtual-pointer v2, virtual-keyboard, screencopy v3, ext-image-copy-capture, foreign-toplevel v3, data-control v2, output-management v4. GLES2 on the NVIDIA render node confirmed (see below) |
| `gnome-shell --headless --virtual-monitor` | gnome-shell alone 209 MiB PSS (335 MiB RSS); with the services D-Bus activated around it about 330 MiB PSS over about 30 processes. Its log printed "Registering session with GDM" even with a private session bus | Heavy, drags in the whole GNOME session stack and touches system services; input would need the RemoteDesktop portal. I did not run it again after the GDM log line. Not pursued further |
| `gnome-shell --devkit` / `mutter --devkit` | Not available: no `mutter-devkit` binary installed | Could not be tested |
| labwc nested in a window (`WLR_BACKENDS=wayland`) | Works, 184 MiB RSS | The workspace dies with the window. With `WLR_BACKENDS=headless,wayland` closing the window keeps the compositor alive but there is no way to re-open the window (labwc has no output creation command), windows spread over two outputs. Cannot meet "agent keeps working after the viewer closes, and I can come back" |
| sway, cage, weston (wlroots/own headless) | Not installed on the workstation; weston exists on the Aurora test machine | Not prototyped. labwc met every requirement and was already installed, so I stopped there. This is not a claim that they are worse |
| VNC/RDP viewer (wayvnc, gnome-remote-desktop) | wayvnc not installed; not tested | A custom GTK4 viewer using damage-driven screencopy was built instead and measured (below) |
| Virtual machines, containers | Not tested | Would not share the host GPU/compositor path cheaply; not needed for graphical isolation |
| A second GNOME virtual desktop | Not tested (by design) | Same pointer, same keyboard focus, same Shell: not isolation |
| xdotool/ydotool/uinput | Rejected by design | They address the host session or the whole machine. Agent input must never be able to target the personal desktop |

### GPU acceleration (evidence, not flags)

`docs/evidence/gpu-evidence.txt` (machine: the workstation with the RTX 4060 Ti, driver 595.99.02):

* wlroots log of the workspace compositor: `EGL vendor: NVIDIA`, `EGL driver name: nvidia`, render node `/dev/dri/renderD128`.
* `nvidia-smi pmon` lists the workspace's `labwc` as a Graphics process (4 MB framebuffer) and a Vulkan app (`vkcube --wsi wayland`) running inside the workspace as `C+G` with 7 to 11 % SM and 19 MB, while the same listing for a `gpu=off` workspace does not contain the compositor.
* The integration test `GpuAcceleration` asserts both PIDs appear in `nvidia-smi pmon`.

Aurora (the other test machine) has an Intel iGPU without a working NVIDIA driver, so GPU numbers were not taken there.

### Viewer transport

The viewer reads frames with `wlr-screencopy copy_with_damage` (the compositor only answers when pixels change), so an idle workspace costs about 0 % CPU in the viewer. Measured in the integration stand-in desktop (not your real desktop): see [measurements](measurements.md). Alternatives not chosen: nested window (above), VNC (not installed), embedding a compositor window (not possible without the nested backend).

## Data flow for one agent action

1. Agent calls an MCP tool or the CLI with its kind (`AW_AGENT_KIND`).
2. The daemon checks assignment (agent kind vs workspace) and manual takeover (`E_PAUSED`).
3. The workspace's `WorkspaceSession` sends the request through the workspace's own virtual pointer/keyboard.
4. A screenshot is a screencopy of that workspace's output, converted to PNG in Python (about 16 ms for 1280x720 in the measurements).
