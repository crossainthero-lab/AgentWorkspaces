// The agent desktop in the Activities overview.
//
// The agent desktop is stacked ABOVE your personal ones, so in the overview it is a desktop above them too: one thumbnail per
// RUNNING agent workspace in a row of its own directly above GNOME's workspace thumbnails, built from the same style classes
// (`workspace-thumbnails`, `workspace-thumbnail`, `workspace-thumbnail-indicator`) and the same size as the real thumbnails, so it
// is drawn by the theme exactly like them: same size, corner radius, border and selection ring. Its content is the live agent
// screen. When you are on the agent desktop its ring is shown (and GNOME's own ring, which would sit on the agent
// workspace's hidden slot, is hidden). A click goes up to that desktop.
//
// It is a sibling actor, not a Meta.Workspace: the agent workspace the viewer lives on is already in GNOME's list (it is
// hidden from Ctrl+Alt+Left/Right and from the cube), and its own thumbnail is collapsed to nothing here, so there is one
// thumbnail for it, not two. The overview's controls (search, thumbnails, workspaces, dash) are moved down and scaled
// uniformly to make room (they lay themselves out against the monitor, so shrinking their box is not an option), which
// works the same with Just Perfection, Blur my Shell, Dash to Dock and Tiling Shell.
//
// Cost: while the overview is closed, nothing runs. When it opens, one `workspace.list` call (only if the service is
// already up: this never starts it) and then one low-resolution screenshot per tile every 1.5 s, one at a time, until it
// closes. Screenshots are requested `quiet` so they do not count as agent or human activity.

import Clutter from 'gi://Clutter';
import Cogl from 'gi://Cogl';
import GdkPixbuf from 'gi://GdkPixbuf';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const REFRESH_MS = 1500;
const GAP = 8;                 // between the row and GNOME's search entry
const RING = 3;                // width of the selection ring (workspace-thumbnail-indicator)
const ROW_PAD = 6;             // padding of .workspace-thumbnails

export class OverviewStrip {
    constructor(ext) {
        this._ext = ext;
        this._tiles = new Map();          // workspace id -> {slot, ring, preview, name}
        this._hidden = [];                // undo functions for what was hidden in the overview
        this._open = false;
        this._gen = 0;                    // invalidates in-flight requests when the overview closes
        this._timer = 0;
        this._searchId = 0;
        this._activeId = 0;

        this.actor = new St.Widget({visible: false, reactive: false});
        this.actor.set_height(0);
        // Same classes as GNOME's ThumbnailsBox, so the theme draws it the same way.
        this._row = new St.BoxLayout({style_class: 'workspace-thumbnails', x_expand: true, x_align: Clutter.ActorAlign.CENTER});
        this._outer = new St.BoxLayout();           // as wide as the screen, so the row can be centred like GNOME's
        this._outer.add_child(this._row);
        this.actor.add_child(this._outer);
        Main.layoutManager.overviewGroup.add_child(this.actor);

        this._ids = [
            Main.overview.connect('showing', () => this._onShowing()),
            Main.overview.connect('hidden', () => this._onHidden()),
        ];
        if (Main.overview.visible)
            this._onShowing();       // created while the overview is already opening (first time it opens)
    }

    destroy() {
        for (const id of this._ids)
            Main.overview.disconnect(id);
        this._onHidden();
        this.actor.destroy();
    }

    // ---- open / close --------------------------------------------------------------------------------------

    // The agent workspace is a GNOME workspace too (it holds the viewer). Here it is already a thumbnail above, so its own
    // thumbnail is collapsed to nothing (no gap), GNOME's selection ring (which would sit on that empty slot) is hidden,
    // and, unless you are on it, its peek at the edge of the workspace view is hidden.
    _hideAgentWorkspace() {
        const agent = this._ext._agentWs;
        if (!agent || !this._ext._wsValid(agent))
            return;
        const onAgent = this._ext._agentActive();
        try {
            const c = Main.overview._overview.controls;
            const box = c._thumbnailsBox;
            for (const t of box?._thumbnails ?? []) {
                if (t.metaWorkspace === agent) {
                    const was = t.collapse_fraction;
                    t.collapse_fraction = 1;
                    t.hide();
                    this._hidden.push(() => {
                        t.collapse_fraction = was;
                        t.show();
                    });
                }
            }
            this._hidden.push(() => {
                box?._indicator?.show();
                for (const v of c._workspacesDisplay?._workspacesViews ?? []) {
                    for (const w of v._workspaces ?? [])
                        w.opacity = 255;
                }
            });
            this._applyActive();
            box?.queue_relayout();
        } catch (e) {
            console.log(`[agent-workspaces] overview: cannot hide the agent workspace: ${e}`);
        }
    }

