// AgentWorkspaces: optional GNOME Shell navigation.
//
// The agent desktop is ON TOP of your personal ones. Ctrl+Shift+Alt+Up goes up to it (the personal desktop slides down
// and out, the agent desktop slides down into place from above); Ctrl+Shift+Alt+Down comes back to the personal workspace
// you came from. Ctrl+Alt+Left/Right keep walking through your personal workspaces only.
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

import {OverviewStrip} from './overviewStrip.js';

const VIEWER_APP_ID = 'dev.agentworkspaces.Viewer';
const VIEWER_TITLE_PREFIX = 'AgentWorkspaces:';
const GNOME_WM = 'org.gnome.desktop.wm.keybindings';
// GNOME binds Ctrl+Shift+Alt+Up/Down (our defaults) to move-to-workspace-up/down (move the window one workspace up/down); a
// user who sets our shortcuts to Ctrl+Alt+Up/Down collides with switch-to-workspace-up/down. Only an exact match is taken over.
const TAKEOVER_KEYS = ['switch-to-workspace-up', 'switch-to-workspace-down', 'move-to-workspace-up', 'move-to-workspace-down'];
const SLIDE_MS = 250; // same as GNOME Shell's own workspace switch
const VIEWER_WAIT_MS = 1200; // slide up anyway when the viewer window has not appeared by then
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
    // '<Control><Shift><Alt>Down' -> 'alt+ctrl+shift+down'. Enough to compare accelerators from GSettings.
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
        this._entering = null;
        this._ownSwitch = false;
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
        this._patchNeighbors();
        this._patchActivate();
        this._patchKeyHandlers();
        // The overview strip is built only when the overview first opens: nothing exists at login or enable time.
        this._overviewId = Main.overview.connect('showing', () => {
            if (!this._strip && this._settings && !Main.layoutManager._startingUp)
                this._strip = new OverviewStrip(this);
        });
        // Locking the screen ends whatever is animating or queued: nothing may fire behind the lock screen.
        this._sessionId = Main.sessionMode.connect('updated', () => {
            if (Main.sessionMode.isLocked) {
                this._queued = null;
                this._cancelSlide();
            }
        });
    }

    disable() {
        this._queued = null;
        if (this._entering) {
            this._entering.cancelled = true;
            this._entering.abort?.();
            this._entering = null;
        }
        if (this._sessionId) {
            Main.sessionMode.disconnect(this._sessionId);
            this._sessionId = 0;
        }
        if (this._overviewId) {
            Main.overview.disconnect(this._overviewId);
            this._overviewId = 0;
        }
        this._unwatchAgent();
        this._strip?.destroy();
        this._strip = null;
        this._unpatchKeyHandlers();
        this._unpatchActivate();
        this._unpatchNeighbors();
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
        const modes = Shell.ActionMode.NORMAL | Shell.ActionMode.OVERVIEW | Shell.ActionMode.POPUP; // also with a menu open
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
     * Our accelerators may equal accelerators GNOME has bound to move-to-workspace-up/down (it does by default:
     * Ctrl+Shift+Alt+Up/Down move a window one workspace up/down) or switch-to-workspace-up/down (Ctrl+Alt+Up/Down,
     * if you set our shortcuts to those). Ctrl+Alt+Up/Down themselves are NOT touched with the default shortcuts. Mutter cannot reliably pick between two bindings with the same
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

    _isViewerWindow(w) {
        if (!w || w.is_override_redirect())
            return false;
        const id = w.get_gtk_application_id?.() ?? w.get_wm_class?.();
        return id === VIEWER_APP_ID || w.get_wm_class?.() === VIEWER_APP_ID ||
            (w.get_title?.() ?? '').startsWith(VIEWER_TITLE_PREFIX);
    }

    _viewerWindows() {
        return global.get_window_actors().map(a => a.meta_window).filter(w => this._isViewerWindow(w));
    }

    // Ctrl+Alt+Left/Right walk through your PERSONAL workspaces only. The agent desktop is a GNOME workspace too (it holds
    // the viewer), so Meta.Workspace.get_neighbor is wrapped: the agent workspace is skipped in the row (the trailing empty
    // workspace GNOME keeps stays reachable) and you cannot sidestep out of it. Our own switch bypasses the wrapper.
    _patchNeighbors() {
        const proto = Meta.Workspace.prototype;
        const orig = proto.get_neighbor;
        const ext = this;
        this._origNeighbor = orig;
        this._neighborPatch = function (direction) {
            const agent = ext._agentWs;
            if (ext._allowAgentNeighbor || !agent || !ext._wsValid(agent))
                return orig.call(this, direction);
            if (this === agent)
                return this;
            let n = orig.call(this, direction);
            if (n === agent) {
                n = orig.call(agent, direction);
                if (n === agent)
                    n = this;
            }
            return n;
        };
        proto.get_neighbor = this._neighborPatch;
    }

    _unpatchNeighbors() {
        const proto = Meta.Workspace.prototype;
        if (this._origNeighbor && proto.get_neighbor === this._neighborPatch)
            proto.get_neighbor = this._origNeighbor;
        this._origNeighbor = this._neighborPatch = null;
    }

    _agentActive() {
        return !!(this._agentWs && this._wsValid(this._agentWs) && this._wm().get_active_workspace() === this._agentWs);
    }

    // The agent workspace the viewer shows (its title is "AgentWorkspaces: <name>").
    _currentAgentName() {
        for (const w of this._viewerWindows()) {
            const t = w.get_title?.() ?? '';
            if (t.startsWith(VIEWER_TITLE_PREFIX))
                return t.slice(VIEWER_TITLE_PREFIX.length).trim() || null;
        }
        return null;
    }

    // Leaving the agent desktop by any way other than Ctrl+Shift+Alt+Down (a click on one of your desktops in the overview, a
    // window of another desktop being activated) goes through our own, clean exit: no cube turn, no half animation. Our own
    // switches set _ownSwitch and bypass this.
    _patchActivate() {
        const proto = Meta.Workspace.prototype;
        const ext = this;
        this._origActivate = proto.activate;
        this._origActivateFocus = proto.activate_with_focus;
        this._activatePatch = function (time) {
            if (!ext._ownSwitch && ext._agentActive() && this !== ext._agentWs && ext._wsValid(this)) {
                ext._leaveInto(this);
                return;
            }
            ext._origActivate.call(this, time);
        };
        this._activateFocusPatch = function (window, time) {
            if (!ext._ownSwitch && ext._agentActive() && this !== ext._agentWs && ext._wsValid(this)) {
                ext._leaveInto(this);
                GLib.timeout_add(GLib.PRIORITY_DEFAULT, SLIDE_MS + 60, () => {
                    try {
                        window.activate(global.get_current_time());
                    } catch (e) { /* gone */ }
                    return GLib.SOURCE_REMOVE;
                });
                return;
            }
            ext._origActivateFocus.call(this, window, time);
        };
        proto.activate = this._activatePatch;
        if (this._origActivateFocus)
            proto.activate_with_focus = this._activateFocusPatch;
    }

    _unpatchActivate() {
        const proto = Meta.Workspace.prototype;
        if (this._origActivate && proto.activate === this._activatePatch)
            proto.activate = this._origActivate;
        if (this._origActivateFocus && proto.activate_with_focus === this._activateFocusPatch)
            proto.activate_with_focus = this._origActivateFocus;
        this._origActivate = this._origActivateFocus = this._activatePatch = this._activateFocusPatch = null;
    }

    _leaveInto(ws) {
        this._origin = ws;
        this._originFocus = null;
        this.leave();
    }

    // While you are on the agent desktop, GNOME's workspace keys (Ctrl+Alt+Left/Right/Up/Down, Super+Page Up/Down, switch/move to
    // workspace N, last) do nothing at all: no switch, no cube turn, no switcher popup. The one exception: with more than one
    // agent workspace, Left/Right move between agent workspaces only. Everywhere else they are GNOME's own handler, untouched.
    _patchKeyHandlers() {
        const names = [];
        for (const a of ['switch', 'move']) {
            for (const d of ['left', 'right', 'up', 'down', 'last'])
                names.push(`${a}-to-workspace-${d}`);
            for (let i = 1; i <= 12; i++)
                names.push(`${a}-to-workspace-${i}`);
        }
        this._swallowed = names;
        this._origSwitcher = Main.wm._showWorkspaceSwitcher.bind(Main.wm);
        for (const n of names) {
            Main.wm.setCustomKeybindingHandler(n, Shell.ActionMode.NORMAL, (display, window, event, binding) => {
                if (!this._agentActive()) {
                    this._origSwitcher(display, window, event, binding);
                    return;
                }
                const name = binding.get_name();
                if (name === 'switch-to-workspace-left')
                    this._cycleAgent(-1).catch(e => log(`cycle: ${e}`));
                else if (name === 'switch-to-workspace-right')
                    this._cycleAgent(+1).catch(e => log(`cycle: ${e}`));
            });
        }
    }

    _unpatchKeyHandlers() {
        for (const n of this._swallowed ?? [])
            Main.wm.setCustomKeybindingHandler(n, Shell.ActionMode.NORMAL, (...a) => Main.wm._showWorkspaceSwitcher(...a));
        this._swallowed = null;
    }

    // Left/Right on the agent desktop with several agent workspaces: the previous / next one (stops at the ends, like workspaces).
    async _cycleAgent(dir) {
        if (!this._daemonAlive())
            return;
        const {workspaces} = await this._daemonCall('workspace.list', {});
        const running = (workspaces ?? []).filter(w => w.state === 'running');
        if (running.length < 2)
            return;
        const cur = this._currentAgentName();
        let i = running.findIndex(w => w.name === cur);
        if (i < 0)
            i = 0;
        const j = Math.min(running.length - 1, Math.max(0, i + dir));
        if (j !== i)
            await this._requestViewer(running[j].name);
    }

    // Once you are up there, the keyboard belongs to the viewer (so typing and the shortcuts go where you expect).
    _focusViewerSoon() {
        GLib.timeout_add(GLib.PRIORITY_DEFAULT, SLIDE_MS + 40, () => {
            const v = this._viewerWindows().find(w => w.get_workspace() === this._agentWs);
            if (v && this._agentActive() && global.display.focus_window !== v) {
                try {
                    v.activate(global.get_current_time());
                } catch (e) { /* gone */ }
            }
            return GLib.SOURCE_REMOVE;
        });
    }

    _passThrough(direction) {
        // The GNOME binding for this accelerator was taken over, so reproduce its behaviour:
        // move to the neighbouring workspace if the current layout has one (V-Shell's vertical layout does).
        const active = this._wm().get_active_workspace();
        const next = active.get_neighbor(direction);
        if (next && next !== active)
            this._ownSwitch = true;
            try {
                next.activate(global.get_current_time());
            } finally {
                this._ownSwitch = false;
            }
    }

    // A click on an agent desktop's preview in the overview: close the overview, then go up to that workspace.
    _enterFromOverview(name) {
        this._afterOverview(() => this.enter(name));
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

    // name: open that agent workspace (a click in the overview); omitted: where you left off.
    // Ctrl+Shift+Alt+Up ALWAYS goes up to the agent desktop. With nothing running the service is asked for the default one
    // ('agents', no agent assigned yet) first. Never a notification or a dialog: whatever goes wrong is said, quietly,
    // inside the viewer, and details go to the log.
    enter(name = null) {
        const active = this._wm().get_active_workspace();
        if (this._isVertical() && !(this._agentWs && active.get_neighbor(Meta.MotionDirection.UP) === this._agentWs)) {
            // Vertical workspace layout (e.g. V-Shell): Up already means "the workspace above".
            this._passThrough(Meta.MotionDirection.UP);
            return;
        }
        const onAgent = this._agentWs && this._wsValid(this._agentWs) && active === this._agentWs;
        if (onAgent && !name && this._viewerWindows().length > 0)
            return;                                   // already up there
        // Up there with the viewer gone (closed by hand) or a particular workspace wanted: open it again, no slide.
        this._startAgentDesktop(name, !onAgent).catch(e => log(`enter: ${e}\n${e.stack}`));
    }

    /** Make sure the service runs and a workspace exists to look at. Resolves {daemon, name}: daemon false when the
     *  service could not be started (the viewer then says so itself); name is the workspace to open (null: where you
     *  left off). Every failure goes to the log only. */
    async _prepareDesktop(name) {
        if (!this._daemonAlive() && !await this._startDaemon())
            return {daemon: false, name};
        try {
            if (name)
                return {daemon: true, name};
            const {workspaces} = await this._daemonCall('workspace.list', {});
            if ((workspaces ?? []).some(w => w.state === 'running'))
                return {daemon: true, name: null};
            const r = await this._daemonCall('workspace.ensure_default', {}, 15000);
            log(`default agent desktop ${r?.created ? 'created' : 'started'}: ${r?.name}`);
            return {daemon: true, name: r?.name ?? null};
        } catch (e) {
            log(`no workspace to go up to (${e.message}); the viewer will say so`);
            return {daemon: true, name: null};
        }
    }

    async _startAgentDesktop(name, slide) {
        if (this._entering || this._slide)
            return;
        const token = {cancelled: false, abort: null};
        this._entering = token;
        try {
            const haveViewer = slide && this._viewerWindows().length > 0;
            let target = {daemon: true, name};
            if (!haveViewer) {
                // Normally the service answers (and creates the default desktop) well inside VIEWER_WAIT_MS. When it
                // does not, go up anyway: the viewer shows its quiet "starting" state and picks the desktop up when it
                // exists. The preparation carries on in the background.
                const prep = this._prepareDesktop(name);
                const late = new Promise(resolve => {
                    let id = GLib.timeout_add(GLib.PRIORITY_DEFAULT, VIEWER_WAIT_MS, () => {
                        id = 0;
                        resolve(null);
                        return GLib.SOURCE_REMOVE;
                    });
                    const stop = () => {
                        if (id)
                            GLib.source_remove(id);
                        id = 0;
                    };
                    prep.then(stop, stop);
                });
                const first = await Promise.race([prep, late]);
                if (token.cancelled)
                    return;
                if (first) {
                    target = first;
                } else {
                    log('the agent desktop is not ready yet: going up, the viewer shows that it is starting');
                    prep.catch(e => log(`prepare: ${e}`));
                    target = {daemon: this._daemonAlive(), name};
                }
            }
            const active = this._wm().get_active_workspace();
            if (slide && this._agentWs && this._wsValid(this._agentWs) && active === this._agentWs)
                return;
            const ws = this._ensureAgentWorkspace(active);
            if (!ws)
                return;
            if (slide) {
                this._origin = active;
                this._originFocus = global.display.focus_window ?? null;
            }
            if (haveViewer) {
                for (const w of this._viewerWindows()) {      // a viewer left on a personal desktop belongs up here
                    if (w.get_workspace() === active)
                        w.change_workspace(ws);
                }
                this._switchTo(ws, -1);                       // the agent desktop comes down from above
                this._focusViewerSoon();
                // The viewer is already there, so no waiting: whatever is missing (service stopped, desktop stopped
                // or gone) is made good in the background and the viewer follows it. A particular workspace is asked for only
                // now that you are on the agent desktop: asking a window on another desktop to present itself makes GNOME
                // announce that it is "ready".
                if (name)
                    this._requestViewer(name).catch(e => log(`viewer: ${e}`));
                else
                    this._prepareDesktop(null).catch(e => log(`prepare: ${e}`));
                return;
            }
            await this._openViewer(ws, target, slide, token);
        } finally {
            if (this._entering === token)
                this._entering = null;
        }
    }

    // Open the viewer and, when going up, slide as soon as its window has its first frame: the window is sent to the
    // agent desktop the moment it maps, so the slide shows a finished picture, never an empty desktop. If it takes
    // longer than VIEWER_WAIT_MS the slide happens anyway and the viewer's own quiet "starting" state shows.
    _openViewer(ws, target, slide, token) {
        return new Promise(resolve => {
            let finished = false;
            let mapId = 0;
            let timer = 0;
            const finish = doSlide => {
                if (finished)
                    return;
                finished = true;
                token.abort = null;
                if (mapId)
                    global.window_manager.disconnect(mapId);
                if (timer)
                    GLib.source_remove(timer);
                mapId = timer = 0;
                if (doSlide && slide && !token.cancelled && this._wsValid(ws) && this._wm().get_active_workspace() !== ws) {
                    this._switchTo(ws, -1);
                    this._focusViewerSoon();
                }
                resolve();
            };
            token.abort = () => finish(false);
            if (slide) {
                mapId = global.window_manager.connect('map', (_wm, actor) => {
                    const w = actor.meta_window;
                    if (finished || !this._isViewerWindow(w))
                        return;
                    actor.remove_all_transitions();           // no pop-in animation: the slide is the entrance
                    try {
                        if (w.get_workspace() !== ws)
                            w.change_workspace(ws);
                    } catch (e) {
                        log(`could not move the viewer up: ${e}`);
                    }
                    GLib.idle_add(GLib.PRIORITY_HIGH, () => {
                        finish(true);
                        return GLib.SOURCE_REMOVE;
                    });
                });
                timer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, VIEWER_WAIT_MS, () => {
                    timer = 0;
                    finish(true);
                    return GLib.SOURCE_REMOVE;
                });
            }
            this._launchViewer(target).then(ok => {
                if (!ok) {
                    log('the viewer could not be opened; staying where you are');
                    finish(false);
                } else if (!slide) {
                    finish(false);
                }
            }).catch(e => {
                log(`viewer: ${e}`);
                finish(false);
            });
        });
    }

    // Through the service when it answers; else the viewer program itself, which starts the service and says what
    // happened. Resolves false only when neither worked.
    async _launchViewer(target) {
        if (target.daemon && await this._requestViewer(target.name))
            return true;
        return this._spawnViewer();
    }

    _spawnViewer() {
        const cmd = this._daemonCommand();
        if (!cmd) {
            log('no agent-workspaces command found (set daemon-command in the extension settings)');
            return false;
        }
        const envMode = this._settings.get_boolean('environment-mode');
        try {
            GLib.spawn_async(null, ['setsid', '-f', cmd, 'viewer', envMode ? '--environment' : '--maximized'], null,
                GLib.SpawnFlags.SEARCH_PATH | GLib.SpawnFlags.STDOUT_TO_DEV_NULL | GLib.SpawnFlags.STDERR_TO_DEV_NULL,
                null);
            return true;
        } catch (e) {
            log(`could not run ${cmd} viewer: ${e.message}`);
            return false;
        }
    }

    leave() {
        const active = this._wm().get_active_workspace();
        if (this._entering && !(this._agentWs && this._wsValid(this._agentWs) && active === this._agentWs)) {
            this._entering.cancelled = true;          // Down while the agent desktop is still being prepared: stay put
            this._entering.abort?.();
            return;
        }
        if (!(this._agentWs && this._wsValid(this._agentWs) && active === this._agentWs)) {
            this._passThrough(Meta.MotionDirection.DOWN);
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
        if (target && target !== active) {
            const focus = this._originFocus;
            this._switchTo(target, +1);               // the personal desktop comes back up from below
            // keyboard focus returns to the window you were in
            if (focus && focus.get_workspace?.() === target && global.display.focus_window !== focus) {
                try {
                    focus.activate(global.get_current_time());
                } catch (e) { /* window went away */ }
            }
            this._originFocus = null;
        }
    }

    // When the viewer window is closed while you are up on the agent desktop, you are left on an empty desktop: go back down.
    _watchAgent(ws) {
        if (this._watchedWs === ws)
            return;
        this._unwatchAgent();
        this._watchedWs = ws;
        this._watchId = ws.connect('window-removed', () => {
            GLib.timeout_add(GLib.PRIORITY_DEFAULT, 400, () => {
                if (this._agentWs && this._wsValid(this._agentWs) && this._wm().get_active_workspace() === this._agentWs &&
                    this._viewerWindows().length === 0 && !this._slide && !this._pendingViewer && !this._entering)
                    this._afterOverview(() => this.leave());
                return GLib.SOURCE_REMOVE;
            });
        });
    }

    _unwatchAgent() {
        if (this._watchedWs && this._watchId) {
            try {
                this._watchedWs.disconnect(this._watchId);
            } catch (e) { /* workspace already gone */ }
        }
        this._watchedWs = null;
        this._watchId = 0;
    }

    _ensureAgentWorkspace(active) {
        const ws = this._ensureAgentWorkspace0(active);
        if (ws)
            this._watchAgent(ws);
        return ws;
    }

    _ensureAgentWorkspace0(active) {
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
            log('no viewer window is open and create-on-demand is off: nothing to go up to');
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

    // The agent desktop is stacked above the personal ones. dir -1: you go up (the personal desktop slides down and
    // out, the agent desktop slides down into place); dir +1: you come back (the reverse). Two full-screen
    // faces move together: the live window group, which already carries the real wallpaper, and a snapshot of
    // the face you leave with its own wallpaper and your bar and dock. Same 250 ms and ease-out-cubic as GNOME's
    // own workspace slide. Desktop Cube is not involved: its cube is for your personal workspaces only.
    _switchTo(ws, dir) {
        const active = this._wm().get_active_workspace();
        if (!ws || ws === active)
            return;
        this._cancelSlide();
        let snap = null;
        const animate = St.Settings.get().enable_animations && !Main.overview.visible;
        if (!animate) {
            this._ownSwitch = true;
            try {
                ws.activate(global.get_current_time());
            } finally {
                this._ownSwitch = false;
            }
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
        this._allowAgentNeighbor = true;
        this._ownSwitch = true;
        try {
            ws.activate(global.get_current_time());
        } finally {
            this._allowAgentNeighbor = false;
            this._ownSwitch = false;
        }
        if (patched) { // the switch did not go through animateSwitch (e.g. animations blocked)
            delete ctrl.animateSwitch;
        }
        if (!snap)
            return;
        try {
            this._playSlide(snap, dir);
        } catch (e) {
            log(`slide failed, no animation: ${e}`);
            this._slide = null;
            this._resetFaces(snap);
        }
    }

    _snapshotWindows() {
        // The face you leave: its own wallpaper, your bar and dock, and Clutter clones of the windows on screen. A
        // clone keeps painting its source after the switch hides the source window actor, which is what lets the
        // old workspace slide away.
        const group = global.window_group;
        const snap = new Clutter.Actor({reactive: false});
        snap.set_size(global.stage.width, global.stage.height);
        snap._bg = this._wallpaperFace();
        snap.add_child(snap._bg);
        for (const a of group.get_children()) {
            if (!a.visible || !a.meta_window)
                continue;
            const clone = new Clutter.Clone({source: a, reactive: false});
            clone.set_position(a.x, a.y);
            clone.set_size(a.width, a.height);
            snap.add_child(clone);
        }
        for (const a of this._chromeActors()) {
            if (!a.visible)
                continue;
            const [x, y] = a.get_transformed_position();
            const c = new Clutter.Clone({source: a, reactive: false});
            c.set_position(x, y);
            c.set_size(a.width, a.height);
            snap.add_child(c);
        }
        Main.uiGroup.insert_child_above(snap, group);
        return snap;
    }

    _chromeActors() {
        // Your own desktop's furniture: the top bar and the dock. They belong to the personal world, so they slide away
        // with the personal face; the agent desktop is full screen and has its own taskbar.
        const out = [Main.layoutManager.panelBox];
        for (const c of Main.uiGroup.get_children()) {
            if (c.constructor.name === 'DashToDock')
                out.push(c);
        }
        return out.filter(a => a);
    }

    _wallpaperFace() {
        // A face carries its own wallpaper: a fresh BackgroundManager per monitor in a holder the size of the stage,
        // what the shell's own workspace-switch groups do (clones of the live background paint blank).
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

    _resetFaces(snap) {
        const g = global.window_group;
        g.remove_all_transitions();
        g.translation_y = 0;
        for (const c of this._chromeActors()) {
            c.remove_all_transitions();
            c.opacity = 255;
        }
        if (snap) {
            for (const m of snap._bg?._mgrs ?? [])
                m.destroy();
            snap.remove_all_transitions();
            snap.destroy();
        }
    }

    _playSlide(snap, dir) {
        const group = global.window_group;
        const H = global.stage.height;
        const samples = [];
        const sid = group.connect('notify::translation-y', () => samples.push(Math.round(group.translation_y)));
        const chrome = this._chromeActors();
        for (const c of chrome)
            c.opacity = 0;                    // the personal desktop's bar and dock are on the snapshot, they leave with it
        this._slide = {kind: 'slide', snap, sid, samples, chrome};
        group.translation_y = dir * H;
        group.ease({
            translation_y: 0,
            duration: SLIDE_MS,
            mode: Clutter.AnimationMode.EASE_OUT_CUBIC,
            onStopped: () => this._finishSlide(),
        });
        snap.ease({
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
        for (const m of s.snap._bg?._mgrs ?? [])
            m.destroy();
        s.snap.remove_all_transitions();
        s.snap.destroy();
        for (const c of s.chrome) {            // arriving on a personal workspace: bar and dock fade back in
            c.remove_all_transitions();
            c.ease({opacity: 255, duration: 160, mode: Clutter.AnimationMode.EASE_OUT_QUAD});
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

    // Same locations as the Python side (aw/paths.py, docs/api.md 'Credential lookup', role admin).
    _baseDir() {
        return GLib.getenv('AW_BASE_DIR') ||
            GLib.build_filenamev([GLib.get_user_runtime_dir(), 'agent-workspaces']);
    }

    _socketPath() {
        return GLib.build_filenamev([this._baseDir(), 'api.sock']);
    }

    _readTokenFile(path) {
        try {
            const [ok, bytes] = GLib.file_get_contents(path);
            return ok ? new TextDecoder().decode(bytes).trim() || undefined : undefined;
        } catch (e) {
            return undefined;
        }
    }

    // The extension acts for the person at the keyboard, so it uses the admin credential: AW_ADMIN_TOKEN, the file
    // AW_ADMIN_TOKEN_FILE, else <base>/admin.token (a 0600 file next to the socket). No credential: the daemon
    // answers E_UNAUTHENTICATED and the notice below says what to do.
    _adminToken() {
        return GLib.getenv('AW_ADMIN_TOKEN') ||
            (GLib.getenv('AW_ADMIN_TOKEN_FILE') && this._readTokenFile(GLib.getenv('AW_ADMIN_TOKEN_FILE'))) ||
            this._readTokenFile(GLib.build_filenamev([this._baseDir(), 'admin.token']));
    }

    _daemonCall(method, params, timeoutMs = 4000) {
        return new Promise((resolve, reject) => {
            const client = new Gio.SocketClient();
            const cancel = new Gio.Cancellable();
            const timer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, timeoutMs, () => {
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

    // ---- the service: never a silent no-op, never an invisible desktop ---------------------------------

    _daemonAlive() {
        // A unix-socket connect to a local file answers at once, so this is fine to do synchronously on a key press.
        try {
            const c = new Gio.SocketClient();
            const conn = c.connect(Gio.UnixSocketAddress.new(this._socketPath()), null);
            conn.close(null);
            return true;
        } catch (e) {
            return false;
        }
    }

    _daemonCommand() {
        const custom = this._settings.get_string('daemon-command');
        if (custom)
            return custom;
        const home = GLib.get_home_dir();
        for (const c of [GLib.build_filenamev([home, '.local', 'share', 'agent-workspaces', 'bin', 'agent-workspaces']),
            GLib.build_filenamev([home, '.local', 'bin', 'agent-workspaces']),
            GLib.find_program_in_path('agent-workspaces')]) {
            if (c && GLib.file_test(c, GLib.FileTest.IS_EXECUTABLE))
                return c;
        }
        return null;
    }

    _run(argv, timeoutMs) {
        // Run a command, resolve with its exit status (-1: could not run, -2: timed out). Never blocks the shell.
        return new Promise(resolve => {
            let proc;
            try {
                proc = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE);
            } catch (e) {
                resolve(-1);
                return;
            }
            const timer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, timeoutMs, () => {
                try {
                    proc.force_exit();
                } catch (e) { /* gone */ }
                resolve(-2);
                return GLib.SOURCE_REMOVE;
            });
            proc.wait_async(null, (p, res) => {
                GLib.source_remove(timer);
                try {
                    p.wait_finish(res);
                    resolve(p.get_exit_status());
                } catch (e) {
                    resolve(-1);
                }
            });
        });
    }

    _waitForSocket(ms) {
        return new Promise(resolve => {
            const t0 = GLib.get_monotonic_time();
            GLib.timeout_add(GLib.PRIORITY_DEFAULT, 100, () => {
                if (this._daemonAlive()) {
                    resolve(true);
                    return GLib.SOURCE_REMOVE;
                }
                if ((GLib.get_monotonic_time() - t0) / 1000 > ms) {
                    resolve(false);
                    return GLib.SOURCE_REMOVE;
                }
                return GLib.SOURCE_CONTINUE;
            });
        });
    }

    /** Start the AgentWorkspaces service: the systemd user unit first, else the daemon itself, with bounded waits.
     *  Resolves true when the socket answers; otherwise says exactly why in a notification and resolves false. */
    async _startDaemon() {
        if (this._daemonAlive())
            return true;
        if (this._startingDaemon)
            return this._startingDaemon;
        this._startingDaemon = (async () => {
            const why = [];
            const st = await this._run(['systemctl', '--user', 'start', 'agent-workspaces.service'], 5000);
            if (st === 0 && await this._waitForSocket(4000))
                return true;
            why.push(st === 0 ? 'the systemd unit started but the socket did not appear' :
                st === -2 ? 'systemctl timed out' : 'no usable agent-workspaces.service user unit');
            const cmd = this._daemonCommand();
            if (!cmd) {
                why.push('no agent-workspaces command found (set daemon-command in the extension settings)');
            } else {
                const base = this._baseDir();
                GLib.mkdir_with_parents(base, 0o700);
                try {
                    GLib.spawn_async(null, ['setsid', '-f', cmd, 'daemon'], null,
                        GLib.SpawnFlags.SEARCH_PATH | GLib.SpawnFlags.STDOUT_TO_DEV_NULL | GLib.SpawnFlags.STDERR_TO_DEV_NULL,
                        null);
                } catch (e) {
                    why.push(`could not run ${cmd}: ${e.message}`);
                }
                if (await this._waitForSocket(6000))
                    return true;
                why.push(`${cmd} daemon did not open ${this._socketPath()} within 6 s`);
            }
            log(`the AgentWorkspaces service could not be started (${why.join('; ')})`);
            return false;
        })();
        try {
            return await this._startingDaemon;
        } finally {
            this._startingDaemon = null;
        }
    }

    // name: a workspace to open; omitted: the viewer opens where you left off. Resolves true when the viewer was started
    // (or is already open); false with the reason in the log.
    async _requestViewer(name = null) {
        if (this._pendingViewer)
            return true;
        this._pendingViewer = true;
        try {
            const env = {};
            for (const k of ['WAYLAND_DISPLAY', 'DISPLAY', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS'])
                if (GLib.getenv(k))
                    env[k] = GLib.getenv(k);
            const envMode = this._settings.get_boolean('environment-mode');
            const params = {display_env: env, environment: envMode, maximized: !envMode};
            if (name)
                params.workspace = name;
            const r = await this._daemonCall('workspace.enter', params);
            if (r && r.launched === false) {
                log(`the viewer could not be opened: ${r.reason ?? 'unknown reason'}`);
                return false;
            }
            return true;
        } catch (e) {
            log(`workspace.enter failed: ${e.message}`);
            return false;
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
        for (const k of ['switch-to-workspace-up', 'switch-to-workspace-down', 'move-to-workspace-up', 'move-to-workspace-down', 'switch-to-workspace-left', 'switch-to-workspace-right'])
            keys[k] = this._wmSettings.get_strv(k);
        return JSON.stringify({
            active: wm.get_active_workspace_index(), n_workspaces: wm.n_workspaces,
            agent: this._agentWs && this._wsValid(this._agentWs) ? this._agentWs.index() : -1,
            origin: this._wsValid(this._origin) ? this._origin.index() : -1,
            layout_rows: wm.layout_rows, windows: wins, keys, backup: this._settings.get_string('backup'),
            conflicts: this._settings.get_strv('conflicts'), slide_samples: this._lastSamples,
            sliding: !!this._slide, agent_active: this._agentActive(), focus: global.display.focus_window?.get_title?.() ?? null, entering: !!this._entering, daemon_socket: this._socketPath(),
            animations: St.Settings.get().enable_animations, pointer: global.get_pointer().slice(0, 2),
            notifications: Main.messageTray.getSources().map(x => `${x.title}:${x.notifications?.length ?? 0}`),
            window_actors: global.get_window_actors().length, overview: Main.overview.visible,
            wg: [global.window_group.translation_y, global.window_group.rotation_angle_x, global.window_group.translation_z],
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
