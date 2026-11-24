"""UNIT tests: per-invocation agent runs never expose the host display and never edit agent config."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from aw import agentrun, apps, integrations  # noqa: E402

HOST = {"PATH": "/usr/bin", "HOME": "/home/x", "DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0", "WAYLAND_SOCKET": "3",
        "XAUTHORITY": "/x", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus", "XDG_RUNTIME_DIR": "/run/user/1000",
        "SSH_AUTH_SOCK": "/s", "ANTHROPIC_API_KEY": "secret-should-not-pass"}


class CleanEnv(unittest.TestCase):
    def test_no_host_display_and_private_runtime_dir(self):
        e = agentrun.clean_env("/tmp/priv-rt", host=HOST)
        for k in ("DISPLAY", "WAYLAND_DISPLAY", "WAYLAND_SOCKET", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS",
                  "SSH_AUTH_SOCK", "ANTHROPIC_API_KEY"):
            self.assertNotIn(k, e)
        self.assertEqual(e["XDG_RUNTIME_DIR"], "/tmp/priv-rt")
        self.assertEqual(e["BROWSER"], "/bin/true")
        self.assertEqual(e["HOME"], "/home/x")

    def test_extra_display_vars_are_refused(self):
        with self.assertRaises(ValueError):
            agentrun.clean_env("/tmp/x", {"DISPLAY": ":1"}, host=HOST)


class Plans(unittest.TestCase):
    def test_claude_plan_is_non_persistent_and_strict(self):
        p = agentrun.plan("claude-code", "open a text editor", base_dir="/b")
        try:
            a = p.argv
            self.assertIn("--strict-mcp-config", a)
            self.assertIn("--mcp-config", a)
            self.assertEqual(a[a.index("--tools") + 1], "")                    # no built-in shell
            self.assertEqual(a[a.index("--model") + 1], "haiku")
            self.assertNotIn("AW_PROJECT", open(a[a.index("--mcp-config") + 1]).read())
            self.assertNotIn("DISPLAY", p.env)
            self.assertNotIn("agent-workspaces", " ".join(a[:3]))              # the prompt is not rewritten
            self.assertEqual(a[2], "open a text editor")
        finally:
            p.close()

    def test_codex_plan_uses_overrides_not_a_config_file(self):
        p = agentrun.plan("codex", "open a browser", base_dir="/b")
        try:
            a = p.argv
            self.assertIn("--ignore-user-config", a)
            self.assertTrue(any(x.startswith("mcp_servers.agent-workspaces.command=") for x in a))
            self.assertEqual(a[a.index("-m") + 1], "gpt-6-luna")
            self.assertEqual(a[-1], "open a browser")
        finally:
            p.close()

    def test_unknown_agent_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            agentrun.plan("hermes", "x")


class Apps(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        def add(name, body):
            open(os.path.join(self.d, name), "w").write("[Desktop Entry]\nType=Application\n" + body)
        add("tor.desktop", "Name=Tor Browser\nExec=tor-launcher %u\nCategories=Network;WebBrowser;\n")
        add("google-chrome.desktop", "Name=Google Chrome\nExec=/usr/bin/google-chrome-stable %U\nCategories=Network;WebBrowser;\n")
        add("org.gnome.TextEditor.desktop", "Name=Text Editor\nExec=gnome-text-editor %U\nCategories=TextEditor;\n")
        add("term.desktop", "Name=Term\nExec=xterm\nTerminal=true\n")
        add("hidden.desktop", "Name=Hidden\nExec=hid\nNoDisplay=true\n")
        add("agentdock.desktop", "Name=AgentDock\nExec=agentdock\nCategories=Utility;\n")

    def test_role_prefers_well_known_default(self):
        found = apps.find_app("browser", apps.list_apps(deny=[], dirs=[self.d]))
        self.assertEqual(found["id"], "google-chrome")
        self.assertEqual(apps.find_app("editor", apps.list_apps(deny=[], dirs=[self.d]))["id"], "org.gnome.TextEditor")

    def test_terminal_and_hidden_entries_and_denylist(self):
        ids = {a["id"] for a in apps.list_apps(deny=["agentdock"], dirs=[self.d])}
        self.assertNotIn("term", ids)
        self.assertNotIn("hidden", ids)
        self.assertNotIn("agentdock", ids)
        self.assertIn("tor", ids)

    def test_field_codes_are_stripped_and_chromium_gets_wayland_flags(self):
        a = apps.find_app("google chrome", apps.list_apps(deny=[], dirs=[self.d]))
        self.assertEqual(a["argv"], ["/usr/bin/google-chrome-stable"])
        self.assertIn("--ozone-platform=wayland", apps.launch_argv(a))


class IntegrateDryRun(unittest.TestCase):
    def test_dry_run_changes_nothing(self):
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, ".claude", "skills"))
        old = os.environ.get("HOME")
        os.environ["HOME"] = home
        try:
            class A:
                agent, action, workspace, dry_run, prompt, model, project = "claude-code", "install", None, True, None, None, None
            self.assertEqual(integrations.run(A()) if integrations.shutil.which("claude") else 0, 0)
            self.assertFalse(os.path.exists(integrations.skill_path("claude-code", home)))
        finally:
            os.environ["HOME"] = old

    def test_skill_text_states_the_rule(self):
        self.assertIn("never the user's personal desktop", integrations.SKILL_MD)
        self.assertIn("automatically", integrations.SKILL_MD)


if __name__ == "__main__":
    unittest.main()