    // Which desktop is current decides: GNOME's own selection ring (hidden while you are on the agent desktop, where it would sit
    // on the agent workspace's empty slot), ours (shown while you are), and whether the workspace view shows the agent
    // desktop's own page (only while you are on it: otherwise it would peek in at the edge).
    _applyActive() {
        const agent = this._ext._agentWs;
        if (!agent || !this._ext._wsValid(agent))
            return;
        const onAgent = this._ext._agentActive();
        try {
            const c = Main.overview._overview.controls;
            const box = c._thumbnailsBox;
            if (box?._indicator)
                box._indicator.visible = !onAgent;
            for (const v of c._workspacesDisplay?._workspacesViews ?? []) {
                for (const w of v._workspaces ?? []) {
                    if (w.metaWorkspace === agent)
                        w.opacity = onAgent ? 255 : 0;
                }
            }
        } catch (e) {
            console.log(`[agent-workspaces] overview: ${e}`);
        }
        const current = onAgent ? this._ext._currentAgentName() : null;
        for (const t of this._tiles.values())
            t.ring.visible = current !== null && (current === t.name || this._tiles.size === 1);
    }

    // The size GNOME's own thumbnails have right now (they exist from the moment the overview starts to show, but get their
    // size at the first layout).
    _thumbSize() {
        const box = Main.overview._overview.controls._thumbnailsBox;
        const wa = Main.layoutManager.getWorkAreaForMonitor(Main.layoutManager.primaryIndex);
        const t = (box?._thumbnails ?? []).find(x => x.visible && x.width > 0);
        const w = t ? t.width : Math.round(wa.width * (box?._maxThumbnailScale ?? 0.05));
        return {w, h: Math.round(w * wa.height / wa.width)};      // the width is settled at once, the height is mid-animation
    }

    async _onShowing() {
        // The shell opens in the overview at login: do nothing until it has finished starting up.
        if (Main.layoutManager._startingUp)
            return;
        this._open = true;
        const gen = ++this._gen;
        this._hideAgentWorkspace();
        if (!this._activeId) {
            this._activeId = global.workspace_manager.connect('active-workspace-changed', () => this._applyActive());
        }
        if (!this._ext._daemonAlive())
            return;                        // no service: nothing extra, and the overview must not start it
        let list;
        try {
            list = (await this._ext._daemonCall('workspace.list', {})).workspaces ?? [];
        } catch (e) {
            return;
        }
        if (gen !== this._gen || !this._open)
            return;
        const running = list.filter(w => w.state === 'running');
        if (!running.length)
            return;                        // nothing running: nothing in the overview
        await new Promise(r => GLib.timeout_add(GLib.PRIORITY_DEFAULT, 60, () => {
            r();
            return GLib.SOURCE_REMOVE;
        }));                               // let GNOME lay its thumbnails out first, to read their size
        if (gen !== this._gen || !this._open)
            return;
        const mon = Main.layoutManager.primaryMonitor;
        const real = this._thumbSize();
        // The controls are scaled down to make room, so GNOME's thumbnails will look `sc` times their size: ours match that.
        const bar = Main.panel.height;
        const room0 = real.h + 2 * ROW_PAD + 2 * RING;
        const sc0 = (mon.height - room0 - GAP) / mon.height;
        const thumb = {w: Math.round(real.w * sc0), h: Math.round(real.h * sc0)};
        this._build(running, thumb);
        const room = thumb.h + 2 * ROW_PAD + 2 * RING;
        const sc = (mon.height - room - GAP) / mon.height;
        this._controls = Main.overview._overview.controls;
        this._controls.set_pivot_point(0.5, 0);
        this._outer.style = 'padding-top: ' + (bar + 4) + 'px;';
        this.actor.set_position(mon.x, mon.y);
        this.actor.set_width(mon.width);
        this._outer.set_width(mon.width);
        this.actor.visible = true;
        this.actor.remove_all_transitions();
        this.actor.opacity = 0;
        this.actor.set_height(bar + room);
        this.actor.ease({opacity: 255, duration: 180, mode: Clutter.AnimationMode.EASE_OUT_CUBIC});
        this._controls.ease({scale_x: sc, scale_y: sc, translation_y: room + GAP, duration: 180,
            mode: Clutter.AnimationMode.EASE_OUT_CUBIC});
        // Like GNOME's thumbnails, the row steps aside while you search.
        const entry = Main.overview.searchEntry?.clutter_text;
        if (entry && !this._searchId) {
            this._searchId = entry.connect('text-changed', () =>
                this._row.ease({opacity: entry.get_text() ? 0 : 255, duration: 150, mode: Clutter.AnimationMode.EASE_OUT_QUAD}));
            this._searchEntry = entry;
        }
        this._applyActive();
        this._refresh(gen);
    }

