#!/usr/bin/python3
"""TEST STAND-IN for the AgentWorkspaces viewer: a GTK4 window with the viewer's app id and title scheme.
Only used by run_isolated.py, inside the isolated shell's Wayland display."""
import sys
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib

name = sys.argv[1] if len(sys.argv) > 1 else "demo"
app_id = sys.argv[2] if len(sys.argv) > 2 else "dev.agentworkspaces.Viewer"
title = f"AgentWorkspaces: {name}" if len(sys.argv) <= 2 else name
GLib.set_prgname(app_id)  # GTK4 on Wayland reports the program name as the xdg app_id


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=app_id)

    def do_activate(self):
        w = Gtk.ApplicationWindow(application=self, title=title)
        w.set_default_size(900, 500)
        css = Gtk.CssProvider()
        css.load_from_string("label { font-size: 40px; font-weight: 700; padding: 40px; }")
        Gtk.StyleContext.add_provider_for_display(w.get_display(), css, 800)
        w.set_child(Gtk.Label(label=("stand-in viewer: " if app_id.endswith("Viewer") else "") + name))
        w.present()


App().run([])
