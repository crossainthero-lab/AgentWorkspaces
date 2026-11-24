# Browsers in a workspace

```
agent-workspaces browsers NAME                       # installed browsers + profile NAMES/IDS
agent-workspaces launch-browser NAME --url https://example.org            # throwaway profile
agent-workspaces launch-browser NAME --browser firefox-tarball --profile agent:research
agent-workspaces launch-browser NAME --profile authorized:Default         # needs your approval, below
```

API: `browser.list`, `browser.launch`, `browser.profile.list`, `browser.profile.request` (agent),
`browser.profile.authorize` / `browser.profile.revoke` (human only). MCP-facing tool definitions: `aw/mcp_ext.py`
(`list_browsers`, `launch_browser`, `request_browser_profile`).

A browser started this way is an ordinary app of the workspace: it runs through the launcher, so it sees only
the workspace's Wayland display (or its XWayland), private runtime dir, private session bus and private audio.
There is no Claude-in-Chrome, no AI extension and no external automation service involved, and AgentWorkspaces
adds no remote-debugging port or pipe.

## What is launched

* Chromium family: `--user-data-dir=<profile> --ozone-platform=wayland --no-first-run
  --no-default-browser-check --password-store=basic --disable-sync --disable-background-networking
  --disable-features=Translate`. `--backend x11` uses `--ozone-platform=x11` on the workspace's XWayland.
  Verified with Google Chrome 152.
* Firefox family: `--no-remote --profile <profile>` (+ `MOZ_ENABLE_WAYLAND=0 GDK_BACKEND=x11` for `--backend
  x11`). A `user.js` is written for profiles AgentWorkspaces creates: telemetry, update checks and the
  default-browser prompt off. It does **not** accept any terms on your behalf: Firefox 160 shows its own
  first-run terms screen and somebody has to click "Continue" (the compatibility test does that with a real
  click).
* Arguments that would open an automation channel, load code or change the profile are refused with
  `E_BAD_BROWSER_ARG`: `--remote-debugging-*`, `--remote-allow-origins`, `--user-data-dir`,
  `--profile-directory`, `--load-extension`, `--disable-extensions-except`, `--enable-automation`, `--no-sandbox`,
  `--disable-web-security`, `--headless`, `--ozone-platform`, `--marionette`, `--start-debugger-server`,
  `-profile`/`--profile`, `--no-remote`, `--new-instance`, and similar. URLs must be `http(s)://`, `about:` or
  `file:///`. A profile is never given as a path: only as a mode and a safe name.
* Verified: no process of a Chrome workspace listens on a TCP port (the test walks the workspace cgroup and
  compares it with `ss -ltnp`).

## Discovery

`browsers` lists, per installed browser: id, label, family, variant (`deb`, `binary`, `snap`, `flatpak`),
whether it can be launched and why not. Profiles are listed by **id and display name only**: Chromium from the
`info_cache` of `Local State` (names and ids), Firefox from `profiles.ini` (`Name=` and the directory name).
No filesystem path of a host profile and nothing else from it is returned.

| browser | looked up | notes |
|---|---|---|
| Google Chrome (+beta/dev), Chromium, Brave, Edge, Vivaldi | `~/.config/<vendor>`, snap and flatpak data dirs | only Google Chrome (deb) was available to test |
| Firefox, LibreWolf | `~/.mozilla/firefox`, snap and flatpak data dirs, `~/.librewolf` | Firefox 160 vendor tarball tested |
| anything else | `AW_BROWSER_EXTRA='[{"id":"firefox-tarball","family":"firefox","exe":"/path/firefox"}]'` or `~/.config/agent-workspaces/browsers.json` | id must be `[A-Za-z0-9_.-]` |

**Snap and flatpak browsers are not launched.** The Ubuntu `firefox` is a snap here: snap confinement cannot
use the workspace's private runtime dir or profile, and a snap keeps its data under the real home
(`~/snap/...`), which a workspace must not touch. `launch-browser` answers `E_BROWSER_UNSUPPORTED` with that
reason; install the `.deb` or use the vendor tarball. (Tested: the snap stub is detected as `snap` and
nothing is started.)

## Profile modes

| mode | where | lifetime |
|---|---|---|
| `throwaway` (default) | `<workspace dir>/browser/throwaway/<browser>` (0700; under `$XDG_RUNTIME_DIR`, i.e. RAM) | kept while the workspace exists, deleted with it |
| `agent:NAME` | `~/.local/share/agent-workspaces/browser-profiles/<browser>/NAME` (0700) | kept across workspace restarts and deletions; **one workspace at a time** |
| `authorized:ID` | `~/.local/share/agent-workspaces/authorized/<workspace id>/<browser>__<ID>` (0700) | a COPY of one of your profiles; deleted on revoke, workspace deletion or expiry |