    _onHidden() {
        this._open = false;
        this._gen++;
        for (const undo of this._hidden.splice(0)) {
            try {
                undo();
            } catch (e) { /* the actor is gone */ }
        }
        if (this._timer) {
            GLib.source_remove(this._timer);
            this._timer = 0;
        }
        if (this._searchId) {
            try {
                this._searchEntry.disconnect(this._searchId);
            } catch (e) { /* gone */ }
            this._searchId = 0;
            this._searchEntry = null;
        }
        if (this._activeId) {
            global.workspace_manager.disconnect(this._activeId);
            this._activeId = 0;
        }
        this._row.remove_all_transitions();
        this._row.opacity = 255;
        this.actor.remove_all_transitions();
        this.actor.visible = false;
        this.actor.set_height(0);
        if (this._controls) {
            this._controls.remove_all_transitions();
            this._controls.set_scale(1, 1);
            this._controls.translation_y = 0;
            this._controls = null;
        }
        this._row.destroy_all_children();
        this._tiles.clear();
    }

    // ---- tiles -----------------------------------------------------------------------------------------------

    _build(workspaces, thumb) {
        this._row.destroy_all_children();
        this._tiles.clear();
        for (const w of workspaces) {
            // slot: the thumbnail's own box; the ring is drawn around it, outside, like GNOME's indicator
            const slot = new St.Widget({width: thumb.w, height: thumb.h, reactive: true, track_hover: true});
            const ring = new St.Bin({style_class: 'workspace-thumbnail-indicator', visible: false});
            ring.set_position(-RING, -RING);
            ring.set_size(thumb.w + 2 * RING, thumb.h + 2 * RING);
            const face = new St.Widget({style_class: 'workspace-thumbnail', clip_to_allocation: true, width: thumb.w,
                height: thumb.h, track_hover: true});
            const content = new St.Widget({width: thumb.w, height: thumb.h, content_gravity: Clutter.ContentGravity.RESIZE_ASPECT});
            face.add_child(content);
            slot.add_child(ring);
            slot.add_child(face);
            ring.visible = false;
            const click = new Clutter.ClickGesture();
            click.connect('recognize', () => this._ext._enterFromOverview(w.name));
            slot.add_action(click);
            this._row.add_child(slot);
            this._tiles.set(w.id, {slot, ring, preview: content, name: w.name});
        }
    }

    // ---- live previews: one screenshot at a time, only while the overview is open ----------------------------

    async _refresh(gen) {
        for (const [id, tile] of this._tiles) {
            if (gen !== this._gen || !this._open)
                return;
            try {
                const r = await this._ext._daemonCall('screen.screenshot',
                    {workspace: id, max_width: 320, inline: true, quiet: true});
                if (gen !== this._gen)
                    return;
                this._setImage(tile.preview, GLib.base64_decode(r.base64));
            } catch (e) {
                console.log(`[agent-workspaces] overview preview: ${e}`);     // the preview just stays as it was
            }
        }
        if (gen !== this._gen || !this._open)
            return;
        this._timer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, REFRESH_MS, () => {
            this._timer = 0;
            this._refresh(gen);
            return GLib.SOURCE_REMOVE;
        });
    }

    _setImage(actor, png) {
        const stream = Gio.MemoryInputStream.new_from_bytes(new GLib.Bytes(png));
        const pb = GdkPixbuf.Pixbuf.new_from_stream(stream, null);
        const img = St.ImageContent.new_with_preferred_size(pb.get_width(), pb.get_height());
        const format = pb.get_has_alpha() ? Cogl.PixelFormat.RGBA_8888 : Cogl.PixelFormat.RGB_888;
        const args = [pb.read_pixel_bytes(), format, pb.get_width(), pb.get_height(), pb.get_rowstride()];
        try {
            img.set_bytes(global.stage.context.get_backend().get_cogl_context(), ...args);
        } catch (e) {
            img.set_bytes(...args);                       // older shells take no context
        }
        actor.set_content(img);
    }
}
