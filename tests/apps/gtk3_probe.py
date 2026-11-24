#!/usr/bin/python3
"""Tiny GTK3 test window (compatibility matrix). Banner, entry, button; every pointer click, key and
text change is appended to a JSON-lines log. Usage: gtk3_probe.py BANNER LOGFILE COLOR"""
import json
import sys
import time

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk  # noqa: E402

banner = sys.argv[1] if len(sys.argv) > 1 else "GTK3"
logpath = sys.argv[2] if len(sys.argv) > 2 else "/dev/null"
color = sys.argv[3] if len(sys.argv) > 3 else "#2b6cb0"


def log(**kw):
    kw["t"] = time.time()
    with open(logpath, "a") as f:
        f.write(json.dumps(kw) + "\n")


win = Gtk.Window(title=f"gtk3 {banner}")
win.set_default_size(640, 400)
css = Gtk.CssProvider()
css.load_from_data(f".banner {{ background: {color}; color: white; font-size: 44px; font-weight: 700; padding: 24px; }}".encode())
Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css, 800)
box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
lab = Gtk.Label(label=banner)
lab.get_style_context().add_class("banner")
box.pack_start(lab, False, False, 0)
entry = Gtk.Entry()
entry.set_placeholder_text("type here")
entry.connect("changed", lambda e: log(ev="text", text=e.get_text()))
box.pack_start(entry, False, False, 0)
n = [0]
btn = Gtk.Button(label="Click me: 0")


def clicked(b):
    n[0] += 1
    b.set_label(f"Click me: {n[0]}")
    log(ev="button", n=n[0])


btn.connect("clicked", clicked)
box.pack_start(btn, False, False, 0)
win.add(box)
win.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.POINTER_MOTION_MASK)
win.connect("button-press-event", lambda w, e: log(ev="press", x=e.x, y=e.y, button=e.button))
win.connect("motion-notify-event", lambda w, e: log(ev="motion", x=e.x, y=e.y))
win.connect("destroy", Gtk.main_quit)
win.show_all()
log(ev="ready", gdk_backend=Gdk.Display.get_default().__gtype__.name)
Gtk.main()
