"""AgentWorkspaces viewer: a normal GTK4 window on YOUR desktop that shows one workspace and, when
you take over, forwards your mouse and keyboard to it.

It is an ordinary Wayland client of the user's desktop (no GNOME Shell extension needed). It reads
the workspace through wlr-screencopy (damage-driven, so an idle workspace costs ~0 CPU) and sends
input through the workspace compositor's own virtual-pointer / virtual-keyboard. Closing the viewer
never affects the workspace or the agent.

Usage: python3 -m aw.viewer NAME [--takeover]
"""
from __future__ import annotations

import os
import sys
import threading
import time
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from . import paths  # noqa: E402
from .client import ApiError, Client  # noqa: E402
from .session import SessionError, WorkspaceSession  # noqa: E402

APP_ID = "dev.agentworkspaces.Viewer"
FORMATS = {
    0: Gdk.MemoryFormat.B8G8R8A8, 1: Gdk.MemoryFormat.B8G8R8X8,   # argb8888 / xrgb8888
    0x34324241: Gdk.MemoryFormat.R8G8B8A8, 0x34324258: Gdk.MemoryFormat.R8G8B8X8,  # AB24 / XB24
    0x34324742: Gdk.MemoryFormat.R8G8B8,                                          # BG24: memory R,G,B
    0x34324752: Gdk.MemoryFormat.B8G8R8,                                          # RG24: memory B,G,R
}
GTK_MODS = ((Gdk.ModifierType.SHIFT_MASK, 1), (Gdk.ModifierType.LOCK_MASK, 2),
            (Gdk.ModifierType.CONTROL_MASK, 4), (Gdk.ModifierType.ALT_MASK, 8),
            (Gdk.ModifierType.SUPER_MASK, 64))
METRICS = os.environ.get("AW_VIEWER_METRICS")


def gtk_to_xkb_mods(state) -> int:
    m = 0
    for g, x in GTK_MODS:
        if state & g:
            m |= x
    return m


