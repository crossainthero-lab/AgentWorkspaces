// AgentWorkspaces: optional GNOME Shell navigation.
//
// Ctrl+Alt+Down enters the AgentWorkspaces viewer on its own GNOME workspace; Ctrl+Alt+Up returns to the
// personal workspace you came from. Everything else (Ctrl+Alt+Left/Right, other workspaces) is untouched.
// The AgentWorkspaces service does not depend on this extension.

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as Background from 'resource:///org/gnome/shell/ui/background.js';

const VIEWER_APP_ID = 'dev.agentworkspaces.Viewer';
const VIEWER_TITLE_PREFIX = 'AgentWorkspaces:';
const GNOME_WM = 'org.gnome.desktop.wm.keybindings';
const TAKEOVER_KEYS = ['switch-to-workspace-up', 'switch-to-workspace-down'];
const SLIDE_MS = 250; // same as GNOME Shell's own workspace switch
const CUBE_UUID = 'desktop-cube@schneegans.github.com';
const CUBE_MS = 450; // a 90 degree turn is a longer way than Desktop Cube's 250 ms between neighbouring faces
// Small public interface so the viewer's "Back to desktop" button (and any other program) can do what the
// shortcuts do. Same-user session bus only; it can only switch workspace.
const PUBLIC_NAME = 'dev.agentworkspaces.ShellExtension';
const PUBLIC_PATH = '/dev/agentworkspaces/ShellExtension';
const PUBLIC_IFACE = `
<node>
  <interface name="dev.agentworkspaces.ShellExtension1">
    <method name="Enter"/>
    <method name="Return"/>
  </interface>
</node>`;
const TEST_IFACE = `
<node>
  <interface name="dev.agentworkspaces.ShellExtension.Test">
    <method name="State"><arg type="s" direction="out"/></method>
    <method name="Press"><arg type="as" direction="in"/></method>
    <method name="Enter"/>
    <method name="Return"/>
    <method name="Screenshot"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
    <method name="CloseViewers"/>
    <method name="HideOverview"/>
    <method name="ClearNotifications"/>
    <method name="SetShortcut"><arg type="s" direction="in"/><arg type="as" direction="in"/></method>
    <method name="Click"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  </interface>
</node>`;

// ---- accelerator helpers ---------------------------------------------------------------------

function normalizeAccel(accel) {
    // '<Control><Alt>Down' -> 'alt+ctrl+down'. Enough to compare accelerators from GSettings.
    const mods = new Set();
    let rest = accel.trim();
    for (;;) {
        const m = rest.match(/^<([^>]+)>/);
        if (!m)
            break;
        const name = m[1].toLowerCase();
        const map = {control: 'ctrl', primary: 'ctrl', ctrl: 'ctrl', alt: 'alt', mod1: 'alt', shift: 'shift',
            super: 'super', mod4: 'super', meta: 'super', hyper: 'super'};
        mods.add(map[name] ?? name);
        rest = rest.slice(m[0].length);
    }
    return [...[...mods].sort(), rest.toLowerCase()].join('+');
}

function sameAccel(a, b) {
    return normalizeAccel(a) === normalizeAccel(b);
}

function log(msg) {
    console.log(`[agent-workspaces] ${msg}`);
}

// ---- the extension --------------------------------------------------------------------------

export default class AgentWorkspacesExtension extends Extension {
    enable() {
        this._settings = this.getSettings();
        this._wmSettings = new Gio.Settings({schema_id: GNOME_WM});
        this._agentWs = null;
        this._origin = null;
        this._slide = null;
        this._pendingViewer = false;
        this._lastSamples = [];
        this._boundKeys = new Set();

        // A previous shell may have died while accelerators were removed: put them back first.
        this._restoreBindings('stale backup from a previous session');

        this._applyShortcuts();
        this._handlers = [
            this._settings.connect('changed::enter-shortcut', () => this._applyShortcuts()),
            this._settings.connect('changed::return-shortcut', () => this._applyShortcuts()),
            this._settings.connect('changed::test-hooks', () => this._syncTestHooks()),
        ];
        this._syncTestHooks();
        this._exportPublic(true);
    }

    disable() {
        this._queued = null;
        this._exportPublic(false);
        for (const id of this._handlers ?? [])
            this._settings.disconnect(id);
        this._handlers = null;
        this._unbindShortcuts();
        this._restoreBindings('extension disabled');
        this._cancelSlide();
        this._exportTestHooks(false);
        this._agentWs = null;
        this._origin = null;
        this._settings = null;
        this._wmSettings = null;
    }

