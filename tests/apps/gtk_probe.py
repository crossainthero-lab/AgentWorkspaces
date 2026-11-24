#!/usr/bin/python3
"""Tiny GTK4 test window used by the integration tests. Shows a banner (so screenshots from
different workspaces are visibly distinct), an entry and a button, and appends every pointer,
click, scroll and key event it receives to a JSON-lines log file."""
import json, sys, time
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gdk, GLib

banner = sys.argv[1] if len(sys.argv) > 1 else "PROBE"
logpath = sys.argv[2] if len(sys.argv) > 2 else "/dev/null"
color = sys.argv[3] if len(sys.argv) > 3 else "#2b6cb0"


def log(**kw):
    kw["t"] = time.time()
    with open(logpath, "a") as f:
        f.write(json.dumps(kw) + "\n")


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="dev.agentworkspaces.GtkProbe")

    def do_activate(self):
        win = Gtk.ApplicationWindow(application=self, title=f"probe {banner}")
        win.set_default_size(640, 400)
        css = Gtk.CssProvider()
        css.load_from_data(f"""
        .banner {{ background: {color}; color: white; font-size: 44px; font-weight: 700; padding: 24px; }}
        """.encode())
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, 800)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        lab = Gtk.Label(label=banner); lab.add_css_class("banner"); box.append(lab)
        self.pos = Gtk.Label(label="pointer: -"); box.append(self.pos)
        entry = Gtk.Entry(); entry.set_placeholder_text("type here"); box.append(entry)
        self.clicks = 0
        btn = Gtk.Button(label="Click me: 0")

        def on_click(b):
            self.clicks += 1; b.set_label(f"Click me: {self.clicks}"); log(ev="button", n=self.clicks)
        btn.connect("clicked", on_click); box.append(btn)
        win.set_child(box)
        m = Gtk.EventControllerMotion()
        m.connect("motion", lambda c, x, y: (self.pos.set_text(f"pointer: {int(x)},{int(y)}"), log(ev="motion", x=x, y=y)))
        win.add_controller(m)
        k = Gtk.EventControllerKey()
        k.connect("key-pressed", lambda c, kv, kc, st: (log(ev="key", keyval=kv, name=Gdk.keyval_name(kv), state=int(st)), False)[1])
        win.add_controller(k)
        s = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.BOTH_AXES)
        s.connect("scroll", lambda c, dx, dy: (log(ev="scroll", dx=dx, dy=dy), False)[1])
        win.add_controller(s)
        entry.connect("changed", lambda e: log(ev="text", text=e.get_text()))
        win.present()
        log(ev="ready")


App().run([])
