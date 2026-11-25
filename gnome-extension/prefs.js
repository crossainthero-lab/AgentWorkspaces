import Adw from 'gi://Adw';
import Gdk from 'gi://Gdk';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Gtk from 'gi://Gtk';

import {ExtensionPreferences} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

const ROWS = [
    {key: 'enter-shortcut', title: 'Go up to AgentWorkspaces', subtitle: 'The agent desktop is stacked above your personal ones.'},
    {key: 'return-shortcut', title: 'Come back down', subtitle: 'From the agent desktop, back to the personal workspace you came from.'},
];

export default class AgentWorkspacesPrefs extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        const settings = this.getSettings();
        window.set_default_size(560, 480);
        const page = new Adw.PreferencesPage();
        window.add(page);

        const shortcuts = new Adw.PreferencesGroup({title: 'Shortcuts'});
        page.add(shortcuts);

        let capturing = null; // {key, button, label}
        const refreshers = [];

        for (const {key, title, subtitle} of ROWS) {
            const row = new Adw.ActionRow({title, subtitle});
            const label = new Gtk.ShortcutLabel({accelerator: settings.get_strv(key).join(' '), valign: Gtk.Align.CENTER,
                disabled_text: 'Disabled'});
            const change = new Gtk.Button({child: label, valign: Gtk.Align.CENTER, tooltip_text: 'Click, then press the new shortcut'});
            const reset = new Gtk.Button({icon_name: 'edit-undo-symbolic', valign: Gtk.Align.CENTER, css_classes: ['flat'],
                tooltip_text: 'Restore default'});
            row.add_suffix(change);
            row.add_suffix(reset);
            shortcuts.add(row);

            const show = () => {
                label.accelerator = settings.get_strv(key).join(' ');
                const def = settings.get_default_value(key).deepUnpack();
                reset.sensitive = settings.get_strv(key).join(' ') !== def.join(' ');
            };
            show();
            settings.connect(`changed::${key}`, show);
            reset.connect('clicked', () => settings.reset(key));
            change.connect('clicked', () => {
                if (capturing?.button === change) {
                    stopCapture();
                    return;
                }
                stopCapture();
                capturing = {key, button: change, label};
                label.accelerator = '';
                label.disabled_text = 'Press a shortcut, Esc to cancel';
                change.grab_focus();
            });
            refreshers.push(show);
        }

        const stopCapture = () => {
            if (!capturing)
                return;
            capturing.label.disabled_text = 'Disabled';
            capturing = null;
            refreshers.forEach(f => f());
        };

        const keys = new Gtk.EventControllerKey();
        keys.connect('key-pressed', (_c, keyval, keycode, state) => {
            if (!capturing)
                return Gdk.EVENT_PROPAGATE;
            const mods = state & Gtk.accelerator_get_default_mod_mask();
            if (keyval === Gdk.KEY_Escape && !mods) {
                stopCapture();
                return Gdk.EVENT_STOP;
            }
            if ([Gdk.KEY_Control_L, Gdk.KEY_Control_R, Gdk.KEY_Alt_L, Gdk.KEY_Alt_R, Gdk.KEY_Shift_L, Gdk.KEY_Shift_R,
                Gdk.KEY_Super_L, Gdk.KEY_Super_R, Gdk.KEY_Meta_L, Gdk.KEY_Meta_R].includes(keyval))
                return Gdk.EVENT_STOP; // wait for the real key
            const useful = mods & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK);
            if (!useful)
                return Gdk.EVENT_STOP; // a shortcut without Ctrl, Alt or Super would steal normal typing
            const accel = Gtk.accelerator_name(Gdk.keyval_to_lower(keyval), mods);
            settings.set_strv(capturing.key, [accel]);
            stopCapture();
            return Gdk.EVENT_STOP;
        });
        window.add_controller(keys);

        // Conflicts are computed by the extension when it is enabled.
        const status = new Adw.PreferencesGroup();
        page.add(status);
        const statusLabel = new Gtk.Label({wrap: true, xalign: 0, css_classes: ['dim-label']});
        status.add(statusLabel);
        const showConflicts = () => {
            const lines = [];
            const taken = [];
            for (const c of settings.get_strv('conflicts')) {
                if (c.endsWith('(not modified)'))
                    lines.push(`Also used by ${c.replace(' (not modified)', '')}. GNOME may run either one.`);
                else
                    taken.push(c.replace(/^.*\.(switch-to-workspace-\w+) .*$/, '$1'));
            }
            if (taken.length) {
                lines.unshift(`Replaces GNOME's ${taken.join(' and ')} on the same keys while the extension is on; ` +
                    'restored when it is turned off.');
            }
            statusLabel.label = lines.join('\n');
            status.visible = lines.length > 0;
        };
        showConflicts();
        settings.connect('changed::conflicts', showConflicts);

        const behaviour = new Adw.PreferencesGroup();
        page.add(behaviour);
        const toggle = new Adw.SwitchRow({title: 'Create the AgentWorkspaces workspace on demand',
            subtitle: 'Off: entering only works while a viewer window is already open.'});
        behaviour.add(toggle);
        settings.bind('create-on-demand', toggle, 'active', Gio.SettingsBindFlags.DEFAULT);
    }
}