    // ---- shortcuts: register ours, take over only the exact GNOME accelerators we replace --------

    _applyShortcuts() {
        this._unbindShortcuts();
        this._restoreBindings('shortcut setting changed');
        const conflicts = this._takeOverAndCollectConflicts();
        this._settings.set_strv('conflicts', conflicts);
        const flags = Meta.KeyBindingFlags.IGNORE_AUTOREPEAT;
        const modes = Shell.ActionMode.NORMAL | Shell.ActionMode.OVERVIEW; // like GNOME's own workspace keys
        const a1 = Main.wm.addKeybinding('enter-shortcut', this._settings, flags, modes, () => this._onEnter());
        const a2 = Main.wm.addKeybinding('return-shortcut', this._settings, flags, modes, () => this._onReturn());
        log(`bound enter=${a1} return=${a2}; conflicts=${JSON.stringify(conflicts)}`);
        this._boundKeys = new Set(['enter-shortcut', 'return-shortcut']);
    }

    _unbindShortcuts() {
        for (const k of this._boundKeys ?? [])
            Main.wm.removeKeybinding(k);
        this._boundKeys = new Set();
    }

    /**
     * Our accelerators may equal accelerators GNOME has bound to switch-to-workspace-up/down (it does by
     * default: Ctrl+Alt+Up/Down). Mutter cannot reliably pick between two bindings with the same
     * accelerator, so exactly those accelerators are removed from the GNOME key, and recorded in our
     * own `backup` setting BEFORE the change so a crash can be repaired. Any other binding that uses our
     * accelerators (another GNOME key, another extension) is never touched; it is reported in `conflicts`.
     */
    _takeOverAndCollectConflicts() {
        const ours = [
            ...this._settings.get_strv('enter-shortcut'),
            ...this._settings.get_strv('return-shortcut'),
        ].filter(a => a);
        const conflicts = [];
        const removed = {};

        for (const key of TAKEOVER_KEYS) {
            const current = this._wmSettings.get_strv(key);
            const hit = current.filter(c => ours.some(o => sameAccel(c, o)));
            if (hit.length) {
                removed[key] = {before: current, removed: hit};
                conflicts.push(`${GNOME_WM}.${key} (taken over while enabled; restored on disable)`);
            }
        }
        if (Object.keys(removed).length) {
            this._settings.set_string('backup', JSON.stringify(removed));  // 1. record
            Gio.Settings.sync();
            for (const [key, v] of Object.entries(removed)) {            // 2. change
                this._wmSettings.set_strv(key, v.before.filter(c => !v.removed.includes(c)));
            }
            Gio.Settings.sync();
        }

        // Report (never modify) everything else that uses one of our accelerators: GNOME's own schemas and the
        // schemas shipped by the other enabled extensions.
        const sources = [Gio.SettingsSchemaSource.get_default()];
        // (by the user's enabled list, not by state: extensions enable one after another at start-up)
        const wanted = new Gio.Settings({schema_id: 'org.gnome.shell'}).get_strv('enabled-extensions');
        for (const ext of Main.extensionManager.getUuids().map(u => Main.extensionManager.lookup(u))) {
            if (!ext || ext.uuid === this.uuid || !wanted.includes(ext.uuid))
                continue;
            try {
                const dir = ext.dir.get_child('schemas');
                if (dir.query_exists(null))
                    sources.push(Gio.SettingsSchemaSource.new_from_directory(dir.get_path(), sources[0], false));
            } catch (e) { /* no schemas */ }
        }
        const seen = new Set();
        for (const source of sources) {
            const ids = source.list_schemas(false)[0] ?? [];
            for (const id of ids) {
                if (id === this._settings.schema_id || seen.has(id))
                    continue;
                const isWm = id === GNOME_WM;
                const relevant = isWm || id.startsWith('org.gnome.shell') || id.startsWith('org.gnome.mutter') ||
                    id.startsWith('org.gnome.settings-daemon.plugins.media-keys') || source !== sources[0];
                if (!relevant)
                    continue;
                seen.add(id);
                let schema, settings;
                try {
                    schema = source.lookup(id, false);
                    settings = new Gio.Settings({settings_schema: schema});
                } catch (e) {
                    continue;
                }
                for (const key of schema.list_keys()) {
                    if (schema.get_key(key).get_value_type().dup_string() !== 'as')
                        continue;
                    if (isWm && TAKEOVER_KEYS.includes(key))
                        continue; // handled above
                    const vals = settings.get_strv(key);
                    if (vals.some(v => v && ours.some(o => sameAccel(v, o))))
                        conflicts.push(`${id}.${key} (not modified)`);
                }
            }
        }
        return conflicts;
    }

