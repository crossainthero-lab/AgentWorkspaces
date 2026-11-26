// The agent desktop in the Activities overview: one more desktop in GNOME's own row of workspace thumbnails, in the FIRST place.
//
// The workspace that holds the viewer is an ordinary GNOME workspace, so GNOME already draws a thumbnail for it: same row, same
// spacing, same size, same corner radius, same selection ring, live content (the viewer window, i.e. the live agent screen),
// and the big workspace area shows it when it is selected, all by GNOME's own code. Nothing is drawn by this extension. All it
// does while the overview is open is:
//   * move that workspace to the first position (the thumbnail row follows by itself) and put it back when the overview closes,
//     so everywhere else it stays where the extension keeps it (after your personal desktops, skipped by Ctrl+Alt+Left/Right);
//   * refuse windows dropped on its thumbnail (a window dragged there would leave your desktop for the agent's).
// Cost: nothing while the overview is closed; no polling while it is open (the thumbnail is a live clone of the viewer window).

import * as DND from 'resource:///org/gnome/shell/ui/dnd.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

export class OverviewStrip {
    constructor(ext) {
        this._ext = ext;
        this._open = false;
        this._moved = null;               // {ws, index} while the agent workspace is held in first position
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
    }

    _agent() {
        const a = this._ext._agentWs;
        return a && this._ext._wsValid(a) ? a : null;
    }

    _onShowing() {
        // The shell opens in the overview at login: do nothing until it has finished starting up.
        if (Main.layoutManager._startingUp)
            return;
        const agent = this._agent();
        if (!agent)
            return;
        this._open = true;
        const wm = global.workspace_manager;
        if (agent.index() !== 0) {
            this._moved = {ws: agent, index: agent.index()};
            wm.reorder_workspace(agent, 0);
        }
        this._refuseDrops(agent);
    }

    // A window dropped on the agent thumbnail would be moved onto the agent's desktop: say no, like GNOME does for a target
    // that cannot take it.
    _refuseDrops(agent) {
        try {
            const box = Main.overview._overview.controls._thumbnailsBox;
            for (const t of box?._thumbnails ?? []) {
                if (t.metaWorkspace !== agent)
                    continue;
                t.handleDragOverInternal = () => DND.DragMotionResult.NO_DROP;
                t.acceptDropInternal = () => false;
            }
        } catch (e) {
            console.log(`[agent-workspaces] overview: cannot refuse drops on the agent thumbnail: ${e}`);
        }
    }

    _onHidden() {
        this._open = false;
        const m = this._moved;
        this._moved = null;
        if (!m || !this._ext._wsValid(m.ws))
            return;
        // Back where it was: before GNOME's trailing empty workspace, after your own.
        const wm = global.workspace_manager;
        const last = wm.n_workspaces - 1;
        let target = Math.min(m.index, last);
        const lastWs = wm.get_workspace_by_index(last);
        const lastEmpty = lastWs !== m.ws && lastWs.list_windows().filter(w => !w.is_on_all_workspaces()).length === 0;
        if (lastEmpty && target >= last)
            target = last - 1;
        if (m.ws.index() !== target)
            wm.reorder_workspace(m.ws, Math.max(0, target));
    }
}
