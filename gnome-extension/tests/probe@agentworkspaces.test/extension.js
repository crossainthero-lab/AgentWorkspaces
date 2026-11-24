// TEST-ONLY probe, loaded into an ISOLATED headless GNOME Shell (never install it on a real desktop).
// Exposes dev.agentworkspaces.Probe on the private session bus: Eval (run JS inside the shell, returns JSON),
// Shot (Shell.Screenshot of the whole stage). Same idea as the probe of the sibling Gnotch test harness.
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

Gio._promisify(Shell.Screenshot.prototype, 'screenshot', 'screenshot_finish');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

const XML = `<node><interface name="dev.agentworkspaces.Probe">
  <method name="Shot"><arg type="s" direction="in"/><arg type="b" direction="out"/></method>
  <method name="Eval"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
</interface></node>`;

export default class Probe extends Extension {
    enable() {
        this._impl = Gio.DBusExportedObject.wrapJSObject(XML, this);
        this._impl.export(Gio.DBus.session, '/dev/agentworkspaces/Probe');
        this._own = Gio.DBus.session.own_name('dev.agentworkspaces.Probe', Gio.BusNameOwnerFlags.NONE, null, null);
    }

    disable() {
        this._impl?.unexport();
        Gio.bus_unown_name(this._own);
    }

    async Shot(path) {
        const stream = Gio.File.new_for_path(path).replace(null, false, Gio.FileCreateFlags.NONE, null);
        const shot = new Shell.Screenshot();
        await shot.screenshot(false, stream);
        stream.close(null);
        return true;
    }

    async Eval(code) {
        try {
            const fn = new AsyncFunction('Main', 'Gio', 'GLib', 'St', 'Clutter', 'Meta', 'Shell', 'global', code);
            const r = await fn(Main, Gio, GLib, St, Clutter, Meta, Shell, global);
            return JSON.stringify(r === undefined ? null : r);
        } catch (e) {
            return JSON.stringify({error: `${e}`, stack: `${e.stack}`});
        }
    }
}