    /** Put back what _takeOverAndCollectConflicts removed. Merges into the current value, so a binding the
     *  user changed in the meantime is not overwritten. Idempotent. */
    _restoreBindings(why) {
        let raw = this._settings.get_string('backup');
        if (!raw)
            return;
        try {
            const backup = JSON.parse(raw);
            for (const [key, v] of Object.entries(backup)) {
                if (!TAKEOVER_KEYS.includes(key))
                    continue;
                const cur = this._wmSettings.get_strv(key);
                const merged = [...cur];
                for (const acc of v.removed) {
                    if (!merged.some(c => sameAccel(c, acc)))
                        merged.push(acc);
                }
                // keep the original order when possible
                const ordered = v.before.filter(a => merged.includes(a)).concat(merged.filter(a => !v.before.includes(a)));
                this._wmSettings.set_strv(key, ordered);
            }
            log(`restored GNOME keybindings (${why})`);
        } catch (e) {
            log(`could not restore keybindings: ${e}`);
            return; // keep the backup so it can be retried or restored by hand
        }
        this._settings.set_string('backup', '');
        Gio.Settings.sync();
    }

    // ---- navigation -------------------------------------------------------------------------------

    _wm() {
        return global.workspace_manager;
    }

    _isVertical() {
        return this._wm().layout_rows === -1;
    }

    _wsValid(ws) {
        if (!ws)
            return false;
        const i = ws.index();
        return i >= 0 && i < this._wm().n_workspaces && this._wm().get_workspace_by_index(i) === ws;
    }

    _viewerWindows() {
        return global.get_window_actors().map(a => a.meta_window).filter(w => {
            if (!w || w.is_override_redirect())
                return false;
            const id = w.get_gtk_application_id?.() ?? w.get_wm_class?.();
            return id === VIEWER_APP_ID || w.get_wm_class?.() === VIEWER_APP_ID ||
                (w.get_title?.() ?? '').startsWith(VIEWER_TITLE_PREFIX);
        });
    }

    _passThrough(direction) {
        // The GNOME binding for this accelerator was taken over, so reproduce its behaviour:
        // move to the neighbouring workspace if the current layout has one (V-Shell's vertical layout does).
        const active = this._wm().get_active_workspace();
        const next = active.get_neighbor(direction);
        if (next && next !== active)
            next.activate(global.get_current_time());
    }

