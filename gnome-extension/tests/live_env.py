"""Shared pieces for the isolated-shell tests that use the REAL daemon, REAL workspaces and the REAL viewer.

The daemon is started with a private base dir (AW_BASE_DIR), private HOME / XDG_CONFIG_HOME and no display
variables. It still needs the user's systemd manager bus (only to create the workspaces' transient scopes, as in
tests/integration). Workspace compositors are headless labwc instances; their apps get an environment built from
scratch by the AgentWorkspaces launcher. Nothing here starts a program on the user's screen: the viewer is started
by the daemon with the display of the ISOLATED shell, which the extension passes (workspace.enter display_env).
"""
import os
import shutil
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path[:0] = [ROOT, HERE]

from aw.client import ApiError, Client  # noqa: E402

PROBE_APP = os.path.join(ROOT, "tests", "apps", "gtk_probe.py")


class Daemon:
    def __init__(self, session, tag="gs"):
        self.session = session
        self.base = os.path.join(session.rt, "agent-workspaces")   # where the extension looks: $XDG_RUNTIME_DIR/agent-workspaces
        os.makedirs(self.base, mode=0o700, exist_ok=True)
        home = os.path.join(session.base, "dhome")
        os.makedirs(home, mode=0o700, exist_ok=True)
        # Fully private: the daemon gets the isolated session's runtime dir and bus, never the user's. It therefore finds
        # no systemd user manager and runs workspaces as plain processes (no transient scopes, no cgroup accounting).
        from gnome_session import assert_private_bus
        self.env = {"PATH": "/usr/bin:/bin", "HOME": home, "XDG_CONFIG_HOME": os.path.join(home, ".config"),
                    "XDG_STATE_HOME": os.path.join(home, ".state"), "AW_BASE_DIR": self.base, "PYTHONPATH": ROOT,
                    "XDG_RUNTIME_DIR": session.rt, "DBUS_SESSION_BUS_ADDRESS": session.env["DBUS_SESSION_BUS_ADDRESS"],
                    "LANG": "C.UTF-8", "GSETTINGS_BACKEND": "memory"}
        assert_private_bus(self.env["DBUS_SESSION_BUS_ADDRESS"], session.base)
        for k in ("AW_VIEWER_DEBUG", "AW_VIEWER_METRICS"):     # opt-in diagnostics for the viewer the daemon starts
            if os.environ.get(k):
                self.env[k] = os.environ[k]
        for k in ("WAYLAND_DISPLAY", "DISPLAY", "XAUTHORITY"):
            assert k not in self.env
        self.proc = None

    def start(self):
        os.environ["AW_BASE_DIR"] = self.base
        self.proc = subprocess.Popen([sys.executable, "-m", "aw.daemon"], env=self.env, cwd=ROOT,
                                     stdout=open(os.path.join(self.base, "daemon.log"), "wb"), stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, start_new_session=True)
        end = time.monotonic() + 8
        while time.monotonic() < end and not os.path.exists(os.path.join(self.base, "api.sock")):
            time.sleep(0.05)
        return self

    def client(self) -> Client:
        """The human's client (admin credential, like the CLI and the viewer)."""
        return Client(autostart=False, role="admin")

    def agent_client(self, workspace) -> Client:
        """An agent's client: only the workspace's own token file, exactly like an MCP session."""
        return Client(autostart=False, role="agent", workspace=workspace)

    def stop(self):
        c = None
        try:
            c = self.client()
            for w in c.call("workspace.list")["workspaces"]:
                c.call("workspace.destroy", workspace=w["id"])
        except Exception:
            pass
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        os.environ.pop("AW_BASE_DIR", None)

    def viewer_pids(self):
        """Exact pids of viewer processes started in this session (children of the daemon)."""
        out = []
        for d in os.listdir("/proc"):
            if d.isdigit():
                try:
                    with open(f"/proc/{d}/stat") as f:
                        ppid = int(f.read().rsplit(")", 1)[1].split()[1])
                    if ppid == self.proc.pid and b"aw.viewer" in open(f"/proc/{d}/cmdline", "rb").read():
                        out.append(int(d))
                except (OSError, ValueError, IndexError):
                    pass
        return out


