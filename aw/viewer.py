"""AgentWorkspaces viewer: one normal GTK4 window on YOUR desktop for all agent workspaces.

* a sidebar lists the workspaces (agent, what it is doing, who has input, pending requests) and switches
  between them; an overview shows all of them as live thumbnails; one workspace is shown live in the main area
* "Agent | Paused | You" is the one control for who has input: Paused rejects agent input (view only), You
  rejects agent input and forwards your mouse and keyboard; Agent gives control back
* workspaces are created and closed from here; closing this window only detaches: workspaces keep running
* requests that an agent makes of the human (approvals) appear in a bar above the workspace

It is an ordinary Wayland client of the user's desktop (no GNOME Shell extension needed). It reads the workspace
through wlr-screencopy (damage-driven, so an idle workspace costs ~0 CPU) and sends input through the workspace
compositor's own virtual-pointer / virtual-keyboard. Everything else goes through the daemon API (docs/api.md).

Usage: python3 -m aw.viewer [NAME] [--takeover]     (a second call reuses the open window)
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from . import paths  # noqa: E402
from .client import ApiError, Client  # noqa: E402
from .session import SessionError, WorkspaceSession  # noqa: E402

APP_ID = "dev.agentworkspaces.Viewer"
SHELL_EXT_NAME = "dev.agentworkspaces.ShellExtension"      # optional GNOME Shell extension (docs/gnome-extension.md)
SHELL_EXT_PATH = "/dev/agentworkspaces/ShellExtension"
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
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,39}$")
SIZES = [("1280 x 800", 1280, 800), ("1280 x 720", 1280, 720), ("1600 x 900", 1600, 900),
         ("1920 x 1080", 1920, 1080), ("1024 x 768", 1024, 768)]
THUMB_WIDTH = 480
THUMB_EVERY = 1.5            # seconds between thumbnail refreshes, only while the overview is visible
SIDEBAR_W, RAIL_W = 244, 56

CSS = b"""
window.aw-window, .aw-main { background: #000; }
.aw-sidebar { background: #0d0d0d; border-right: 1px solid alpha(white, 0.09); }
.aw-sidebar list, .aw-sidebar scrolledwindow { background: transparent; }
headerbar { background: #0d0d0d; box-shadow: none; border-bottom: 1px solid alpha(white, 0.09); }
headerbar togglebutton:checked { background: alpha(white, 0.2); }
.aw-seg button:checked { background: white; color: black; }
.aw-sidebar row { border-radius: 8px; margin: 1px 6px; padding: 7px 8px; }
.aw-sidebar row:selected { background: alpha(white, 0.13); }
.aw-sidebar row:hover:not(:selected) { background: alpha(white, 0.06); }
.aw-sub { font-size: 0.85em; opacity: 0.62; }
.aw-title { font-weight: 600; }
.aw-badge { background: white; color: black; border-radius: 9px; padding: 0 6px; font-size: 0.8em; font-weight: 700; }
.aw-frame { border: 2px solid transparent; }
.aw-frame.aw-you { border-color: white; }
.aw-frame.aw-paused { border-color: alpha(white, 0.38); }
.aw-card { background: #111; border: 1px solid alpha(white, 0.10); border-radius: 12px; padding: 0; }
.aw-card:hover { border-color: alpha(white, 0.35); }
.aw-thumb { background: #050505; border-radius: 11px 11px 0 0; }
.aw-newcard { border: 1px dashed alpha(white, 0.25); border-radius: 12px; background: transparent; min-height: 150px; }
.aw-newcard:hover { border-color: alpha(white, 0.6); }
.aw-approvals { background: #161616; border-bottom: 1px solid alpha(white, 0.12); padding: 8px 12px; }
.aw-error { color: #ff8a80; }
"""

ACTION_PAST = {"click": "clicked", "move": "moved the pointer", "drag": "dragged", "type": "typed", "key": "pressed keys",
               "scroll": "scrolled", "screenshot": "took a screenshot", "clipboard": "used the clipboard"}
ACTION_NOW = {"click": "clicking", "move": "moving the pointer", "drag": "dragging", "type": "typing",
              "key": "pressing keys", "scroll": "scrolling", "screenshot": "looking", "clipboard": "using the clipboard"}
RECENT_S = 5.0


def gtk_to_xkb_mods(state) -> int:
    m = 0
    for g, x in GTK_MODS:
        if state & g:
            m |= x
    return m


# ---- wording ---------------------------------------------------------------------------------------------------

def _split_action(last: Optional[str]):
    if not last or ":" not in last:
        return None, None
    actor, _, kind = last.partition(":")
    return actor, kind


def _phrase(kind: str, table: Dict[str, str], fallback: str) -> str:
    if kind.startswith("window."):
        return {"close": "closed a window", "focus": "switched window"}.get(kind[7:], "changed a window") \
            if table is ACTION_PAST else "managing windows"
    return table.get(kind, fallback)


def ago(sec: float) -> str:
    if sec < 3:
        return "just now"
    if sec < 60:
        return f"{int(sec)} s ago"
    if sec < 3600:
        return f"{int(sec // 60)} min ago"
    return f"{int(sec // 3600)} h ago"


def activity_text(ws: Dict[str, Any]) -> str:
    """'Claude Code clicked 3 s ago' from the daemon's real activity record."""
    act = ws.get("activity") or {}
    actor, kind = _split_action(act.get("last_action"))
    at = act.get("last_action_at")
    if not actor or not at:
        return "No activity yet"
    who = "You" if actor == "human" else (ws.get("agent", {}).get("label") or "Agent")
    return f"{who} {_phrase(kind, ACTION_PAST, 'did something')} {ago(max(0.0, time.time() - at))}"


def status_text(ws: Dict[str, Any], controlling: bool = False) -> str:
    """Short second line of a sidebar row / card."""
    label = ws.get("agent", {}).get("label") or "Agent"
    if ws.get("state") != "running":
        return f"{label} · {ws.get('state')}"
    if ws.get("input_mode") == "manual":
        return f"{label} · " + ("you are in control" if controlling else "paused")
    act = ws.get("activity") or {}
    actor, kind = _split_action(act.get("last_action"))
    at = act.get("last_action_at")
    if actor == "agent" and at and time.time() - at < RECENT_S:
        return f"{label} · {_phrase(kind, ACTION_NOW, 'working')}"
    return label


def agent_icon(hint: str, size: int = 20) -> Gtk.Image:
    """The consumer owns the icons (docs/api.md): a bundled aw/icons/<hint>.svg, then a theme icon
    agent-workspaces-<hint>, then one neutral glyph. No brand logo is drawn or approximated here."""
    f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons", f"{hint}.svg")
    if os.path.exists(f):
        img = Gtk.Image.new_from_file(f)
    else:
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        specific = f"agent-workspaces-{hint}-symbolic"
        img = Gtk.Image.new_from_icon_name(specific if theme.has_icon(specific) else
                                           ("computer-symbolic" if hint in ("none", "") else "avatar-default-symbolic"))
    img.set_pixel_size(size)
    return img


# ---- approvals: the place where requests for the human are shown --------------------------------------------------
# Every pending approval is rendered by APPROVAL_VIEWS[kind](viewer, approval, workspace_name) -> Gtk.Widget, or by
# default_approval_view. Register a renderer for a new kind (browser profile access, host clipboard, ...) with
# register_approval_view(kind, fn); the widget decides its own buttons and calls viewer.resolve_approval(id, decision).

APPROVAL_VIEWS: Dict[str, Callable[["Viewer", Dict[str, Any], str], Gtk.Widget]] = {}


def register_approval_view(kind: str, fn: Callable[["Viewer", Dict[str, Any], str], Gtk.Widget]):
    APPROVAL_VIEWS[kind] = fn


def default_approval_view(viewer: "Viewer", a: Dict[str, Any], ws_name: str) -> Gtk.Widget:
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
    t = Gtk.Label(label=f"{ws_name}: {a.get('title', 'Agent needs input')}", xalign=0, wrap=True)
    t.add_css_class("aw-title")
    text.append(t)
    if a.get("detail"):
        d = Gtk.Label(label=a["detail"], xalign=0, wrap=True)
        d.add_css_class("aw-sub")
        text.append(d)
    row.append(text)
    deny = Gtk.Button(label="Deny", valign=Gtk.Align.CENTER)
    ok = Gtk.Button(label="Approve", valign=Gtk.Align.CENTER)
    deny.connect("clicked", lambda b: viewer.resolve_approval(a["id"], "denied"))
    ok.connect("clicked", lambda b: viewer.resolve_approval(a["id"], "approved"))
    row.append(deny)
    row.append(ok)
    return row


# ---- live feed of one workspace --------------------------------------------------------------------------------

class Feed:
    """Screen capture + input path to ONE workspace. Closing it detaches this viewer; the workspace and its
    agent are untouched (the daemon only counts viewers)."""

    def __init__(self, viewer: "Viewer", info: Dict[str, Any]):
        self.v = viewer
        self.id, self.name = info["id"], info["name"]
        self.api = Client(autostart=False, role="admin")
        self.api.call("viewer.attach", workspace=self.id)
        sock = os.path.join(paths.ws_rt(self.id), "wayland-0")
        self.cap = WorkspaceSession(sock)
        self.inp = WorkspaceSession(self.cap.socket_path)
        self.running = True
        self.size = (0, 0)
        self.metrics: List[float] = []
        self._latest = None
        self._pending = False
        self._lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while self.running:
            try:
                t0 = time.monotonic()
                fr = self.cap.capture_native(wait_damage=True, timeout=2.0, cursor=True)
            except SessionError:
                if self.running:
                    GLib.idle_add(self.v.feed_lost, self, "Lost connection to the workspace")
                return
            if fr is None:
                if not self.cap.alive:
                    if self.running:
                        GLib.idle_add(self.v.feed_lost, self, "Workspace stopped")
                    return
                continue
            with self._lock:
                self._latest = (fr, t0)
                if self._pending:
                    continue          # newest frame wins; the main loop is busy with an older one
                self._pending = True
            GLib.idle_add(self._deliver)

    def _deliver(self):
        with self._lock:
            item, self._latest, self._pending = self._latest, None, False
        if item and self.running:
            self.v.show_frame(self, *item)
        return False

    def close(self):
        self.running = False
        for s in (self.cap, self.inp):
            try:
                s.close()
            except Exception:
                pass
        self.api.close()      # drops the viewer.attach count in the daemon


# ---- the application ---------------------------------------------------------------------------------------------

class Viewer(Gtk.Application):
    def __init__(self):
        flags = Gio.ApplicationFlags.HANDLES_COMMAND_LINE
        if os.environ.get("AW_VIEWER_NONUNIQUE"):
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id=APP_ID, flags=flags)
        self.ws: Dict[str, Dict[str, Any]] = {}          # id -> workspace summary (+ pending_approvals)
        self.approvals: List[Dict[str, Any]] = []
        self.agent_kinds: Dict[str, Any] = {}
        self.sel: Optional[str] = None                   # selected workspace id
        self.mode = "overview"                           # overview | live
        self.feed: Optional[Feed] = None
        self.controlling = False
        self.fit = True
        self.collapsed = False
        self.win: Optional[Gtk.ApplicationWindow] = None
        self.connected = False
        self.alive = True
        self.shell_ext = False
        self.rows: Dict[str, "Row"] = {}
        self.cards: Dict[str, "Card"] = {}
        self.want_takeover = False
        self.want_name: Optional[str] = None
        self.want_maximized = False
        self.down_keys: set = set()
        self.down_buttons: set = set()
        self._syncing = False
        self._refresh_src = 0
        self._last_frame_at = 0.0
        self.dbg: List[str] = []
        self.load_state()

    # ---- persisted view state
    def _state_path(self) -> str:
        return os.path.join(paths.config_dir(), "viewer.json")

    def load_state(self):
        try:
            d = json.load(open(self._state_path()))
        except (OSError, ValueError):
            d = {}
        self.fit = bool(d.get("fit", True))
        self.collapsed = bool(d.get("sidebar_collapsed", False))
        self.last_name = d.get("last") if isinstance(d.get("last"), str) else None

    def save_state(self):
        sel = self.ws.get(self.sel or "", {})
        try:
            os.makedirs(paths.config_dir(), mode=0o700, exist_ok=True)
            json.dump({"fit": self.fit, "sidebar_collapsed": self.collapsed,
                       "last": sel.get("name") or self.last_name}, open(self._state_path(), "w"))
        except OSError:
            pass

    # ---- command line: NAME [--takeover]; a second invocation lands here in the running instance
    def do_command_line(self, cmdline):
        args = list(cmdline.get_arguments())[1:]
        takeover = "--takeover" in args
        names = [a for a in args if not a.startswith("--")]
        self.want_takeover = takeover
        self.want_maximized = "--maximized" in args
        self.want_name = names[0] if names else None
        self.activate()
        if self.ws and self.want_name:
            self.select_named(self.want_name)
        return 0

    def do_activate(self):
        if self.win:
            self.win.present()
            return
        GLib.set_prgname(APP_ID)
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
        css = Gtk.CssProvider()
        css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.build_window()
        if self.want_maximized:
            self.win.maximize()
        self.win.present()
        Gio.bus_watch_name(Gio.BusType.SESSION, SHELL_EXT_NAME, Gio.BusNameWatcherFlags.NONE,
                           lambda *a: self.set_shell_ext(True), lambda *a: self.set_shell_ext(False))
        threading.Thread(target=self.event_loop, daemon=True).start()
        threading.Thread(target=self.thumb_loop, daemon=True).start()
        GLib.timeout_add_seconds(1, self.tick)
        GLib.timeout_add(500, self.poll_clipboard_out)
        self.apply_collapsed()

    # ---- window ----------------------------------------------------------------------------------------------
    def build_window(self):
        win = Gtk.ApplicationWindow(application=self)
        win.add_css_class("aw-window")
        win.set_default_size(1240, 780)
        win.set_title("AgentWorkspaces")
        self.win = win

        hb = Gtk.HeaderBar()
        win.set_titlebar(hb)
        self.sb_btn = Gtk.ToggleButton(icon_name="sidebar-show-symbolic", tooltip_text="Workspace list")
        self.sb_btn.set_active(not self.collapsed)
        self.sb_btn.connect("toggled", self.on_sidebar_toggle)
        hb.pack_start(self.sb_btn)
        self.ov_btn = Gtk.ToggleButton(icon_name="view-grid-symbolic", tooltip_text="All workspaces")
        self.ov_btn.connect("toggled", self.on_overview_toggle)
        hb.pack_start(self.ov_btn)

        tb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        self.h_title = Gtk.Label(label="Workspaces")
        self.h_title.add_css_class("aw-title")
        self.h_sub = Gtk.Label(label="", ellipsize=Pango.EllipsizeMode.END)
        self.h_sub.add_css_class("aw-sub")
        tb.append(self.h_title)
        tb.append(self.h_sub)
        hb.set_title_widget(tb)

        menu = Gio.Menu()
        view = Gio.Menu()
        view.append("Fit to window", "win.fit")
        view.append("Fullscreen", "win.fullscreen")
        menu.append_section(None, view)
        wsm = Gio.Menu()
        wsm.append("Start workspace", "win.start-ws")
        wsm.append("Stop workspace", "win.stop-ws")
        wsm.append("Close workspace…", "win.close-ws")
        menu.append_section(None, wsm)
        self.menu_btn = Gtk.MenuButton(icon_name="view-more-symbolic", tooltip_text="More")
        self.menu_btn.set_menu_model(menu)
        hb.pack_end(self.menu_btn)
        for name, cb, state in (("fit", self.on_fit_action, self.fit), ("fullscreen", self.on_fullscreen_action, False)):
            act = Gio.SimpleAction.new_stateful(name, None, GLib.Variant.new_boolean(state))
            act.connect("change-state", cb)
            win.add_action(act)
        for name, cb in (("start-ws", self.on_start_ws), ("stop-ws", self.on_stop_ws), ("close-ws", self.on_close_ws)):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            win.add_action(act)

        self.clip_btn = Gtk.MenuButton(icon_name="edit-paste-symbolic", tooltip_text="Clipboard sharing")
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

        # who has input: three mutually exclusive states of one setting
        self.ctl = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.ctl.add_css_class("linked")
        self.ctl.add_css_class("aw-seg")
        self.ctl_btns: Dict[str, Gtk.ToggleButton] = {}
        first = None
        for key, label, tip in (("agent", "Agent", "The agent controls this workspace"),
                                ("paused", "Paused", "Reject the agent's input; you only watch"),
                                ("you", "You", "Reject the agent's input and use your own mouse and keyboard")):
            b = Gtk.ToggleButton(label=label, tooltip_text=tip)
            if first:
                b.set_group(first)
            else:
                first = b
            b.connect("toggled", self.on_control, key)
            self.ctl.append(b)
            self.ctl_btns[key] = b
        hb.pack_end(self.ctl)

        # body
        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        win.set_child(body)
        self.sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.sidebar.add_css_class("aw-sidebar")
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.list.set_activate_on_single_click(True)
        self.list.connect("row-activated", self.on_row_activated)
        sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_child(self.list)
        self.sidebar.append(sc)
        self.new_btn = Gtk.Button(tooltip_text="New workspace")
        self.new_btn.add_css_class("flat")
        self.new_lbl = Gtk.Label(label="New workspace")
        nb = Gtk.Box(spacing=8)
        nb.append(Gtk.Image.new_from_icon_name("list-add-symbolic"))
        nb.append(self.new_lbl)
        self.new_btn.set_child(nb)
        self.new_btn.connect("clicked", lambda b: self.show_create_dialog())
        self.back_btn = Gtk.Button(tooltip_text="Back to your desktop (Ctrl+Alt+Up)", visible=False)
        self.back_btn.add_css_class("flat")
        self.back_lbl = Gtk.Label(label="Back to desktop")
        bb = Gtk.Box(spacing=8)
        bb.append(Gtk.Image.new_from_icon_name("go-up-symbolic"))
        bb.append(self.back_lbl)
        self.back_btn.set_child(bb)
        self.back_btn.connect("clicked", lambda b: self.return_to_desktop())
        for b, l in ((self.new_btn, nb), (self.back_btn, bb)):
            l.set_halign(Gtk.Align.START)
            b.set_margin_start(6)
            b.set_margin_end(6)
        self.back_btn.set_margin_bottom(6)
        self.sidebar.append(self.new_btn)
        self.sidebar.append(self.back_btn)
        body.append(self.sidebar)

        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        main.add_css_class("aw-main")
        body.append(main)
        self.appr_rev = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.appr_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.appr_box.add_css_class("aw-approvals")
        self.appr_rev.set_child(self.appr_box)
        main.append(self.appr_rev)

        self.stack = Gtk.Stack(vexpand=True, hexpand=True)
        main.append(self.stack)
        self.stack.add_named(self.build_overview(), "overview")
        self.stack.add_named(self.build_live(), "live")
        self.stack.add_named(self.build_empty(), "empty")

        win.connect("close-request", self.on_close)
        fc = Gtk.EventControllerFocus()
        fc.connect("enter", self.on_focus_in)
        fc.connect("leave", lambda *_: self.release_all())
        win.add_controller(fc)
        kc = Gtk.EventControllerKey()
        kc.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        kc.connect("key-pressed", self.on_key, True)
        kc.connect("key-released", lambda c, kv, code, st: self.on_key(c, kv, code, st, False))
        win.add_controller(kc)

    def build_overview(self) -> Gtk.Widget:
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, column_spacing=14,
                                row_spacing=14, min_children_per_line=1, max_children_per_line=6,
                                valign=Gtk.Align.START, margin_top=18, margin_bottom=18, margin_start=18, margin_end=18)
        self.flow.connect("child-activated", self.on_card_activated)
        self.new_card = Gtk.Button(hexpand=True)
        self.new_card.add_css_class("aw-newcard")
        nb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        nb.append(Gtk.Image.new_from_icon_name("list-add-symbolic"))
        nb.append(Gtk.Label(label="New workspace"))
        self.new_card.set_child(nb)
        self.new_card.connect("clicked", lambda b: self.show_create_dialog())
        self.flow.append(self.new_card)
        sc = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        sc.set_child(self.flow)
        return sc

    def build_live(self) -> Gtk.Widget:
        self.live_overlay = Gtk.Overlay()
        self.frame = Gtk.Box()
        self.frame.add_css_class("aw-frame")
        self.pic = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True, focusable=True,
                               hexpand=True, vexpand=True)
        self.scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.scroller.set_child(self.pic)
        self.frame.append(self.scroller)
        self.live_overlay.set_child(self.frame)
        self.msg_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER,
                               valign=Gtk.Align.CENTER, visible=False)
        self.msg = Gtk.Label()
        self.msg.add_css_class("title-2")
        self.msg_btn = Gtk.Button(halign=Gtk.Align.CENTER, visible=False)
        self.msg_box.append(self.msg)
        self.msg_box.append(self.msg_btn)
        self.live_overlay.add_overlay(self.msg_box)
        self.msg_btn.connect("clicked", self.on_msg_button)
        self.texture_size = (0, 0)
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
        return self.live_overlay

    def build_empty(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, halign=Gtk.Align.CENTER,
                      valign=Gtk.Align.CENTER)
        self.empty_lbl = Gtk.Label(label="No workspaces yet")
        self.empty_lbl.add_css_class("title-2")
        self.empty_btn = Gtk.Button(label="Create a workspace", halign=Gtk.Align.CENTER)
        self.empty_btn.connect("clicked", lambda b: self.show_create_dialog())
        box.append(self.empty_lbl)
        box.append(self.empty_btn)
        return box

    # ---- optional GNOME Shell extension ------------------------------------------------------------------
    def set_shell_ext(self, present: bool):
        self.shell_ext = present
        self.back_btn.set_visible(present)

    def return_to_desktop(self):
        """Ask the optional extension to slide back to the personal workspace (same as Ctrl+Alt+Up)."""
        try:
            conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            conn.call(SHELL_EXT_NAME, SHELL_EXT_PATH, SHELL_EXT_NAME + "1", "Return", None, None,
                      Gio.DBusCallFlags.NONE, 2000, None, None)
        except GLib.Error as e:
            self.flash(f"Could not reach the desktop extension: {e.message}")

    # ---- sidebar / overview ----------------------------------------------------------------------------------
    def on_sidebar_toggle(self, btn):
        self.collapsed = not btn.get_active()
        self.apply_collapsed()
        self.save_state()

    def apply_collapsed(self):
        self.sidebar.set_size_request(RAIL_W if self.collapsed else SIDEBAR_W, -1)
        for r in self.rows.values():
            r.set_collapsed(self.collapsed)
        self.new_lbl.set_visible(not self.collapsed)
        self.back_lbl.set_visible(not self.collapsed)

    def on_row_activated(self, lb, row):
        self.select(row.ws_id)

    def on_card_activated(self, fb, child):
        if child.get_child() is self.new_card:
            return
        self.select(child.ws_id)

    def on_overview_toggle(self, btn):
        if self._syncing:
            return
        if btn.get_active():
            self.mode = "overview"
        elif self.sel:
            self.mode = "live"
        self.update_view()

    # ---- state from the daemon -------------------------------------------------------------------------------
    def rpc(self, method: str, ok: Optional[Callable] = None, err: Optional[Callable] = None, **params):
        """API call in a worker thread (own short connection); callbacks run on the main loop."""
        def work():
            try:
                c = Client(autostart=False, role="admin", timeout=20)
                try:
                    r = c.call(method, **params)
                finally:
                    c.close()
                GLib.idle_add(self._done, ok, r)
            except ApiError as e:
                GLib.idle_add(self._done, err or self.flash_error, e)
            except OSError as e:
                GLib.idle_add(self._done, err or self.flash_error, ApiError("E_NO_DAEMON", str(e)))
        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _done(cb, arg):
        if cb:
            cb(arg)
        return False

    def flash_error(self, e):
        self.flash(getattr(e, "message", str(e)))

    def flash(self, text: str):
        self.h_sub.set_text(text)

    def event_loop(self):
        while self.alive:
            c = Client(autostart=False, role="admin")
            try:
                c.subscribe()
                GLib.idle_add(self.set_connected, True)
                self.refresh_now()                    # after subscribing: nothing can be missed in between
                while self.alive:
                    ev = c.next_event()
                    GLib.idle_add(self.on_event, ev)
            except (ApiError, OSError, ValueError):
                pass
            finally:
                c.close()
            if self.alive:
                GLib.idle_add(self.set_connected, False)
                time.sleep(2.0)

    def set_connected(self, ok: bool):
        self.connected = ok
        if not ok:
            self.close_feed()
            self.ws.clear()
            self.sync_widgets()
            self.msg_show("The AgentWorkspaces service is not running", None)
            self.stack.set_visible_child_name("live")
            self.h_sub.set_text("Start it with: agent-workspaces daemon")
        return False

    def refresh_now(self):
        try:
            c = Client(autostart=False, role="admin", timeout=15)
            try:
                if not self.agent_kinds:
                    self.agent_kinds = c.call("hello").get("agent_kinds", {})
                wl = c.call("workspace.list")["workspaces"]
                ap = c.call("approval.list", pending=True)["approvals"]
            finally:
                c.close()
        except (ApiError, OSError):
            return
        GLib.idle_add(self.apply_state, wl, ap)

    def schedule_refresh(self):
        if self._refresh_src:
            return
        def go():
            self._refresh_src = 0
            threading.Thread(target=self.refresh_now, daemon=True).start()
            return False
        self._refresh_src = GLib.timeout_add(120, go)

    def on_event(self, ev):
        t, d = ev.get("event", ""), ev.get("data", {})
        if t == "workspace.activity":
            w = self.ws.get(d.get("id"))
            if w:
                w.setdefault("activity", {}).update(last_action=f"{d.get('actor')}:{d.get('action')}",
                                                    last_action_at=d.get("at"))
                self.refresh_texts()
        elif t.startswith(("workspace.", "approval.", "viewer.")) or t == "input.rejected":
            if t == "workspace.input_mode":
                w = self.ws.get(d.get("id"))
                if w:
                    w["input_mode"] = d.get("input_mode")
                    self.refresh_texts()
                    self.update_controls()
            if t != "input.rejected":
                self.schedule_refresh()
        return False

    def apply_state(self, wl, ap):
        self.connected = True
        self.ws = {w["id"]: w for w in wl}
        self.approvals = ap
        if self.sel not in self.ws:
            self.sel = None
        if not self.sel:
            pick = None
            if self.want_name:
                pick = self.find(self.want_name)
            if not pick and self.last_name:
                pick = self.find(self.last_name)
            if not pick:
                pick = next((w["id"] for w in wl if w["state"] == "running"), None)
            if pick:
                self.select(pick, sync=False)
        self.sync_widgets()
        self.update_view()
        self.sync_feed()
        self.refresh_approvals()
        if self.want_takeover and self.feed and self.sel == self.feed.id:
            self.want_takeover = False
            self.ctl_btns["you"].set_active(True)
        return False

    def find(self, ref: str) -> Optional[str]:
        for w in self.ws.values():
            if ref in (w["id"], w["name"]):
                return w["id"]
        for w in self.ws.values():
            if w["name"].lower() == ref.lower():
                return w["id"]
        return None

    def select_named(self, ref: str):
        i = self.find(ref)
        if i:
            self.select(i)

    def select(self, ws_id: str, sync: bool = True):
        if ws_id != self.sel:
            self.release_all()
            self.controlling = False
            self.pic.set_paintable(None)
            self.texture_size = (0, 0)
        self.sel = ws_id
        self.mode = "live"
        if ws_id in self.ws:
            self.last_name = self.ws[ws_id]["name"]
        self.save_state()
        if sync:
            self.sync_widgets()
            self.update_view()
            self.sync_feed()

    # ---- widgets follow the model ------------------------------------------------------------------------------
    def sync_widgets(self):
        order = list(self.ws.keys())
        for i in list(self.rows):
            if i not in self.ws:
                self.list.remove(self.rows.pop(i))
        for i in list(self.cards):
            if i not in self.ws:
                self.flow.remove(self.cards.pop(i))
        for i in order:
            w = self.ws[i]
            if i not in self.rows:
                r = Row(self, i)
                self.rows[i] = r
                self.list.append(r)
                r.set_collapsed(self.collapsed)
            if i not in self.cards:
                c = Card(self, i)
                self.cards[i] = c
                self.flow.insert(c, len(self.cards) - 1)
            self.rows[i].update(w)
            self.cards[i].update(w)
        if self.sel in self.rows:
            self.list.select_row(self.rows[self.sel])
        else:
            self.list.unselect_all()

    def refresh_texts(self):
        for i, w in self.ws.items():
            if i in self.rows:
                self.rows[i].update(w)
            if i in self.cards:
                self.cards[i].update(w)
        self.update_header()

    def tick(self):
        if self.win and self.win.is_visible():
            self.refresh_texts()
        return self.alive

    def update_view(self):
        self._syncing = True
        if not self.ws:
            self.stack.set_visible_child_name("empty")
            self.ov_btn.set_active(False)
        elif self.mode == "overview" or not self.sel:
            self.stack.set_visible_child_name("overview")
            self.ov_btn.set_active(True)
        else:
            self.stack.set_visible_child_name("live")
            self.ov_btn.set_active(False)
        self._syncing = False
        self.update_header()
        self.update_controls()

    def current(self) -> Optional[Dict[str, Any]]:
        return self.ws.get(self.sel or "")

    def update_header(self):
        w = self.current()
        live = self.stack.get_visible_child_name() == "live" and w
        if live:
            self.h_title.set_text(w["name"])
            self.h_sub.set_text(activity_text(w) if w["state"] == "running"
                                else f"{w['agent']['label']} · {w['state']}")
            self.win.set_title(f"AgentWorkspaces: {w['name']}")
        else:
            self.h_title.set_text("Workspaces")
            self.h_sub.set_text("")
            self.win.set_title("AgentWorkspaces")

    def control_state(self) -> str:
        w = self.current()
        if not w or w.get("input_mode") != "manual":
            return "agent"
        return "you" if self.controlling else "paused"

    def update_controls(self):
        w = self.current()
        live = self.stack.get_visible_child_name() == "live" and w is not None
        running = bool(live and w["state"] == "running")
        self.ctl.set_visible(live)
        self.ctl.set_sensitive(running)
        self.clip_btn.set_visible(live)
        self.menu_btn.set_visible(bool(w))
        for n, enabled in (("start-ws", bool(w and w["state"] in ("stopped", "failed"))),
                           ("stop-ws", bool(w and w["state"] == "running")), ("close-ws", bool(w))):
            self.win.lookup_action(n).set_enabled(enabled)
        if w and w.get("input_mode") != "manual":
            self.controlling = False
        st = self.control_state()
        self._syncing = True
        self.ctl_btns[st].set_active(True)
        self._syncing = False
        for c in ("aw-you", "aw-paused"):
            self.frame.remove_css_class(c)
        if running and st != "agent":
            self.frame.add_css_class("aw-you" if st == "you" else "aw-paused")
        if w:
            cb = w.get("clipboard", "isolated")
            self._syncing = True
            self.chk_in.set_active(cb in ("to-workspace", "both"))
            self.chk_out.set_active(cb in ("from-workspace", "both"))
            self._syncing = False

    # ---- feed management ---------------------------------------------------------------------------------------
    def close_feed(self):
        if self.feed:
            self.release_all()
            f, self.feed = self.feed, None
            f.close()

    def sync_feed(self):
        w = self.current()
        if self.feed and (not w or w["id"] != self.feed.id or w["state"] != "running"):
            self.close_feed()
        if not w:
            self.msg_show(None)
            return
        if w["state"] != "running":
            self.pic.set_paintable(None)
            self.msg_show(f"Workspace is {w['state']}", "Start workspace" if w["state"] in ("stopped", "failed") else None)
            return
        if self.stack.get_visible_child_name() != "live" or self.feed:
            if self.feed:
                self.msg_show(None)
            return
        try:
            self.feed = Feed(self, w)
        except (ApiError, OSError, SessionError) as e:
            self.msg_show(f"Cannot open workspace: {getattr(e, 'message', e)}", "Try again")
            return
        self.msg_show(None)
        if METRICS:
            self.dbg.append(f"renderer={type(self.win.get_native().get_renderer()).__name__}")

    def feed_lost(self, feed: Feed, text: str):
        if feed is not self.feed:
            return False
        self.close_feed()
        self.msg_show(text, "Try again")
        self.schedule_refresh()
        return False

    def msg_show(self, text: Optional[str], button: Optional[str] = None):
        self.msg_box.set_visible(bool(text))
        if text:
            self.msg.set_label(text)
        self.msg_btn.set_visible(bool(button))
        if button:
            self.msg_btn.set_label(button)

    def on_msg_button(self, b):
        w = self.current()
        if not w:
            return
        if w["state"] in ("stopped", "failed"):
            self.rpc("workspace.start", workspace=w["id"])
        else:
            self.sync_feed()

    def show_frame(self, feed: Feed, fr, t0):
        if feed is not self.feed:
            return
        w, h, stride, fmt, data, yinv = fr
        fm = FORMATS.get(fmt)
        if fm is None:
            return
        tex = Gdk.MemoryTexture.new(w, h, fm, GLib.Bytes.new(data), stride)
        self.pic.set_paintable(tex)
        if (w, h) != self.texture_size:
            self.texture_size = (w, h)
            self.apply_fit()
        now = time.monotonic()
        if METRICS:
            self.dbg.append(f"frame {now:.4f} {(now - t0) * 1000:.1f}")

    # ---- thumbnails (overview only) -----------------------------------------------------------------------------
    def thumb_loop(self):
        while self.alive:
            time.sleep(THUMB_EVERY)
            if not (self.win and self.win.is_visible() and self.stack.get_visible_child_name() == "overview"):
                continue
            for i, w in list(self.ws.items()):
                if w.get("state") != "running" or self.stack.get_visible_child_name() != "overview":
                    continue
                try:
                    c = Client(autostart=False, role="admin", timeout=10)
                    try:
                        import base64
                        r = c.call("screen.screenshot", workspace=i, max_width=THUMB_WIDTH, inline=True, quiet=True,
                                   agent_kind="human")
                    finally:
                        c.close()
                    GLib.idle_add(self.set_thumb, i, base64.b64decode(r["base64"]))
                except (ApiError, OSError, ValueError):
                    continue

    def set_thumb(self, ws_id: str, png: bytes):
        card = self.cards.get(ws_id)
        if card:
            try:
                card.pic.set_paintable(Gdk.Texture.new_from_bytes(GLib.Bytes.new(png)))
            except GLib.Error:
                pass
        return False

    # ---- who has input ------------------------------------------------------------------------------------------
    def on_control(self, btn, key):
        if self._syncing or not btn.get_active():
            return
        w = self.current()
        if not w:
            return
        if key == "agent":
            self.release_all()
            self.controlling = False
            w["input_mode"] = "agent"
            self.rpc("workspace.resume", workspace=w["id"])
        elif key == "paused":
            self.release_all()
            self.controlling = False
            w["input_mode"] = "manual"
            self.rpc("workspace.pause", workspace=w["id"], reason="paused in the viewer")
        else:
            self.controlling = True
            w["input_mode"] = "manual"
            self.rpc("workspace.pause", workspace=w["id"], reason="taken over in the viewer")
            self.pic.grab_focus()
        self.update_controls()
        self.refresh_texts()

    # ---- approvals ---------------------------------------------------------------------------------------------
    def refresh_approvals(self):
        child = self.appr_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.appr_box.remove(child)
            child = nxt
        for a in self.approvals:
            w = self.ws.get(a.get("workspace") or a.get("workspace_id", ""))
            name = w["name"] if w else a.get("workspace", "")
            view = APPROVAL_VIEWS.get(a.get("kind", ""), default_approval_view)
            self.appr_box.append(view(self, a, name))
        self.appr_rev.set_reveal_child(bool(self.approvals))

    def resolve_approval(self, approval_id: str, decision: str):
        self.rpc("approval.resolve", id=approval_id, decision=decision, ok=lambda r: self.schedule_refresh())

    # ---- create / close / start / stop ----------------------------------------------------------------------------
    def show_create_dialog(self):
        CreateDialog(self).present()

    def on_start_ws(self, *_):
        w = self.current()
        if w:
            self.rpc("workspace.start", workspace=w["id"])

    def on_stop_ws(self, *_):
        w = self.current()
        if w:
            self.rpc("workspace.stop", workspace=w["id"])

    def on_close_ws(self, *_):
        w = self.current()
        if not w:
            return
        self.close_workspace_dialog(w)

    def close_workspace_dialog(self, w):
        dlg = Gtk.AlertDialog()
        dlg.set_message(f"Close workspace {w['name']}?")
        dlg.set_detail("Its apps quit, the agent loses this screen, and its private files are deleted. "
                       "Closing the viewer window instead only hides workspaces; they keep running.")
        dlg.set_buttons(["Cancel", "Close workspace"])
        dlg.set_cancel_button(0)
        dlg.set_default_button(0)

        def done(d, res):
            try:
                if d.choose_finish(res) == 1:
                    self.rpc("workspace.destroy", workspace=w["id"], ok=lambda r: self.schedule_refresh())
            except GLib.Error:
                pass
        dlg.choose(self.win, None, done)

    # ---- view options ----------------------------------------------------------------------------------------------
    def on_fit_action(self, act, value):
        act.set_state(value)
        self.fit = value.get_boolean()
        self.apply_fit()
        self.save_state()

    def on_fullscreen_action(self, act, value):
        act.set_state(value)
        self.win.set_fullscreened(value.get_boolean())

    def apply_fit(self):
        w, h = self.texture_size
        if self.fit or not w:
            self.pic.set_content_fit(Gtk.ContentFit.CONTAIN)
            self.pic.set_size_request(-1, -1)
            self.pic.set_can_shrink(True)
        else:
            self.pic.set_content_fit(Gtk.ContentFit.FILL)
            self.pic.set_can_shrink(False)
            self.pic.set_size_request(w, h)

    # ---- clipboard (only when the user enabled it) ------------------------------------------------------------------
    def on_clip_policy(self, _):
        w = self.current()
        if self._syncing or not w:
            return
        pol = {(False, False): "isolated", (True, False): "to-workspace", (False, True): "from-workspace",
               (True, True): "both"}[(self.chk_in.get_active(), self.chk_out.get_active())]
        w["clipboard"] = pol
        self.rpc("workspace.configure", workspace=w["id"], clipboard=pol)

    def on_focus_in(self, *_):
        w = self.current()
        if not w or w.get("clipboard") not in ("to-workspace", "both") or not self.feed:
            return
        self.win.get_clipboard().read_text_async(None, self._clip_in_done)

    def _clip_in_done(self, cb, res):
        try:
            text = cb.read_text_finish(res)
        except GLib.Error:
            return
        if text and self.feed:
            try:
                self.feed.inp.set_clipboard(text)
            except SessionError:
                pass

    last_clip_out: Optional[str] = None

    def poll_clipboard_out(self):
        w, f = self.current(), self.feed
        if w and f and f.inp.alive and w.get("clipboard") in ("from-workspace", "both"):
            def work():
                try:
                    t = f.inp.get_clipboard(timeout=0.5)
                except SessionError:
                    return
                if t is not None and t != self.last_clip_out:
                    self.last_clip_out = t
                    GLib.idle_add(lambda: self.win.get_clipboard().set(t) or False)
            threading.Thread(target=work, daemon=True).start()
        return self.alive

    # ---- coordinate mapping ---------------------------------------------------------------------------------------------
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
        sc = (self.current() or {}).get("display", {}).get("scale", 1.0) or 1.0
        px, py = (x - ox) / s, (y - oy) / s
        if px < 0 or py < 0 or px >= tw or py >= th:
            return None
        return px / sc, py / sc

    # ---- input (only while "You" has control) ---------------------------------------------------------------------------------
    def _send(self, fn, *a):
        f = self.feed
        if not (self.control_state() == "you" and f):
            return
        threading.Thread(target=self._safe, args=(fn, f, *a), daemon=True).start()

    @staticmethod
    def _safe(fn, f, *a):
        try:
            fn(f.inp, *a)
        except SessionError:
            pass

    def on_motion(self, c, x, y):
        p = self.map_xy(x, y)
        if p:
            self._send(lambda s, px, py: s.pointer_move(px, py), *p)

    def on_press(self, g, n, x, y):
        if self.control_state() != "you":
            return
        self.pic.grab_focus()
        p = self.map_xy(x, y)
        if p:
            btn = {1: "left", 2: "middle", 3: "right"}.get(g.get_current_button(), "left")
            self.down_buttons.add(btn)
            self._send(lambda s, pp, b: (s.pointer_move(*pp), s.pointer_button(b, True)), p, btn)

    def on_release(self, g, n, x, y):
        btn = {1: "left", 2: "middle", 3: "right"}.get(g.get_current_button(), "left")
        self.down_buttons.discard(btn)
        self._send(lambda s, b: s.pointer_button(b, False), btn)

    def on_scroll(self, c, dx, dy):
        if self.control_state() == "you":
            self._send(lambda s, a, b: s.scroll(a, b), dx, dy)
            return True
        return False

    def on_key(self, c, keyval, keycode, state, down):
        if self.control_state() != "you" or not self.pic.has_focus():
            return False
        code = keycode - 8  # GDK hardware keycode (xkb) -> evdev
        if code <= 0:
            return False
        (self.down_keys.add if down else self.down_keys.discard)(code)
        mods = gtk_to_xkb_mods(state)
        self._send(lambda s, cd, dn, m: s.raw_key(cd, dn, m), code, down, mods)
        return True

    def release_all(self):
        """Never leave a key or button pressed inside the workspace when focus or control moves away."""
        f = self.feed
        if f:
            keys, btns = list(self.down_keys), list(self.down_buttons)
            def work():
                for k in keys:
                    try:
                        f.inp.raw_key(k, False, 0)
                    except SessionError:
                        pass
                for b in btns:
                    try:
                        f.inp.pointer_button(b, False)
                    except SessionError:
                        pass
            if keys or btns:
                threading.Thread(target=work, daemon=True).start()
        self.down_keys.clear()
        self.down_buttons.clear()

    # ---- closing: detach only -------------------------------------------------------------------------------------------
    def on_close(self, *_):
        """Closing the window detaches from every workspace. Workspaces and agents keep running; a workspace left
        in Paused/You stays paused until someone chooses Agent (the sidebar shows it)."""
        self.alive = False
        self.save_state()
        self.close_feed()
        if METRICS and self.dbg:
            print("\n".join(self.dbg), flush=True)
        return False


# ---- widgets -------------------------------------------------------------------------------------------------------------

class Row(Gtk.ListBoxRow):
    def __init__(self, viewer: Viewer, ws_id: str):
        super().__init__()
        self.v, self.ws_id = viewer, ws_id
        box = Gtk.Box(spacing=10)
        self.icon_holder = Gtk.Box()
        self.icon_hint = None
        box.append(self.icon_holder)
        txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        self.name = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.name.add_css_class("aw-title")
        self.sub = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.sub.add_css_class("aw-sub")
        txt.append(self.name)
        txt.append(self.sub)
        self.txt = txt
        box.append(txt)
        self.badge = Gtk.Label(valign=Gtk.Align.CENTER, visible=False)
        self.badge.add_css_class("aw-badge")
        box.append(self.badge)
        self.set_child(box)

    def set_collapsed(self, c: bool):
        self.txt.set_visible(not c)
        self.badge.set_visible(not c and bool(self.badge.get_text()))

    def update(self, w: Dict[str, Any]):
        hint = w["agent"].get("icon_hint", "none")
        if hint != self.icon_hint:
            self.icon_hint = hint
            child = self.icon_holder.get_first_child()
            if child:
                self.icon_holder.remove(child)
            self.icon_holder.append(agent_icon(hint))
        self.name.set_text(w["name"])
        self.sub.set_text(status_text(w, self.v.controlling and self.ws_id == self.v.sel))
        n = w.get("pending_approvals", 0)
        self.badge.set_text(str(n) if n else "")
        self.badge.set_visible(bool(n) and not self.v.collapsed)
        self.set_tooltip_text(f"{w['name']}: {status_text(w, self.v.controlling and self.ws_id == self.v.sel)}")


class Card(Gtk.FlowBoxChild):
    def __init__(self, viewer: Viewer, ws_id: str):
        super().__init__()
        self.v, self.ws_id = viewer, ws_id
        self.add_css_class("aw-card")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        self.pic.set_size_request(240, 150)
        self.pic.add_css_class("aw-thumb")
        box.append(self.pic)
        foot = Gtk.Box(spacing=8, margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
        txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.name = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.name.add_css_class("aw-title")
        self.sub = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.sub.add_css_class("aw-sub")
        txt.append(self.name)
        txt.append(self.sub)
        foot.append(txt)
        self.badge = Gtk.Label(valign=Gtk.Align.CENTER, visible=False)
        self.badge.add_css_class("aw-badge")
        foot.append(self.badge)
        box.append(foot)
        self.set_child(box)

    def update(self, w: Dict[str, Any]):
        self.name.set_text(w["name"])
        self.sub.set_text(status_text(w, self.v.controlling and self.ws_id == self.v.sel))
        n = w.get("pending_approvals", 0)
        self.badge.set_text(str(n) if n else "")
        self.badge.set_visible(bool(n))


class CreateDialog(Gtk.Window):
    def __init__(self, viewer: Viewer):
        super().__init__(transient_for=viewer.win, modal=True, title="New workspace", resizable=False)
        self.v = viewer
        grid = Gtk.Grid(row_spacing=10, column_spacing=14, margin_top=18, margin_bottom=18, margin_start=20,
                        margin_end=20)
        self.set_child(grid)
        self.name = Gtk.Entry(placeholder_text="research", hexpand=True, activates_default=True, max_length=40)
        self.name.connect("changed", self.validate)
        self.kinds = [k for k in (viewer.agent_kinds or {"other": {}}) if k != "none"] + ["none"]
        labels = [(viewer.agent_kinds.get(k, {}).get("label") or k) if k != "none" else "No agent yet" for k in self.kinds]
        self.agent = Gtk.DropDown.new_from_strings(labels)
        self.size = Gtk.DropDown.new_from_strings([s[0] for s in SIZES])
        for r, (lab, w) in enumerate((("Name", self.name), ("Agent", self.agent), ("Screen size", self.size))):
            l = Gtk.Label(label=lab, xalign=1)
            l.add_css_class("aw-sub")
            grid.attach(l, 0, r, 1, 1)
            grid.attach(w, 1, r, 1, 1)
        self.err = Gtk.Label(xalign=0, wrap=True, max_width_chars=36)
        self.err.add_css_class("aw-error")
        grid.attach(self.err, 1, 3, 1, 1)
        bar = Gtk.Box(spacing=8, halign=Gtk.Align.END, margin_top=6)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda b: self.close())
        self.ok = Gtk.Button(label="Create", sensitive=False)
        self.ok.add_css_class("suggested-action")
        self.ok.connect("clicked", self.create)
        bar.append(cancel)
        bar.append(self.ok)
        grid.attach(bar, 0, 4, 2, 1)
        self.set_default_widget(self.ok)
        self.name.grab_focus()

    def validate(self, *_):
        n = self.name.get_text().strip()
        taken = any(w["name"].lower() == n.lower() for w in self.v.ws.values())
        ok = bool(NAME_RE.match(n)) and not taken
        self.ok.set_sensitive(ok)
        self.err.set_text("A workspace with this name exists" if taken else
                          ("Letters, digits, space, . _ - (up to 40)" if n and not NAME_RE.match(n) else ""))

    def create(self, *_):
        _, w, h = SIZES[self.size.get_selected()]
        kind = self.kinds[self.agent.get_selected()]
        name = self.name.get_text().strip()
        self.ok.set_sensitive(False)

        def done(ws):
            self.v.ws[ws["id"]] = ws
            self.v.select(ws["id"])
            self.close()

        def failed(e):
            self.err.set_text(e.message or str(e))
            self.ok.set_sensitive(True)
        self.v.rpc("workspace.create", ok=done, err=failed, name=name, agent_kind=kind, width=w, height=h)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    GLib.set_prgname(APP_ID)   # becomes the Wayland app_id, so desktop shells can recognise viewer windows
    return Viewer().run([sys.argv[0]] + argv)


if __name__ == "__main__":
    sys.exit(main())