    // From the Activities overview (Super): close it first, then travel. The slide needs the normal workspace view,
    // and leaving the shell in the overview of another workspace would be a surprise.
    _afterOverview(fn) {
        if (this._slide) {
            // A turn is still running (the key was pressed again half way): let it finish, then do this; only the
            // last request counts. Cancelling would make the cube jump to its end position.
            this._queued = fn;
            return;
        }
        if (!Main.overview.visible && !Main.overview.animationInProgress) {
            fn();
            return;
        }
        let done = false;
        let hid = 0;
        let timer = 0;
        const run = () => {
            if (done)
                return;
            done = true;
            if (hid)
                Main.overview.disconnect(hid);
            if (timer)
                GLib.source_remove(timer);
            fn();
        };
        hid = Main.overview.connect('hidden', run);
        timer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 900, () => {
            timer = 0;
            run();
            return GLib.SOURCE_REMOVE;
        });
        Main.overview.hide();
    }

    _onEnter() {
        log('enter shortcut fired');
        try {
            this._afterOverview(() => this.enter());
        } catch (e) {
            log(`enter failed: ${e}\n${e.stack}`);
        }
    }

    _onReturn() {
        try {
            this._afterOverview(() => this.leave());
        } catch (e) {
            log(`return failed: ${e}\n${e.stack}`);
        }
    }

    enter() {
        const active = this._wm().get_active_workspace();
        if (this._isVertical() && !(this._agentWs && active.get_neighbor(Meta.MotionDirection.DOWN) === this._agentWs)) {
            // Vertical workspace layout (e.g. V-Shell): Down already means "next workspace".
            this._passThrough(Meta.MotionDirection.DOWN);
            return;
        }
        if (this._agentWs && this._wsValid(this._agentWs) && active === this._agentWs)
            return; // already there
        const ws = this._ensureAgentWorkspace(active);
        if (!ws)
            return;
        this._origin = active;
        this._switchTo(ws, +1);
        if (this._viewerWindows().length === 0)
            this._requestViewer();
    }

    leave() {
        const active = this._wm().get_active_workspace();
        if (!(this._agentWs && this._wsValid(this._agentWs) && active === this._agentWs)) {
            this._passThrough(Meta.MotionDirection.UP);
            return;
        }
        let target = this._wsValid(this._origin) && this._origin !== active ? this._origin : null;
        if (!target) {
            // The workspace we came from is gone (GNOME removes empty workspaces): use the one before this
            // one, else the one after it, else make a fresh one.
            const wm = this._wm();
            const idx = active.index();
            if (idx > 0)
                target = wm.get_workspace_by_index(idx - 1);
            else if (wm.n_workspaces > 1)
                target = wm.get_workspace_by_index(1);
            else
                target = wm.append_new_workspace(false, global.get_current_time());
        }
        if (target && target !== active)
            this._switchTo(target, -1);
    }

    _ensureAgentWorkspace(active) {
        if (this._agentWs && this._wsValid(this._agentWs))
            return this._agentWs;
        this._agentWs = null;
        const viewers = this._viewerWindows();
        if (viewers.length) {
            // A viewer is already open (for example started with `agent-workspaces enter`): adopt its workspace
            // unless it is the one the user is on, which would make that personal workspace the agent one.
            const ws = viewers[0].get_workspace();
            if (ws && ws !== active) {
                this._agentWs = ws;
                return ws;
            }
        }
        if (!this._settings.get_boolean('create-on-demand')) {
            Main.notify('AgentWorkspaces', 'No AgentWorkspaces viewer is open. Turn on "Create the AgentWorkspaces ' +
                'workspace on demand" in the extension preferences, or run: agent-workspaces enter NAME');
            return null;
        }
        const wm = this._wm();
        const last = wm.get_workspace_by_index(wm.n_workspaces - 1);
        const lastIsEmpty = last.list_windows().filter(w => !w.is_on_all_workspaces()).length === 0;
        if (Meta.prefs_get_dynamic_workspaces() && last !== active && lastIsEmpty)
            this._agentWs = last; // the trailing empty workspace GNOME keeps for new windows
        else
            this._agentWs = wm.append_new_workspace(false, global.get_current_time());
        return this._agentWs;
    }

    // ---- the vertical slide -----------------------------------------------------------------------
    // GNOME animates workspace changes along the workspace layout, which is horizontal here (and a cube with
    // desktop-cube). For enter/return we skip that animation and play our own vertical one: a snapshot of the
    // old windows slides out while the live windows slide in from the other side.

    _switchTo(ws, dir) {
        const active = this._wm().get_active_workspace();
        if (!ws || ws === active)
            return;
        this._cancelSlide();
        let snap = null;
        const animate = St.Settings.get().enable_animations && !Main.overview.visible;
        if (!animate) {
            ws.activate(global.get_current_time());
            return;
        }
        const cube = this._cubeEnabled();
        try {
            snap = this._snapshotWindows(cube);
        } catch (e) {
            log(`no snapshot of the old windows: ${e}`);
        }

        // Skip GNOME's own (horizontal / cube) animation for this one switch.
        const ctrl = Main.wm._workspaceAnimation;
        const origAnimate = ctrl?.animateSwitch;
        let patched = false;
        if (ctrl && origAnimate) {
            ctrl.animateSwitch = (from, to, direction, onComplete) => {
                delete ctrl.animateSwitch;
                patched = false;
                onComplete();
            };
            patched = true;
        }
        ws.activate(global.get_current_time());
        if (patched) { // the switch did not go through animateSwitch (e.g. animations blocked)
            delete ctrl.animateSwitch;
        }
        if (cube && snap) {
            try {
                this._playCube(snap, dir);
            } catch (e) {
                log(`cube turn failed, no animation: ${e}`);
                this._slide = null;
                const g = global.window_group;
                g.rotation_angle_x = 0;
                g.translation_z = 0;
                g.pivot_point_z = 0;
                g.set_pivot_point(0, 0);
                snap._bg && this._destroyFace(snap._bg);
                snap.destroy();
            }
        } else {
            this._playSlide(snap, dir);
        }
    }

    _snapshotWindows(withBackground = false) {
        // Clutter clones of the windows currently on screen. A clone keeps painting its source after the
        // switch hides the source window actor, which is what lets the old workspace slide away.
        // withBackground: the face also carries the wallpaper, so that it turns like a cube face.
        const group = global.window_group;
        const snap = new Clutter.Actor({reactive: false});
        let count = 0;
        if (withBackground) {
            snap.set_size(global.stage.width, global.stage.height);
            snap._bg = this._backgroundClone();
            snap.add_child(snap._bg);
            snap._chrome = [];
            for (const a of this._chromeActors()) {
                if (!a.visible)
                    continue;
                const [x, y] = a.get_transformed_position();
                const c = new Clutter.Clone({source: a, reactive: false});
                c.set_position(x, y);
                c.set_size(a.width, a.height);
                snap.add_child(c);
                snap._chrome.push(c);
            }
        }
        for (const a of group.get_children()) {
            if (!a.visible || !a.meta_window)
                continue;
            const clone = new Clutter.Clone({source: a, reactive: false});
            clone.set_position(a.x, a.y);
            clone.set_size(a.width, a.height);
            snap.add_child(clone);
            count++;
        }
        if (!count && !withBackground) {
            snap.destroy();
            return null; // empty workspace: nothing to slide out, the new content alone slides in
        }
        Main.uiGroup.insert_child_above(snap, group);
        return snap;
    }

    _chromeActors() {
        // Your own desktop's furniture: the top bar and the dock. They belong to the personal world, so they turn away
        // with the personal face and are not on the agent's face (the viewer is full screen).
        const out = [Main.layoutManager.panelBox];
        for (const c of Main.uiGroup.get_children()) {
            if (c.constructor.name === 'DashToDock')
                out.push(c);
        }
        return out.filter(a => a);
    }

    _backgroundClone() {
        // A cube face carries its own wallpaper: a fresh BackgroundManager per monitor in a holder the size of the
        // stage, exactly what the shell's own workspace-switch groups do (clones of the live background paint blank).
        const holder = new Meta.BackgroundGroup();
        holder.set_size(global.stage.width, global.stage.height);
        holder._mgrs = [];
        for (const m of Main.layoutManager.monitors) {
            holder._mgrs.push(new Background.BackgroundManager({
                container: holder, monitorIndex: m.index, vignette: false,
            }));
        }
        return holder;
    }

    _destroyFace(f) {
        for (const m of f._mgrs ?? [])
            m.destroy();
        f.destroy();
    }

    _cubeEnabled() {
        // Desktop Cube loaded and active in THIS shell, and not switched off in our own settings.
        try {
            return this._settings.get_boolean('use-cube-transition') &&
                Main.extensionManager.lookup(CUBE_UUID)?.state === 1;
        } catch (e) {
            return false;
        }
    }

    _cubeParallax() {
        // Desktop Cube's own `window-parallax` (how far the cube is pushed back while it turns), read from its
        // settings when they are reachable, else its default.
        try {
            return Main.extensionManager.lookup(CUBE_UUID).stateObj._settings.get_double('window-parallax');
        } catch (e) {
            return 0.5;
        }
    }

    _playSlide(snap, dir) {
        const group = global.window_group;
        const H = global.stage.height;
        const samples = [];
        const sid = group.connect('notify::translation-y', () => samples.push(Math.round(group.translation_y)));
        this._slide = {kind: 'slide', snap, sid, samples};
        group.translation_y = dir * H;
        group.ease({
            translation_y: 0,
            duration: SLIDE_MS,
            mode: Clutter.AnimationMode.EASE_OUT_CUBIC,
            onStopped: () => this._finishSlide(),
        });
        snap?.ease({
            translation_y: -dir * H,
            duration: SLIDE_MS,
            mode: Clutter.AnimationMode.EASE_OUT_CUBIC,
        });
    }

    // The cube-native version, used while Desktop Cube is enabled. The two faces are two faces of a cube that
    // turns about a horizontal axis: the face you leave (a snapshot of its windows plus the wallpaper) and the face you
    // arrive on (the live window group plus a wallpaper clone). Same ingredients as Desktop Cube's own turn: faces
    // rotated about a pivot `centerDepth` behind them (pivot_point_z), the whole cube pushed back while it turns
    // (its `window-parallax`, strongest half way), the shell's own stage perspective, ease-out-cubic.
    _playCube(snap, dir) {
        // The live window group already carries the real wallpaper (the shell keeps its background group inside it), so
        // it is a complete face; the face you leave brings its own wallpaper (see _snapshotWindows).
        const group = global.window_group;
        const H = global.stage.height;
        const D = H / 2;                                     // face to axis: 90 degrees between faces
        const corner = Math.sqrt(D * D + (H / 2) * (H / 2));
        const maxBack = corner - D;                          // keeps the edge that swings towards you on the screen plane
        const backdrop = new St.Widget({reactive: false, style: 'background-color: #0b141b;'});
        backdrop.set_size(global.stage.width, global.stage.height);
        Main.uiGroup.insert_child_below(backdrop, group);
        const orig = {pivot: group.get_pivot_point(), pivotZ: group.pivot_point_z};
        const chrome = this._chromeActors().map(a => ({a, opacity: 255}));
        for (const c of chrome)
            c.a.opacity = 0;                                   // the personal desktop's bar and dock turn with its face
        const faces = [snap, group];
        for (const f of faces) {
            f.set_pivot_point(0.5, 0.5);
            f.pivot_point_z = -D;
        }
        const samples = [];
        const tl = new Clutter.Timeline({actor: global.stage, duration: this._cubeMs ?? CUBE_MS,
            progress_mode: Clutter.AnimationMode.EASE_OUT_CUBIC});
        let nearer = null;
        const apply = p => {
            const back = maxBack * (1 - Math.abs(2 * p - 1));
            // dir +1 (down): the face you leave turns up and away, the new one comes up from below
            snap.rotation_angle_x = dir * 90 * p;
            group.rotation_angle_x = -dir * 90 * (1 - p);
            for (const f of faces)
                f.translation_z = -back;
            // painter's order: the face that is turned further away is painted first
            const oldNearer = p < 0.5;
            if (nearer !== oldNearer) {
                nearer = oldNearer;
                if (oldNearer)
                    Main.uiGroup.set_child_above_sibling(snap, group);
                else
                    Main.uiGroup.set_child_below_sibling(snap, group);
            }
            samples.push(Math.round(group.rotation_angle_x));
        };
        apply(0);
        tl.connect('new-frame', () => apply(tl.get_progress()));
        tl.connect('completed', () => this._finishSlide());
        this._slide = {kind: 'cube', snap, backdrop, tl, orig, samples, chrome};
        tl.start();
    }

    _finishSlide() {
        const s = this._slide;
        if (!s)
            return;
        this._slide = null;
        const group = global.window_group;
        if (s.kind === 'cube') {
            try {
                s.tl.stop();
            } catch (e) { /* already finished */ }
            group.rotation_angle_x = 0;
            group.translation_z = 0;
            group.pivot_point_z = s.orig.pivotZ;
            group.set_pivot_point(s.orig.pivot[0], s.orig.pivot[1]);
            s.backdrop.destroy();
            for (const c of s.chrome) {                          // arriving on a personal workspace: bar and dock fade in
                c.a.remove_all_transitions();
                c.a.ease({opacity: c.opacity, duration: 160, mode: Clutter.AnimationMode.EASE_OUT_QUAD});
            }
            if (s.snap._bg)
                this._destroyFace(s.snap._bg);
            s.snap.destroy();
            this._lastSamples = s.samples;
            this._lastKind = 'cube';
            this._runQueued();
            return;
        }
        group.disconnect(s.sid);
        group.remove_all_transitions();
        group.translation_y = 0;
        if (s.snap) {
            s.snap.remove_all_transitions();
            s.snap.destroy();
        }
        this._lastSamples = s.samples;
        this._lastKind = 'slide';
        this._runQueued();
    }

    _runQueued() {
        const q = this._queued;
        this._queued = null;
        if (q)
            GLib.idle_add(GLib.PRIORITY_DEFAULT, () => {
                q();
                return GLib.SOURCE_REMOVE;
            });
    }

    _cancelSlide() {
        if (this._slide)
            this._finishSlide();
    }

    // ---- asking the AgentWorkspaces service to open the viewer ------------------------------------

    _socketPath() {
        return GLib.build_filenamev([GLib.get_user_runtime_dir(), 'agent-workspaces', 'api.sock']);
    }

    // The extension acts for the person at the keyboard, so it uses the admin credential (a 0600 file in the same
    // runtime directory as the socket, see docs/api.md 'Authentication'). No file: the daemon answers
    // E_UNAUTHENTICATED and the notice below says what to do.
    _adminToken() {
        try {
            const [ok, bytes] = GLib.file_get_contents(GLib.build_filenamev(
                [GLib.get_user_runtime_dir(), 'agent-workspaces', 'admin.token']));
            return ok ? new TextDecoder().decode(bytes).trim() : undefined;
        } catch (e) {
            return undefined;
        }
    }

    _daemonCall(method, params) {
        return new Promise((resolve, reject) => {
            const client = new Gio.SocketClient();
            const cancel = new Gio.Cancellable();
            const timer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 4000, () => {
                cancel.cancel();
                return GLib.SOURCE_REMOVE;
            });
            const done = (fn, v) => {
                GLib.source_remove(timer);
                try {
                    client.close?.();
                } catch (e) { /* ignore */ }
                fn(v);
            };
            const addr = Gio.UnixSocketAddress.new(this._socketPath());
            client.connect_async(addr, cancel, (c, res) => {
                let conn;
                try {
                    conn = client.connect_finish(res);
                } catch (e) {
                    GLib.source_remove(timer);
                    reject(new Error('daemon-unavailable'));
                    return;
                }
                const req = JSON.stringify({v: 2, id: 1, method, params, token: this._adminToken()}) + '\n';
                const out = conn.get_output_stream();
                const input = new Gio.DataInputStream({base_stream: conn.get_input_stream()});
                out.write_all_async(new TextEncoder().encode(req), GLib.PRIORITY_DEFAULT, cancel, (o, r2) => {
                    try {
                        o.write_all_finish(r2);
                    } catch (e) {
                        done(reject, e);
                        return;
                    }
                    input.read_line_async(GLib.PRIORITY_DEFAULT, cancel, (i, r3) => {
                        try {
                            const [line] = i.read_line_finish_utf8(r3);
                            const msg = JSON.parse(line);
                            if (msg.ok)
                                done(resolve, msg.result);
                            else
                                done(reject, new Error(`${msg.error?.code}: ${msg.error?.message}`));
                        } catch (e) {
                            done(reject, e);
                        } finally {
                            conn.close(null);
                        }
                    });
                });
            });
        });
    }

    async _requestViewer() {
        if (this._pendingViewer)
            return;
        this._pendingViewer = true;
        try {
            const {workspaces} = await this._daemonCall('workspace.list', {});
            const running = (workspaces ?? []).filter(w => w.state === 'running');
            if (!running.length) {
                Main.notify('AgentWorkspaces', 'No agent workspace is running. Create one with: agent-workspaces create --name NAME');
                return;
            }
            const env = {};
            for (const k of ['WAYLAND_DISPLAY', 'DISPLAY', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS'])
                if (GLib.getenv(k))
                    env[k] = GLib.getenv(k);
            // no workspace named: the viewer opens on the one you used last (else the most recently active)
            await this._daemonCall('workspace.enter', {display_env: env, environment: this._settings.get_boolean('environment-mode'),
                maximized: !this._settings.get_boolean('environment-mode')});
        } catch (e) {
            if (String(e.message).includes('daemon-unavailable'))
                Main.notify('AgentWorkspaces', 'The AgentWorkspaces service is not running. Start it with: agent-workspaces daemon');
            else
                log(`workspace.enter failed: ${e.message}`);
        } finally {
            this._pendingViewer = false;
        }
    }

    _exportPublic(on) {
        if (!on) {
            this._pubDbus?.unexport();
            this._pubDbus = null;
            if (this._pubOwner) {
                Gio.bus_unown_name(this._pubOwner);
                this._pubOwner = null;
            }
            return;
        }
        if (this._pubDbus)
            return;
        this._pubDbus = Gio.DBusExportedObject.wrapJSObject(PUBLIC_IFACE, this);
        this._pubDbus.export(Gio.DBus.session, PUBLIC_PATH);
        this._pubOwner = Gio.bus_own_name_on_connection(Gio.DBus.session, PUBLIC_NAME,
            Gio.BusNameOwnerFlags.NONE, null, null);
    }

    // ---- test-only D-Bus interface (off unless the `test-hooks` setting is on) --------------------

    _syncTestHooks() {
        this._exportTestHooks(this._settings.get_boolean('test-hooks'));
    }

    _exportTestHooks(on) {
        if (!on) {
            this._testDbus?.unexport();
            this._testDbus = null;
            if (this._testOwner) {
                Gio.bus_unown_name(this._testOwner);
                this._testOwner = null;
            }
            return;
        }
        if (this._testDbus)
            return;
        this._testDbus = Gio.DBusExportedObject.wrapJSObject(TEST_IFACE, this);
        this._testDbus.export(Gio.DBus.session, '/dev/agentworkspaces/ShellExtension');
        this._testOwner = Gio.bus_own_name_on_connection(Gio.DBus.session, 'dev.agentworkspaces.ShellExtension.Test',
            Gio.BusNameOwnerFlags.NONE, null, null);
    }

    // D-Bus: State() -> JSON
    State() {
        const wm = this._wm();
        const wins = global.get_window_actors().map(a => a.meta_window).filter(w => w.get_window_type() === Meta.WindowType.NORMAL)
            .map(w => ({title: w.get_title(), app_id: w.get_gtk_application_id?.() ?? w.get_wm_class(), ws: w.get_workspace()?.index() ?? -1}));
        const keys = {};
        for (const k of ['switch-to-workspace-up', 'switch-to-workspace-down', 'switch-to-workspace-left', 'switch-to-workspace-right'])
            keys[k] = this._wmSettings.get_strv(k);
        return JSON.stringify({
            active: wm.get_active_workspace_index(), n_workspaces: wm.n_workspaces,
            agent: this._agentWs && this._wsValid(this._agentWs) ? this._agentWs.index() : -1,
            origin: this._wsValid(this._origin) ? this._origin.index() : -1,
            layout_rows: wm.layout_rows, windows: wins, keys, backup: this._settings.get_string('backup'),
            conflicts: this._settings.get_strv('conflicts'), slide_samples: this._lastSamples,
            sliding: !!this._slide, daemon_socket: this._socketPath(),
            animations: St.Settings.get().enable_animations, pointer: global.get_pointer().slice(0, 2),
        });
    }

    // D-Bus: Press(['Control_L','Alt_L','Down']): real key events through Mutter's input pipeline of THIS
    // (isolated) shell. Modifiers first, last element is the key.
    Press(names) {
        const seat = Clutter.get_default_backend().get_default_seat();
        const dev = seat.create_virtual_device(Clutter.InputDeviceType.KEYBOARD_DEVICE);
        let t = GLib.get_monotonic_time();
        const codes = names.map(n => Clutter[`KEY_${n}`]);
        for (const c of codes)
            dev.notify_keyval(t++, c, Clutter.KeyState.PRESSED);
        for (const c of [...codes].reverse())
            dev.notify_keyval(t++, c, Clutter.KeyState.RELEASED);
    }

    Enter() {
        this._onEnter();
    }

    Return() {
        this._onReturn();
    }

    // D-Bus: Click(x, y): left click through this isolated shell's own virtual pointer.
    Click(x, y) {
        const seat = Clutter.get_default_backend().get_default_seat();
        const dev = seat.create_virtual_device(Clutter.InputDeviceType.POINTER_DEVICE);
        dev.notify_absolute_motion(GLib.get_monotonic_time(), x, y);
        GLib.timeout_add(GLib.PRIORITY_DEFAULT, 150, () => {
            dev.notify_button(GLib.get_monotonic_time(), Clutter.BUTTON_PRIMARY, Clutter.ButtonState.PRESSED);
            GLib.timeout_add(GLib.PRIORITY_DEFAULT, 80, () => {
                dev.notify_button(GLib.get_monotonic_time(), Clutter.BUTTON_PRIMARY, Clutter.ButtonState.RELEASED);
                return GLib.SOURCE_REMOVE;
            });
            return GLib.SOURCE_REMOVE;
        });
    }

    // D-Bus: SetShortcut(key, accelerators): same write the preferences window makes.
    SetShortcut(key, accels) {
        if (key === 'enter-shortcut' || key === 'return-shortcut')
            this._settings.set_strv(key, accels);
    }

    ClearNotifications() {
        for (const s of Main.messageTray.getSources())
            s.destroy();
    }

    HideOverview() {
        Main.overview.hide();
    }

    CloseViewers() {
        for (const w of this._viewerWindows())
            w.delete(global.get_current_time());
    }

    Screenshot(path) {
        const shot = new Shell.Screenshot();
        const stream = Gio.File.new_for_path(path).replace(null, false, Gio.FileCreateFlags.NONE, null);
        shot.screenshot(false, stream, (s, res) => {
            try {
                s.screenshot_finish(res);
            } catch (e) {
                log(`screenshot failed: ${e}`);
            }
            stream.close(null);
        });
        return path;
    }
}