Session persistence is verified for both browser families: a persistent cookie set in an `agent:` profile is
still sent after the browser was closed and restarted, also from another workspace once the first browser
exited.

### Locks and concurrent instances

* A writable profile is never shared. `agent:` and `authorized:` profiles carry an owner record (workspace +
  browser pid + start time) outside the profile; a second workspace gets `E_PROFILE_BUSY` while the owner's
  browser lives (tested with real Chrome in two workspaces). The browser's own locks (`SingletonLock`,
  Firefox `lock`/`.parentlock`) are a second layer.
* **The user's own browser.** Profiles of your real browser are never opened by a workspace. To make a copy
  (below) AgentWorkspaces reads `SingletonLock` (Chromium) or `lock`/`.parentlock` (Firefox) of the host
  profile. A lock held by a running process, **and a stale lock** (owner gone), both refuse the copy: nothing is
  copied. **Locks are never removed**, also not stale ones (tested: the symlink is still there afterwards).

## Using a signed-in profile of yours: human-only authorisation

An agent can only **ask**: `browser.profile.request` (MCP `request_browser_profile`) creates a pending
`browser_profile` grant (see the grants in [security-model.md](security-model.md)) for `browser:profile` and
says which command you must run. It cannot grant, approve or revoke, with any workspace credential
(`E_FORBIDDEN`), and `browser.launch` with `authorized:` fails with `E_GRANT_REQUIRED` until you did.

You run, in a terminal:

```
agent-workspaces profile authorize google-chrome Default --workspace research
```

The command refuses to run without an interactive terminal, prints what will and will not be copied, checks the
host profile is closed, and asks you to type the workspace name. Then the daemon (admin credential **and** a
terminal peer, `E_NOT_HUMAN_CHANNEL` otherwise) makes the copy and records the consent in the audit log
(`browser.profile.cloned`: names, item list, byte count, channel; no paths, no contents).

What the copy contains: an **allow-list** of session data. Chromium: `Network/Cookies` (+journal),
`Local Storage`, `Session Storage`, `IndexedDB`, `Preferences`. Firefox: `cookies.sqlite` (+wal/shm),
`webappsstore.sqlite`, `storage/`, `permissions.sqlite`. **Never copied** (also enforced by a deny list):
saved passwords (`Login Data`, `logins.json`, `key4.db`), autofill/payment data, history, extensions, sync
data, caches, lock files, and anything reached through a symlink (symlinks are skipped). Files are 0600,
directories 0700, copy goes to `<name>.part-*` and is renamed into place. The original is only read: a test
fingerprints the whole original tree (names, sizes, mtimes, modes, contents) before and after and it is
identical.

`profile revoke` (terminal, confirmation) closes browsers of that workspace that use the copy, revokes the
grant, **overwrites every file with zeros, then removes it** (`browser.profile.shredded` in the audit log). The
same happens when the workspace is destroyed, and a daemon sweep removes any copy that has no live grant (the
default lifetime is 8 hours, `--ttl-hours`). Overwriting cannot erase blocks that a journaling, copy-on-write or
SSD filesystem keeps elsewhere (a unit test proves the inode's content is zeroed, not that the disk is clean).

Tested end to end with real Chrome: a "host" profile built by a real Chrome in a throw-away fake home with a
cookie; the copy via the CLI in a real pty; `launch-browser --profile authorized:Default` is recognised by a
local web server as the same session; the copy has no `Login Data`/`Web Data`, is 0700, the original is
byte-identical; revoke removes the copy and launching is refused; destroying the workspace removes the copy.

### Limits you should know

* **Chromium cookies may be unreadable in the copy.** The copy is launched with `--password-store=basic`
  (no keyring). Cookies that your Chrome encrypted with your desktop keyring (`v11`) cannot be decrypted without
  that keyring, and giving a workspace your keyring is exactly what this design avoids. In that case the page
  shows you signed out and the agent (or you, through the viewer) signs in once inside the copy. The test
  fixture is built with `--password-store=basic`, so that case is **not tested** with a real keyring-encrypted
  profile. Firefox cookies are plain SQLite and need nothing.
* A human channel is a terminal peer process (shared with the grants in `aw/daemon.py: _human_channel`). A
  process of yours that allocates a pseudo-terminal can imitate it, and the admin token file is readable by your
  other processes. This is the same same-user limit as everywhere else, see the security model.
* The copy stays valid for its TTL even if you change your password elsewhere; sites may notice a second
  device.
* **Not tested: any real browser profile of yours.** Everything above ran against fixtures in a fake home.
  Testing against your real profile needs your explicit approval.
