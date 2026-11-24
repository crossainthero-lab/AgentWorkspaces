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
        host_rt = f"/run/user/{os.getuid()}"
        self.env = {"PATH": "/usr/bin:/bin", "HOME": home, "XDG_CONFIG_HOME": os.path.join(home, ".config"),
                    "XDG_STATE_HOME": os.path.join(home, ".state"), "AW_BASE_DIR": self.base, "PYTHONPATH": ROOT,
                    "XDG_RUNTIME_DIR": host_rt, "DBUS_SESSION_BUS_ADDRESS": f"unix:path={host_rt}/bus",
                    "LANG": "C.UTF-8", "GSETTINGS_BACKEND": "memory"}
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
        return Client(autostart=False)

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
        c = Client(autostart=False)
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


def wait_for(pred, timeout=10.0, step=0.1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None
