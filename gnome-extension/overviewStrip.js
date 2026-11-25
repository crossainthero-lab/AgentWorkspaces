// The agent desktop in the Activities overview.
//
// The agent desktop is stacked ABOVE your personal ones, so in the overview it sits above them too: a strip at the very top
// with one small live preview per RUNNING agent workspace (nothing at all when there is none). Click one to go up to it.
//
// Built as its own widget, not by patching GNOME's workspace thumbnails or its controls layout: the strip is an overlay at
// the top of the overview, and the whole of GNOME's controls (search entry, thumbnails, workspaces, dash) is moved down and
// scaled uniformly to make room (the controls lay themselves out against the monitor, so shrinking their box is not an
// option). It therefore works the same with Just Perfection, Blur my Shell, Dash to Dock and Tiling Shell, which change
// what is inside the controls but not how they are placed.
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
const PREVIEW_W = 144;
const PREVIEW_H = 90;
const TILES_H = 124;           // preview + caption + padding
const GAP = 12;                // between the tiles and GNOME's search entry

export class OverviewStrip {
    constructor(ext) {
        this._ext = ext;
        this._tiles = new Map();          // workspace id -> {button, preview, name}
        this._hidden = [];                // undo functions for what was hidden in the overview
        this._open = false;
        this._gen = 0;                    // invalidates in-flight requests when the overview closes
        this._timer = 0;

        this.actor = new St.BoxLayout({
            style_class: 'aw-ov-strip', x_align: Clutter.ActorAlign.FILL, reactive: false,
            visible: false, clip_to_allocation: true,
        });
        this.actor.set_height(0);
        this._row = new St.BoxLayout({style_class: 'aw-ov-row', x_align: Clutter.ActorAlign.CENTER, x_expand: true});
        this.actor.add_child(this._row);
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

    // The agent desktop is a GNOME workspace too (it holds the viewer). In the overview it is already shown above, so its
    // own thumbnail and its peek at the edge of the workspace view are hidden: one place for it, not two.
    _hideAgentWorkspace() {
        const agent = this._ext._agentWs;
        if (!agent || !this._ext._wsValid(agent))
            return;
        try {
            const c = Main.overview._overview.controls;
            for (const t of c._thumbnailsBox?._thumbnails ?? []) {
                if (t.metaWorkspace === agent) {
                    t.hide();
                    this._hidden.push(() => t.show());
                }
            }
            for (const v of c._workspacesDisplay?._workspacesViews ?? []) {
                for (const w of v._workspaces ?? []) {
                    if (w.metaWorkspace === agent) {
                        w.opacity = 0;
                        this._hidden.push(() => (w.opacity = 255));
                    }
                }
            }
        } catch (e) {
            console.log(`[agent-workspaces] overview: cannot hide the agent workspace: ${e}`);
        }
    }

    async _onShowing() {
        // The shell opens in the overview at login: do nothing until it has finished starting up.
        if (Main.layoutManager._startingUp)
            return;
        this._open = true;
        const gen = ++this._gen;
        this._hideAgentWorkspace();
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
        this._build(running);
        // The top bar is drawn over the overview: leave its height free above the tiles.
        const bar = Main.panel.height;
        const mon = Main.layoutManager.primaryMonitor;
        this._row.style = `padding-top: ${bar + 6}px;`;
        this.actor.set_position(mon.x, mon.y);
        this.actor.set_width(mon.width);
        this._controls = Main.overview._overview.controls;
        this._controls.set_pivot_point(0.5, 0);
        const scale = (mon.height - TILES_H - GAP) / mon.height;
        this.actor.visible = true;
        this.actor.remove_all_transitions();
        this.actor.opacity = 0;
        this.actor.set_height(bar + TILES_H);
        this.actor.ease({opacity: 255, duration: 180, mode: Clutter.AnimationMode.EASE_OUT_CUBIC});
        this._controls.ease({scale_x: scale, scale_y: scale, translation_y: TILES_H + GAP, duration: 180,
            mode: Clutter.AnimationMode.EASE_OUT_CUBIC});
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

    _build(workspaces) {
        this._row.destroy_all_children();
        this._tiles.clear();
        for (const w of workspaces) {
            const button = new St.Button({style_class: 'aw-ov-tile', reactive: true, can_focus: true, track_hover: true});
            const col = new St.BoxLayout({vertical: true});
            const preview = new St.Widget({style_class: 'aw-ov-preview', width: PREVIEW_W, height: PREVIEW_H,
                content_gravity: Clutter.ContentGravity.RESIZE_ASPECT});
            const cap = new St.BoxLayout({style_class: 'aw-ov-caption'});
            cap.add_child(new St.Icon({icon_name: 'avatar-default-symbolic', icon_size: 13, style_class: 'aw-ov-icon'}));
            cap.add_child(new St.Label({text: w.name, style_class: 'aw-ov-name', y_align: Clutter.ActorAlign.CENTER}));
            const agent = w.agent?.label ?? '';
            if (agent && !w.name.toLowerCase().includes(agent.toLowerCase()))
                cap.add_child(new St.Label({text: agent, style_class: 'aw-ov-sub', y_align: Clutter.ActorAlign.CENTER}));
            col.add_child(preview);
            col.add_child(cap);
            button.set_child(col);
            button.connect('clicked', () => this._ext._enterFromOverview(w.name));
            this._row.add_child(button);
            this._tiles.set(w.id, {button, preview, name: w.name});
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
