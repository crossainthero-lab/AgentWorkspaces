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

const VIEWER_APP_ID = 'dev.agentworkspaces.Viewer';
const VIEWER_TITLE_PREFIX = 'AgentWorkspaces:';
const GNOME_WM = 'org.gnome.desktop.wm.keybindings';
const TAKEOVER_KEYS = ['switch-to-workspace-up', 'switch-to-workspace-down'];
const SLIDE_MS = 250; // same as GNOME Shell's own workspace switch
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
    }

    disable() {
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

    _onEnter() {
        log('enter shortcut fired');
        try {
            this.enter();
        } catch (e) {
            log(`enter failed: ${e}\n${e.stack}`);
        }
    }

    _onReturn() {
        try {
            this.leave();
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
        try {
            snap = this._snapshotWindows();
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
        this._playSlide(snap, dir);
    }

    _snapshotWindows() {
        // Clutter clones of the windows currently on screen. A clone keeps painting its source after the
        // switch hides the source window actor, which is what lets the old workspace slide away.
        const group = global.window_group;
        const snap = new Clutter.Actor({reactive: false});
        let count = 0;
        for (const a of group.get_children()) {
            if (!a.visible || !a.meta_window)
                continue;
            const clone = new Clutter.Clone({source: a, reactive: false});
            clone.set_position(a.x, a.y);
            clone.set_size(a.width, a.height);
            snap.add_child(clone);
            count++;
        }
        if (!count) {
            snap.destroy();
            return null; // empty workspace: nothing to slide out, the new content alone slides in
        }
        Main.uiGroup.insert_child_above(snap, group);
        return snap;
    }

    _playSlide(snap, dir) {
        const group = global.window_group;
        const H = global.stage.height;
        const samples = [];
        const sid = group.connect('notify::translation-y', () => samples.push(Math.round(group.translation_y)));
        this._slide = {snap, sid, samples};
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

    _finishSlide() {
        const s = this._slide;
        if (!s)
            return;
        this._slide = null;
        const group = global.window_group;
        group.disconnect(s.sid);
        group.remove_all_transitions();
        group.translation_y = 0;
        if (s.snap) {
            s.snap.remove_all_transitions();
            s.snap.destroy();
        }
        this._lastSamples = s.samples;
    }

    _cancelSlide() {
        if (this._slide)
            this._finishSlide();
    }

    // ---- asking the AgentWorkspaces service to open the viewer ------------------------------------

    _socketPath() {
        return GLib.build_filenamev([GLib.get_user_runtime_dir(), 'agent-workspaces', 'api.sock']);
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
                const req = JSON.stringify({v: 1, id: 1, method, params}) + '\n';
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
            running.sort((a, b) => (b.activity?.last_action_at ?? 0) - (a.activity?.last_action_at ?? 0));
            const env = {};
            for (const k of ['WAYLAND_DISPLAY', 'DISPLAY', 'XDG_RUNTIME_DIR'])
                if (GLib.getenv(k))
                    env[k] = GLib.getenv(k);
            await this._daemonCall('workspace.enter', {workspace: running[0].name, display_env: env});
        } catch (e) {
            if (String(e.message).includes('daemon-unavailable'))
                Main.notify('AgentWorkspaces', 'The AgentWorkspaces service is not running. Start it with: agent-workspaces daemon');
            else
                log(`workspace.enter failed: ${e.message}`);
        } finally {
            this._pendingViewer = false;
        }
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
