# Application compatibility

AgentWorkspaces is a general-purpose graphical execution environment, not a browser tool: any Wayland or X11
program can run in a workspace. This table is what was **actually run** in real workspaces (real labwc, real
apps, screenshots and real pointer/keyboard input through the workspace's virtual devices), on the development
workstation (Ubuntu 26.04, NVIDIA RTX 4060 Ti, labwc 0.9.3). "Pass" means: the window appeared in `window.list`,
the screenshot is not blank, and input had an effect the **application itself reported** (event log, stdout,
local web server or clipboard), not just that the click was sent. Test code: `tests/integration/test_compat_real.py`
(`AW_COMPAT_REPORT` writes the numbers below).

| Family | App (version) | Backend | Result | Startup to window | Screenshot (median of 5) | Workspace RAM |
|---|---|---|---|---|---|---|
| GTK3 | gtk3_probe.py (GTK 3.24.52 (PyGObject probe)) | x11 | pass | 0.38 s | 15.1 ms | 186.6 MiB |
| GTK3 | gtk3_probe.py (GTK 3.24.52 (PyGObject probe)) | wayland | pass | 0.16 s | 15.3 ms | 108.0 MiB |
| GTK4 + libadwaita | gnome-calculator (gnome-calculator 50.0) | wayland | pass | 1.21 s | 14.9 ms | 220.9 MiB |
| GTK4 | gtk_probe.py (GTK 4.22.4 (PyGObject probe)) | wayland | pass | 0.86 s | 15.5 ms | 227.8 MiB |
| Qt6 (KDE Frameworks 6) | kdialog (KDE 25.12) | wayland | pass | 0.16 s | 15.0 ms | 103.3 MiB |
| Qt5 | vlc (3.0.23) | xwayland | pass | 0.27 s | 15.0 ms | 181.9 MiB |
| Electron | stock Electron + probe app (33.4.11) | wayland | pass | 0.83 s | 15.1 ms | 417.0 MiB |
| Chromium-based | Google Chrome (Google Chrome 152.0.7977.82) | wayland | pass | 1.85 s | 15.0 ms | 667.4 MiB |
| Chromium-based | Google Chrome (Google Chrome 152.0.7977.82) | x11 | pass | 0.85 s | 15.3 ms | 453.3 MiB |
| Firefox | Firefox (vendor tarball) (Mozilla Firefox 160.0.1) | wayland | pass | 1.21 s | 14.8 ms | 1132.5 MiB |
| X11 (Athena) | xmessage (-) | xwayland | pass | 0.27 s | 14.9 ms | 157.1 MiB |

Measurements are one run, warm caches, a 1280x800 workspace with GPU rendering, private audio on. "Startup" is
from the launch call to the first window / the app's own ready event; "Workspace RAM" is the cgroup memory of the
whole workspace (compositor, bus, audio daemons, app and its helpers) after the app is up; it includes file cache charged to the cgroup, so it overstates the resident size. They are indicative, not
a benchmark.

## How each row was verified

| App | Input check |
|---|---|
| GTK3 / GTK4 probes | content rectangle in the screenshot equals the reported geometry; a click is reported with window coordinates; "Grüße ab" typed (non-US characters go through the workspace clipboard) arrives in the entry |
| GNOME Calculator | `6*7` + Enter typed, Ctrl+A / Ctrl+C, `clipboard.get` returns `42` (this also proves the workspace clipboard round trip with a libadwaita app) |
| Qt6 (`kdialog --inputbox`) | typed text including `ünï` + Enter is printed on the app's stdout |
| Qt5 (VLC) | the first-run policy dialog is closed by a real click on its Continue button |
| xmessage | a real click on its button prints `yes` on stdout (`-print`) |
| Electron probe app, Chrome, Firefox | a local web server (127.0.0.1) receives the page's own events for the typed text and the click |
| HiDPI | a workspace with `--scale 2`: screenshot size is physical pixels, the pointer coordinates are logical |

Also run and looked at (screenshot + input by hand, not in the automated suite): KDE Kate 25.12 (Qt6) opened
and rendered its welcome page.

## What was found and what it means

* **Everything above works with the launcher's default environment** (`GDK_BACKEND=wayland,x11`,
  `QT_QPA_PLATFORM=wayland;xcb`, `MOZ_ENABLE_WAYLAND=1`, `ELECTRON_OZONE_PLATFORM_HINT=wayland`): no extra variable
  or flag had to be added for GTK3/4, Qt6, Chrome or Firefox. `GTK_USE_PORTAL=0` and a private session bus without
  service directories mean no app reaches a desktop portal.
* **Qt5 has no Wayland plugin on this machine** (`qt5/plugins/platforms` has only `xcb`, `offscreen`, ...): Qt5
  apps fall back to XWayland through the `wayland;xcb` list and work there. Qt6 has `libqwayland` and runs native.
* **VLC** printed `PulseAudio server connection failure: Connection refused` before private audio existed; with it,
  its Pulse output has a server (a null sink).
* **Firefox** is a snap on this system and is refused ([browsers.md](browsers.md)); the matrix row is the
  Mozilla vendor build 160.0.1. Its 160 first-run terms screen needs a click. Its log says
  `Sandbox: CanCreateUserNamespace() unshare(CLONE_NEWPID): EPERM`: the content-process namespace sandbox cannot be
  created under this system's user-namespace restrictions (what that costs in protection was not examined).
* **Electron** needs `--no-sandbox` (the npm build's `chrome-sandbox` is not setuid root, so it aborts otherwise);
  that is a property of the stock binary, not of the workspace. Google Chrome (deb) runs with its sandbox.
* **Chrome** on Wayland draws its own title bar (client-side decoration); on `--ozone-platform=x11` labwc decorates
  it. The labwc window list reports their geometry differently for that reason ([windows.md](windows.md)).
* **Window policy**: the default `maximized` makes dialogs fill the screen (kdialog and the VLC dialog did). Use
  `--window-policy floating` for dialog-heavy apps.
* **Windows labwc does not announce** (`xeyes`, `xclock`) used to be invisible to `window.list`; they are listed
  now through the workspace's own X server.

## Not tested (and why)

* Qt5 on native Wayland: no Qt5 Wayland plugin installed (`qtwayland5` package).
* A `.deb` Firefox, Chromium, Brave, Edge, Vivaldi: not installed here; no sudo to install them.
* Snap and flatpak apps: refused for browsers; other snap/flatpak GUI apps were not tried.
* Java/Swing, SDL games, Wine, LibreOffice, Blender, Steam: not installed or not tried.
* Apps that need a desktop portal (file chooser through xdg-desktop-portal, screen sharing): there is none in a
  workspace.
* Non-NVIDIA GPUs; multiple outputs; IME composition; drag and drop between windows.