class Viewer(Gtk.Application):
    def __init__(self, name: str, takeover: bool):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.ws_name = name
        self.want_takeover = takeover
        self.api: Optional[Client] = None
        self.info = {}
        self.cap: Optional[WorkspaceSession] = None
        self.inp: Optional[WorkspaceSession] = None
        self.running = True
        self.takeover = False
        self.texture_size = (0, 0)
        self.paint_rect = (0, 0, 1, 1)   # x, y, w, h of the picture inside the widget
        self.fit = True
        self.last_clip_out: Optional[str] = None
        self.metrics = []
        self.dbg = []

    # ---- UI
    def do_activate(self):
        win = Gtk.ApplicationWindow(application=self)
        win.set_default_size(1100, 700)
        self.win = win
        hb = Gtk.HeaderBar()
        win.set_titlebar(hb)
        win.set_title(f"AgentWorkspaces: {self.ws_name}")

        self.take_btn = Gtk.ToggleButton(label="Take over")
        self.take_btn.set_tooltip_text("Pause the agent's input and control this workspace yourself")
        self.take_btn.connect("toggled", self.on_takeover)
        hb.pack_start(self.take_btn)

        self.clip_btn = Gtk.MenuButton(icon_name="edit-paste-symbolic")
        self.clip_btn.set_tooltip_text("Clipboard sharing")
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(10)
        self.chk_in = Gtk.CheckButton(label="Paste your clipboard into the workspace")
        self.chk_out = Gtk.CheckButton(label="Copy from the workspace to your clipboard")
        box.append(self.chk_in)
        box.append(self.chk_out)
        pop.set_child(box)
        self.clip_btn.set_popover(pop)
        self.chk_in.connect("toggled", self.on_clip_policy)
        self.chk_out.connect("toggled", self.on_clip_policy)
        hb.pack_end(self.clip_btn)

        self.fit_btn = Gtk.ToggleButton(label="Fit")
        self.fit_btn.set_active(True)
        self.fit_btn.set_tooltip_text("Scale to the window; off = actual pixels")
        self.fit_btn.connect("toggled", self.on_fit)
        hb.pack_end(self.fit_btn)

        fs = Gtk.Button(icon_name="view-fullscreen-symbolic")
        fs.set_tooltip_text("Fullscreen")
        fs.connect("clicked", lambda b: win.set_fullscreened(not win.is_fullscreen()))
        hb.pack_end(fs)

        self.overlay = Gtk.Overlay()
        self.pic = Gtk.Picture()
        self.pic.set_can_shrink(True)
        self.pic.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.pic.set_focusable(True)
        self.pic.set_hexpand(True)
        self.pic.set_vexpand(True)
        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_child(self.pic)
        self.scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.overlay.set_child(self.scroller)
        self.msg = Gtk.Label()
        self.msg.set_visible(False)
        self.msg.add_css_class("title-2")
        self.msg.set_halign(Gtk.Align.CENTER)
        self.msg.set_valign(Gtk.Align.CENTER)
        self.overlay.add_overlay(self.msg)
        win.set_child(self.overlay)

        mc = Gtk.EventControllerMotion()
        mc.connect("motion", self.on_motion)
        self.pic.add_controller(mc)
        g = Gtk.GestureClick()
        g.set_button(0)
        g.connect("pressed", self.on_press)
        g.connect("released", self.on_release)
        self.pic.add_controller(g)
        sc = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.BOTH_AXES |
                                       Gtk.EventControllerScrollFlags.DISCRETE)
        sc.connect("scroll", self.on_scroll)
        self.pic.add_controller(sc)
        kc = Gtk.EventControllerKey()
        kc.connect("key-pressed", self.on_key, True)
        kc.connect("key-released", lambda c, kv, code, st: self.on_key(c, kv, code, st, False))
        win.add_controller(kc)
        win.connect("close-request", self.on_close)
        fc = Gtk.EventControllerFocus()
        fc.connect("enter", self.on_focus_in)
        win.add_controller(fc)
        win.present()
        if not self.connect_backend():
            return
        GLib.timeout_add(500, self.poll_clipboard_out)
        if self.want_takeover:
            self.take_btn.set_active(True)

    def show_message(self, text: Optional[str]):
        self.msg.set_visible(bool(text))
        if text:
            self.msg.set_label(text)

    # ---- backend
    def connect_backend(self) -> bool:
        try:
            self.api = Client(autostart=False, role="admin")
            self.info = self.api.call("workspace.get", workspace=self.ws_name)
            if self.info["state"] != "running":
                self.show_message(f"Workspace is {self.info['state']}")
                return False
            self.api.call("viewer.attach", workspace=self.ws_name)
            self.cap = WorkspaceSession(os.path.join(paths.ws_rt(self.info["id"]), "wayland-0"))
            self.inp = WorkspaceSession(self.cap.socket_path)
        except (ApiError, OSError, SessionError) as e:
            self.show_message(f"Cannot open workspace: {e}")
            return False
        self.sync_from_info()
        threading.Thread(target=self.capture_loop, daemon=True).start()
        threading.Thread(target=self.event_loop, daemon=True).start()
        return True

    def sync_from_info(self):
        mode = self.info.get("input_mode")
        self.take_btn.handler_block_by_func(self.on_takeover)
        self.take_btn.set_active(mode == "manual")
        self.take_btn.handler_unblock_by_func(self.on_takeover)
        self.takeover = mode == "manual"
        cb = self.info.get("clipboard", "isolated")
        for chk, on in ((self.chk_in, cb in ("to-workspace", "both")), (self.chk_out, cb in ("from-workspace", "both"))):
            chk.handler_block_by_func(self.on_clip_policy)
            chk.set_active(on)
            chk.handler_unblock_by_func(self.on_clip_policy)

    def capture_loop(self):
        while self.running:
            try:
                t0 = time.monotonic()
                fr = self.cap.capture_native(wait_damage=True, timeout=2.0, cursor=True)
            except SessionError:
                GLib.idle_add(self.show_message, "Lost connection to the workspace")
                return
            if fr is None:
                if not self.cap.alive:
                    GLib.idle_add(self.show_message, "Workspace stopped")
                    return
                continue
            w, h, stride, fmt, data, yinv = fr
            fm = FORMATS.get(fmt)
            if fm is None:
                continue
            GLib.idle_add(self.set_frame, w, h, stride, fm, data, t0)

    def set_frame(self, w, h, stride, fm, data, t0):
        tex = Gdk.MemoryTexture.new(w, h, fm, GLib.Bytes.new(data), stride)
        self.pic.set_paintable(tex)
        if (w, h) != self.texture_size:
            self.texture_size = (w, h)
            self.apply_fit()
        if METRICS:
            self.metrics.append((time.monotonic() - t0) * 1000)
        return False

    def event_loop(self):
        try:
            c = Client(autostart=False, role="admin")
            for ev in c.events(workspaces=[self.info["id"]]):
                t, d = ev["event"], ev["data"]
                if t == "workspace.input_mode":
                    self.info["input_mode"] = d["input_mode"]
                    GLib.idle_add(self.sync_from_info)
                elif t in ("workspace.state",) and d.get("state") != "running":
                    GLib.idle_add(self.show_message, f"Workspace {d.get('state')}")
                elif t == "workspace.destroyed":
                    GLib.idle_add(self.show_message, "Workspace closed")
                elif t == "workspace.configured":
                    self.info["clipboard"] = d.get("clipboard", "isolated")
                    GLib.idle_add(self.sync_from_info)
        except (ApiError, OSError):
            return

    # ---- controls
    def on_takeover(self, btn):
        try:
            if btn.get_active():
                self.api.call("workspace.pause", workspace=self.ws_name, reason="manual takeover in viewer")
                self.takeover = True
                self.pic.grab_focus()
            else:
                self.api.call("workspace.resume", workspace=self.ws_name)
                self.takeover = False
        except ApiError as e:
            self.show_message(str(e.message))

    def on_fit(self, btn):
        self.fit = btn.get_active()
        self.apply_fit()

    def apply_fit(self):
        w, h = self.texture_size
        if self.fit:
            self.pic.set_content_fit(Gtk.ContentFit.CONTAIN)
            self.pic.set_size_request(-1, -1)
            self.pic.set_can_shrink(True)
        else:
            self.pic.set_content_fit(Gtk.ContentFit.FILL)
            self.pic.set_can_shrink(False)
            self.pic.set_size_request(w, h)

    def on_clip_policy(self, _):
        pol = {(False, False): "isolated", (True, False): "to-workspace", (False, True): "from-workspace",
               (True, True): "both"}[(self.chk_in.get_active(), self.chk_out.get_active())]
        try:
            self.info["clipboard"] = pol
            self.api.call("workspace.configure", workspace=self.ws_name, clipboard=pol)
        except ApiError as e:
            self.show_message(e.message)

    def on_focus_in(self, *_):
        """host -> workspace clipboard, only when the user enabled it, and only when the window is focused."""
        if self.info.get("clipboard") not in ("to-workspace", "both") or not self.inp:
            return
        cb = self.win.get_clipboard()
        cb.read_text_async(None, self._clip_in_done)

    def _clip_in_done(self, cb, res):
        try:
            text = cb.read_text_finish(res)
        except GLib.Error:
            return
        if text and self.inp:
            try:
                self.inp.set_clipboard(text)
            except SessionError:
                pass

    def poll_clipboard_out(self):
        if self.info.get("clipboard") in ("from-workspace", "both") and self.inp and self.inp.alive:
            def work():
                try:
                    t = self.inp.get_clipboard(timeout=0.5)
                except SessionError:
                    return
                if t is not None and t != self.last_clip_out:
                    self.last_clip_out = t
                    GLib.idle_add(lambda: self.win.get_clipboard().set(t) or False)
            threading.Thread(target=work, daemon=True).start()
        return self.running

    # ---- coordinate mapping
    def map_xy(self, x, y):
        tw, th = self.texture_size
        if not tw:
            return None
        ww, wh = self.pic.get_width(), self.pic.get_height()
        if self.fit:
            s = min(ww / tw, wh / th)
            ox, oy = (ww - tw * s) / 2, (wh - th * s) / 2
        else:
            s, ox, oy = 1.0, 0.0, 0.0
        sc = self.info.get("display", {}).get("scale", 1.0) or 1.0
        px, py = (x - ox) / s, (y - oy) / s
        if px < 0 or py < 0 or px >= tw or py >= th:
            return None
        return px / sc, py / sc

    # ---- input (only while taking over)
    def _send(self, fn, *a):
        if not (self.takeover and self.inp):
            return
        threading.Thread(target=self._safe, args=(fn, *a), daemon=True).start()

    def _safe(self, fn, *a):
        try:
            fn(*a)
        except SessionError:
            pass

    def on_motion(self, c, x, y):
        p = self.map_xy(x, y)
        if os.environ.get("AW_VIEWER_DEBUG"):
            self.dbg.append(f"motion {x:.0f},{y:.0f} -> {p} takeover={self.takeover} tex={self.texture_size} "
                            f"pic={self.pic.get_width()}x{self.pic.get_height()}")
        if p:
            self._send(self.inp.pointer_move, *p)

    def on_press(self, g, n, x, y):
        if not self.takeover:
            return
        self.pic.grab_focus()
        p = self.map_xy(x, y)
        if p:
            btn = {1: "left", 2: "middle", 3: "right"}.get(g.get_current_button(), "left")
            self._send(self._press, p, btn, True)

    def on_release(self, g, n, x, y):
        btn = {1: "left", 2: "middle", 3: "right"}.get(g.get_current_button(), "left")
        self._send(self.inp.pointer_button, btn, False)

    def _press(self, p, btn, down):
        self.inp.pointer_move(*p)
        self.inp.pointer_button(btn, down)

    def on_scroll(self, c, dx, dy):
        if self.takeover:
            self._send(self.inp.scroll, dx, dy)
            return True
        return False

    def on_key(self, c, keyval, keycode, state, down):
        if not self.takeover:
            return False
        code = keycode - 8  # GDK hardware keycode (xkb) -> evdev
        if code <= 0:
            return False
        mods = gtk_to_xkb_mods(state)
        self._send(self.inp.raw_key, code, down, mods)
        return True

    def on_close(self, *_):
        self.running = False
        for s in (self.cap, self.inp):
            if s:
                s.close()
        if self.api:
            self.api.close()   # drops the viewer.attach count in the daemon
        if self.dbg:
            print("\n".join(self.dbg), flush=True)
        if METRICS and self.metrics:
            m = sorted(self.metrics)
            print(f"viewer frame handoff ms: n={len(m)} median={m[len(m)//2]:.1f} max={m[-1]:.1f}", flush=True)
        return False


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    takeover = "--takeover" in argv
    names = [a for a in argv if not a.startswith("--")]
    if not names:
        print("usage: python3 -m aw.viewer NAME [--takeover]", file=sys.stderr)
        return 2
    GLib.set_prgname(APP_ID)   # becomes the Wayland app_id, so desktop shells can recognise viewer windows
    return Viewer(names[0], takeover).run([])


if __name__ == "__main__":
    sys.exit(main())
