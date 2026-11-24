"""Authorisation policy of the window, audio and browser methods (merged into `auth.POLICY` by the daemon).

Kept here, next to the features, so the generic auth module stays untouched. Same table format as
`aw/auth.py: POLICY` (method -> (target, scope)); methods missing from the table are denied to every
non-admin credential.
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

POLICY_EXT: Dict[str, Tuple[str, Optional[str]]] = {
    # windows (docs/windows.md)
    "window.move": ("ws", "windows"), "window.resize": ("ws", "windows"), "window.set_geometry": ("ws", "windows"),
    # audio (docs/audio.md). Recording is observation of the workspace output, like a screenshot.
    "audio.info": ("ws", "read"), "audio.record": ("ws", "screenshot"),
    "audio.route": ("global", None),                       # host speakers: human channel only
    # browsers (docs/browsers.md)
    "browser.list": ("ws", "launch"), "browser.launch": ("ws", "launch"),
    "browser.profile.list": ("ws", "read"),
    "browser.profile.request": ("ws", "approval"),         # the agent can only ASK
    "browser.profile.authorize": ("global", None),         # admin credential + human channel (tty)
    "browser.profile.revoke": ("global", None),
}
