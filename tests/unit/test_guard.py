"""UNIT tests of the graphical-isolation guard's decisions and rewriting (aw/guard.py). Real-shell tests against a
stand-in desktop are in tests/integration/test_guard_real.py."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from aw import guard  # noqa: E402

BLOCKED = [
    "xdg-open https://example.org",
    "xdg-open",
    "gio open /tmp/a.pdf",
    "gtk-launch org.gnome.Calculator",
    "gapplication launch org.gnome.Calculator",
    "gapplication action org.gnome.Calculator new-window",
    "gnome-open x", "sensible-browser http://x", "x-www-browser http://x",
    "dbus-send --session --dest=org.gnome.Shell /org/gnome/Shell org.gnome.Shell.Eval string:'1'",
    "gdbus call --session --dest org.freedesktop.Application --object-path /x --method org.freedesktop.Application.Activate",
    "busctl --user call org.gnome.Shell /org/gnome/Shell org.gnome.Shell Eval s 1",
    "systemd-run --user firefox", "systemd-run --user --scope xeyes",
    "systemctl --user start app-gnome-calculator.service", "systemctl --user restart gnome-shell.service",
    "xdotool key a", "ydotool click 0x00", "wtype hello", "wmctrl -l", "scrot /tmp/x.png", "grim /tmp/x.png",
    "gnome-screenshot -f x.png", "import -window root x.png", "xclip -o", "wl-paste", "xdg-screensaver reset",
    "notify-send hi", "gsettings set org.gnome.desktop.interface color-scheme prefer-dark",
    "DISPLAY=:0 xeyes", "export DISPLAY=:0", "env DISPLAY=:1 gnome-calculator", "WAYLAND_DISPLAY=wayland-0 gnome-calculator",
    "WAYLAND_DISPLAY=/run/user/1000/wayland-0 foot", "XDG_RUNTIME_DIR=/run/user/1000 xeyes",
    "export XDG_RUNTIME_DIR=/run/user/1000; xeyes", "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus gdbus call x",
    "XAUTHORITY=/home/u/.Xauthority xeyes",
    "python3 -c \"import os; os.environ['DISPLAY']=':0'; import tkinter; tkinter.Tk()\"",
    "ls /run/user/1000/wayland-0", "cat /run/user/1000/bus", "ls /tmp/.X11-unix", "foot --server /tmp/wayland-1",
    "nohup xdg-open http://x &", "setsid -f xdg-open http://x", "timeout 5 xdg-open x", "env -i xdg-open x",
    "sh -c 'xdg-open http://x'", "bash -lc \"gio open x\"", "echo hi; (xdotool key a)", "true && xdotool key a",
    "echo $(xdg-open x)", "echo `xdg-open x`", "eval 'xdg-open x'", "sudo -n xdotool key a", "nice -n 5 xdotool key a",
    "xargs xdg-open < urls.txt", "at now <<< 'xeyes'",
    "sed -i 's/a/b/' ~/.claude/settings.json", "echo '{}' > /home/u/.claude/settings.json", "rm ~/.hermes/shell-hooks-allowlist.json",
    "python3 -c \"open('/home/u/.hermes/config.yaml','w').write('')\"", "tee .claude/settings.local.json < x",
    "cp x /home/u/.local/share/agent-workspaces/aw/guard.py", "hermes hooks revoke foo",
    "python3 -m webbrowser http://x", "python3 -c \"import webbrowser; webbrowser.open('http://x')\"", "open http://x",
    "/opt/google/chrome/chrome http://x", "/usr/bin/google-chrome-stable --new-tab http://x", "env FOO=1 google-chrome http://x",
    "sh -c 'google-chrome http://x'", "nohup chromium http://x >/dev/null 2>&1 &", "(setsid brave-browser http://x &)",
    "google-chrome --headless --user-data-dir=$HOME/.config/google-chrome http://x",
    "google-chrome --user-data-dir=/home/u/.config/google-chrome --headless", "chromium --user-data-dir ~/.config/chromium http://x",
    "firefox --headless -profile /home/u/.mozilla/firefox/abc.default http://x", "firefox -P default http://x",
    "code .", "code /tmp/file.txt", "slack", "discord", "obsidian obsidian://open?vault=x", "signal-desktop", "codium .",
    "flatpak run org.mozilla.firefox", "kioclient5 exec http://x", "mimeopen file.pdf", "xdg-open http://x &",
    "f(){ google-chrome \"$@\"; }; f http://x", "for u in a b; do firefox $u; done", "if true; then google-chrome http://x; fi",
    "ln -s /usr/bin/google-chrome-stable /tmp/c", "cp /usr/bin/firefox /tmp/f", "BROWSER=google-chrome python3 -m webbrowser x",
    "google-chrome https://example.org", "firefox https://example.org", "firefox", "chromium-browser x",
    "/usr/bin/google-chrome-stable x", "nohup google-chrome x &", "brave-browser", "epiphany x",
]
ALLOWED = [
    "cat ~/.claude/settings.json", "grep -n hooks ~/.hermes/config.yaml", "jq .env ~/.claude/settings.json",
    "ls -la", "git status", "git log --oneline -5", "git commit -m 'fix: xdg-open handling in docs'",
    "grep -rn xdg-open src/", "echo xdg-open is blocked", "cat README.md | head", "python3 script.py --flag",
    "python3 -m pytest -q", "pip --version", "pip install requests", "npm --version", "npm run dev", "npm test",
    "node -e 'console.log(1)'", "make -j4", "cargo build --release", "go test ./...", "docker ps", "docker run --rm alpine true",
    "curl -s http://127.0.0.1:8000/ | head", "ssh user@host 'ls /tmp'", "scp a b:/tmp/", "rsync -a a/ b/",
    "systemctl status ssh", "systemctl --user status agent-workspaces", "systemctl --user list-units",
    "systemctl --user restart agent-workspaces.service", "journalctl --user -u foo -n 5",
    "google-chrome --headless --disable-gpu --screenshot=/tmp/x.png http://127.0.0.1:8000/",
    "chromium --headless=new --dump-dom http://127.0.0.1:8000/", "firefox --headless --screenshot /tmp/x.png http://127.0.0.1",
    "code --list-extensions", "code --install-extension ms-python.python", "code --version", "libreoffice --headless --convert-to pdf a.odt",
    "google-chrome --user-data-dir=/tmp/p --no-first-run about:blank", "firefox --profile /tmp/ffp --version",
    "git commit -m 'open the browser docs'", "echo open", "grep -rn webbrowser src/", "python3 -c \"print('webbrowser')\"",
    "google-chrome --user-data-dir=/tmp/p --headless http://x", "google-chrome --version", "firefox --version",
    "xvfb-run -a pytest tests/gui", "Xvfb :99 & export DISPLAY=:99; python3 t.py",
    "dbus-send --system --print-reply --dest=org.freedesktop.DBus / org.freedesktop.DBus.ListNames",
    "busctl --system list", "gdbus introspect --system --dest org.freedesktop.systemd1 --object-path /org/freedesktop/systemd1",
    "echo \"DISPLAY is not set\"", "env | grep -i display", "printenv HOME", "export PATH=$PATH:/opt/x",
    "cd ${HOME}/projects && ls", "ls ${HOME}/xdg-open-notes", "for f in *.py; do python3 $f; done",
    "tail -f /var/log/syslog", "kill 1234", "pkill -f myserver", "sed -i 's/a/b/' f.txt", "awk '{print $1}' f",
    "ffmpeg -i a.mp4 b.gif", "convert a.png -resize 50% b.png", "sleep 1", "true", "tar czf a.tgz dir/", "unset DISPLAY",
    "which firefox", "command -v xdg-open", "man xdg-open", "apt list --installed | grep gnome",
    "python3 -c \"print('x' * 3)\"", "bash script.sh", "sh -c 'echo ok'", "time ls", "nohup python3 server.py &",
    "git diff --stat", "cat /etc/os-release", "systemd-analyze", "loginctl list-sessions",
]


class Decisions(unittest.TestCase):
    def test_blocked(self):
        missed = [c for c in BLOCKED if not guard.check_command(c).deny]
        self.assertEqual(missed, [], f"not blocked: {missed}")

    def test_allowed(self):
        wrongly = [(c, guard.check_command(c).reason[:90]) for c in ALLOWED if guard.check_command(c).deny]
        self.assertEqual(wrongly, [], f"wrongly blocked: {wrongly}")

    def test_the_message_points_to_the_workspace_tools(self):
        r = guard.check_command("xdg-open http://x").reason
        for w in ("launch_browser", "screenshot", "agent-workspaces"):
            self.assertIn(w, r)


class Rewrite(unittest.TestCase):
    def setUp(self):
        self.rt = os.path.join(os.environ.get("TMPDIR", "/tmp"), "awguard-test-rt")
        os.makedirs(self.rt, exist_ok=True)

    def test_prelude_makes_the_display_unreachable_and_keeps_the_command_verbatim(self):
        cmd = "echo hi && cd /tmp\nls"
        new = guard.rewrite(cmd, self.rt)
        self.assertTrue(new.endswith(cmd))
        self.assertIn("WAYLAND_DISPLAY=" + guard.GUARD_DISPLAY_NAME, new)
        self.assertIn("unset DISPLAY XAUTHORITY WAYLAND_SOCKET DBUS_SESSION_BUS_ADDRESS", new)
        self.assertIn(os.path.join(self.rt, "aw-guard"), new)
        self.assertTrue(guard.already_guarded(new))

    def test_guard_dir_is_private_and_has_no_bus_or_wayland_entries(self):
        d = guard.ensure_guard_dir(self.rt)
        self.assertEqual(oct(os.stat(d).st_mode & 0o777), "0o700")
        for n in os.listdir(d):
            self.assertNotIn("wayland", n)
            self.assertNotEqual(n, "bus")

    def test_hooks_output_shapes(self):
        self.assertEqual(guard._claude({"tool_name": "Bash", "tool_input": {"command": "ls"}}), (0, ""))   # allowed: silent
        code, out = guard._claude({"tool_name": "Bash", "tool_input": {"command": "xdg-open x"}})
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(guard._claude({"tool_name": "Read", "tool_input": {}}), (0, ""))
        code, out = guard._hermes({"tool_name": "terminal", "tool_input": {"command": "ls"}})
        self.assertEqual(json.loads(out)["action"], "modify")
        code, out = guard._hermes({"tool_name": "terminal", "tool_input": {"command": "xeyes; xdotool key a"}})
        self.assertEqual(json.loads(out)["action"], "block")
        self.assertEqual(guard._hermes({"tool_name": "web_search", "tool_input": {}}), (0, ""))
        for t in ("browser_exec", "browser_cdp", "computer_use"):
            code, out = guard._hermes({"tool_name": t, "tool_input": {}})
            self.assertEqual(json.loads(out)["action"], "block", t)

    def test_idempotent(self):
        once = guard.rewrite("ls", self.rt)
        code, out = guard._hermes({"tool_name": "terminal", "tool_input": {"command": once}})
        self.assertEqual(out, "")
        code, out = guard._hermes({"tool_name": "terminal", "tool_input": {"command": guard.rewrite("xdg-open x", self.rt)}})
        self.assertEqual(json.loads(out)["action"], "block")

    def test_internal_error_fails_closed(self):
        import io
        import contextlib
        old = sys.stdin
        sys.stdin = io.StringIO("not json")
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(guard.main(["guard", "claude"]), 2)
        finally:
            sys.stdin = old


if __name__ == "__main__":
    unittest.main()