class ScriptedAgent(threading.Thread):
    """A scripted AgentWorkspaces client, NOT a model-driven agent: it drives a workspace through the same
    input path as an MCP agent (daemon API input.click / input.type) so that there is live change to watch."""

    LINES = [
        "Agent notes (scripted demo client, not a model)\n",
        "1. Opened the project folder and read the README.\n",
        "2. The build needs two packages; checking the lockfile.\n",
        "3. Wrote the failing test first, then the fix.\n",
        "4. Running the suite again.\n",
        "5. All checks pass. Preparing the summary.\n",
        "6. Waiting for review.\n",
    ]

    def __init__(self, ws, word_delay=0.22, line_pause=1.0):
        super().__init__(daemon=True)
        self.ws, self.word_delay, self.line_pause = ws, word_delay, line_pause
        self.stop_flag = False
        self.typed = 0
        self.errors = []
        self.paused_rejections = 0

    def run(self):
        c = Client(autostart=False, role="agent", workspace=self.ws)   # the workspace's own credential
        try:
            for line in self.LINES:
                for word in line.split(" "):
                    if self.stop_flag:
                        return
                    text = word + (" " if not word.endswith("\n") else "")
                    while not self.stop_flag:
                        try:
                            c.call("input.type", workspace=self.ws, text=text)
                            self.typed += len(text)
                            break
                        except ApiError as e:
                            if e.code == "E_PAUSED":      # the human has taken over: wait, do not fight
                                self.paused_rejections += 1
                                time.sleep(0.3)
                                continue
                            self.errors.append(str(e))
                            return
                    time.sleep(self.word_delay)
                time.sleep(self.line_pause)
        finally:
            c.close()


class DemoAgent(threading.Thread):
    """Scripted client for the demo (NOT a model): works in a floating workspace that has a calculator at
    (60,70) 380x560 and a text editor behind it. It uses the workspace's own credential and the same API an MCP
    agent uses: focus a window, glide the pointer, click buttons, type. The pointer positions are real
    `input.move` calls in small steps, so the viewer's orange marker follows real positions."""

    BUTTONS = {"7": (104, 481), "8": (176, 481), "9": (248, 481), "4": (104, 529), "5": (176, 529), "6": (248, 529),
               "1": (104, 577), "2": (176, 577), "3": (248, 577), "0": (104, 625), "+": (320, 577), "*": (320, 481),
               "-": (320, 529), "=": (392, 601), "C": (104, 433)}
    EDITOR_TEXT = (720, 320)
    JOBS = [("7*6=", "7 x 6 = 42"), ("8+5=", "8 + 5 = 13"), ("9*9=", "9 x 9 = 81"), ("4+3=", "4 + 3 = 7")]

    def __init__(self, ws, glide_s=0.45, step_s=0.04):
        super().__init__(daemon=True)
        self.ws, self.glide_s, self.step_s = ws, glide_s, step_s
        self.stop_flag = False
        self.pos = (640.0, 360.0)
        self.clicks = 0
        self.focus_calls = 0
        self.errors = []
        self.paused_rejections = 0

    def _call(self, c, method, **kw):
        while not self.stop_flag:
            try:
                return c.call(method, workspace=self.ws, **kw)
            except ApiError as e:
                if e.code == "E_PAUSED":          # the human has control: wait, never fight
                    self.paused_rejections += 1
                    time.sleep(0.3)
                    continue
                self.errors.append(str(e))
                raise
        raise RuntimeError("stopped")

    def glide(self, c, x, y):
        x0, y0 = self.pos
        n = max(4, int(self.glide_s / self.step_s))
        for i in range(1, n + 1):
            t = i / n
            t = t * t * (3 - 2 * t)                 # ease in/out
            self._call(c, "input.move", x=x0 + (x - x0) * t, y=y0 + (y - y0) * t)
            time.sleep(self.step_s)
        self.pos = (x, y)

    def click(self, c, x, y):
        self.glide(c, x, y)
        time.sleep(0.12)
        self._call(c, "input.click", x=x, y=y)
        self.clicks += 1
        time.sleep(0.3)

    def window_id(self, c, needle):
        for w in self._call(c, "window.list")["windows"]:
            if needle in ((w.get("app_id") or "") + (w.get("title") or "")):
                return w["id"]

    def focus(self, c, needle):
        wid = self.window_id(c, needle)
        if wid is not None:
            self._call(c, "window.action", window=wid, action="focus")
            self.focus_calls += 1
            time.sleep(0.4)

    def run(self):
        c = Client(autostart=False, role="agent", workspace=self.ws)
        try:
            for keys, note in (self.JOBS * 50):
                if self.stop_flag:
                    return
                self.focus(c, "Calculator")
                self.click(c, *self.BUTTONS["C"])
                for k in keys:
                    self.click(c, *self.BUTTONS[k])
                time.sleep(0.8)
                self.focus(c, "TextEditor")
                self.click(c, *self.EDITOR_TEXT)
                self._call(c, "input.type", text=note + "\n")
                time.sleep(1.0)
        except (ApiError, RuntimeError, OSError):
            pass
        finally:
            c.close()


def wait_for(pred, timeout=10.0, step=0.1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None
