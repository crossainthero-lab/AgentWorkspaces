# Measurements

Machine for every number below: the development **workstation** (16 CPUs, 30 GB RAM, NVIDIA GeForce RTX 4060 Ti, driver 595.99.02, Ubuntu 26.04, kernel 7.0). Nothing was measured on the Aurora machine (no working NVIDIA driver there; it was also unreachable when I tried to run the unit tests there). Raw reports: `scripts/measure.py` (workspaces), `scripts/measure_viewer.py` (viewer), `scripts/gpu_evidence.py` (GPU). Re-run them on your hardware.

## Memory and CPU per workspace

Method: PSS summed over every process in the workspace's systemd slice (`/proc/<pid>/smaps_rollup`), plus the cgroup `MemoryCurrent`; CPU is the delta of the slice's `CPUUsageNSec` over 8 s. Single run per configuration, 1280x720.

| Configuration | PSS | cgroup memory | CPU (8 s, idle) |
|---|---|---|---|
| compositor only, `gpu=auto` (GLES2 on the NVIDIA render node) | 100 MiB | 74 MiB | 0.0 % |
| compositor only, `gpu=off` (pixman) | 17.5 MiB | 14 MiB | 0.0 % |
| + GTK4 test app, `gpu=auto` | 295 MiB (labwc 94, XWayland 88, app 112, bus 0.5) | 207 MiB | 0.1 % |
| + GTK4 test app, `gpu=auto`, `--no-xwayland` | 233 MiB (labwc 97, app 136) | not recorded | not recorded |
| + GTK4 test app, `gpu=off` | 50 MiB (labwc 22, app 28) | 42 MiB | 0.0 % |
| two workspaces, each with the GTK4 app, `gpu=auto` | 570 MiB total | | |

Note: with `gpu=auto`, the NVIDIA driver stack probes X11 and wakes the lazily started XWayland (about 88 MiB) even for a Wayland-only app. `--no-xwayland` avoids it (the "no-xwayland" row is from a separate single run).

Chromium and Electron were used as test apps but their memory was not measured.

## Startup

Workspace create to ready (compositor up, socket present, display configured): 261 ms (`gpu=auto`), 156 ms (`gpu=off`), one run each.

## Screenshot latency

Method: 60 consecutive `screen.screenshot` calls on an idle workspace with a GTK4 app, first 5 discarded (n = 55). "roundtrip" is measured by the client including JSON, base64 and the socket; "capture+encode" is measured inside the daemon (screencopy, Python pixel conversion, PNG level 1).

| | median | p95 | min | max |
|---|---|---|---|---|
| roundtrip, 1280x720, `gpu=auto` | 16.1 ms | 17.3 ms | 11.0 ms | 21.2 ms |
| capture+encode in the daemon, `gpu=auto` | 15.7 ms | 16.1 ms | 15.0 ms | 16.6 ms |
| roundtrip, `gpu=off` | 18.4 ms | 19.6 ms | 17.7 ms | 21.0 ms |

A single CLI screenshot at 1024x640 took 7.7 ms (one sample, earlier prototype).

## Input to pixels

Method: agent pointer move through the API, then poll a screenshot of the strip that contains the test app's "pointer: x,y" label until the bytes change (n = 27, first 3 discarded). The poll interval (one screenshot, about 16 ms) limits resolution.

| | median | p95 |
|---|---|---|
| move to changed pixels in the workspace, `gpu=auto` | 16.9 ms | 22.9 ms |
| same, `gpu=off` | 16.5 ms | 16.6 ms |

The headless output refreshes at 60 Hz, so about 16.7 ms is the floor.

## Viewer

Method: the viewer runs inside the **stand-in desktop** (a headless labwc with the pixman renderer and GTK's cairo renderer, not your real GNOME session), n = 30 samples, first 3 discarded.

| | |
|---|---|
| idle viewer CPU (8 s) | 0.0 %, 45 MiB PSS |
| agent pointer move to changed pixels in the viewer window (read from the stand-in desktop) | median 17.0 ms, p95 33.3 ms, min 16.5, max 33.5 |
| viewer CPU while the workspace pointer moves about 60 times a second for 5 s | 37.9 % of one core, 56 MiB PSS |

The 38 % figure is software rendering in the stand-in desktop plus the Python frame copy (a full 1280x720 frame per update); on a GPU-composited GNOME session it is expected to be lower, but that was **not measured**, because no test may open a window on your real desktop. The viewer's input path (your pointer/keys to the workspace) was verified functionally, but its latency was not measured.

## GPU

`docs/evidence/gpu-evidence.txt`, same machine: the workspace compositor uses `EGL vendor: NVIDIA` on `/dev/dri/renderD128`; `nvidia-smi pmon` lists the compositor (type G, 4 MB) and a Vulkan app inside the workspace (type C+G, 7 to 11 % SM, 19 MB) as GPU processes. With `gpu=off` the compositor is not listed. GPU engine utilisation of the compositor itself reads `-` (below the sampling resolution) for an idle desktop.
