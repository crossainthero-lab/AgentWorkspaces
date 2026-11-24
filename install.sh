#!/usr/bin/env bash
# AgentWorkspaces user-level installer. No root, no GNOME configuration changes, no reboot.
#   ./install.sh [--prefix DIR] [--bin-dir DIR] [--enable-service] [--dry-run]
# Reverse with ./uninstall.sh
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}/agent-workspaces"
BINDIR="$HOME/.local/bin"
ENABLE=0
DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2;;
    --bin-dir) BINDIR="$2"; shift 2;;
    --enable-service) ENABLE=1; shift;;
    --dry-run) DRY=1; shift;;
    -h|--help) sed -n '2,5p' "$0"; exit 0;;
    *) echo "unknown option $1" >&2; exit 2;;
  esac
done
run() { if [ "$DRY" = 1 ]; then echo "[dry-run] $*"; else "$@"; fi; }

echo "== checking requirements"
missing=0
need() { command -v "$1" >/dev/null 2>&1 || { echo "  missing: $1 ($2)"; missing=1; }; }
need python3 "python3"
need labwc "sudo apt install labwc   (the workspace compositor)"
need systemd-run "systemd (user scopes)"
python3 - <<'PY' || echo "  note: python3-gi / GTK4 not importable: the viewer window (agent-workspaces enter) will not work; everything else will"
import gi
gi.require_version("Gtk", "4.0")
PY
[ -e /usr/lib/x86_64-linux-gnu/libxkbcommon.so.0 ] || { echo "  missing: libxkbcommon0"; missing=1; }
if [ "$missing" = 1 ]; then echo "Install the missing items above (needs root, done by you) and re-run." >&2; exit 1; fi

echo "== installing to $PREFIX"
run mkdir -p "$PREFIX" "$BINDIR"
# replace only our own files
run rm -rf "$PREFIX/aw" "$PREFIX/bin" "$PREFIX/gnome-extension" "$PREFIX/docs"
run cp -r "$SRC/aw" "$SRC/bin" "$SRC/docs" "$SRC/gnome-extension" "$PREFIX/"
run find "$PREFIX" -name __pycache__ -type d -prune -exec rm -rf {} +
run chmod +x "$PREFIX/bin/agent-workspaces"
run mkdir -p "$PREFIX/.installed"
if [ "$DRY" = 0 ]; then date -Is > "$PREFIX/.installed/stamp"; fi

LINK="$BINDIR/agent-workspaces"
if [ -e "$LINK" ] && [ ! -L "$LINK" ]; then
  echo "  $LINK exists and is not a symlink: leaving it alone" >&2
else
  run ln -sfn "$PREFIX/bin/agent-workspaces" "$LINK"
  [ "$DRY" = 0 ] && echo "$LINK" > "$PREFIX/.installed/link"
fi

UNITDIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
run mkdir -p "$UNITDIR"
run cp "$SRC/packaging/agent-workspaces.service" "$UNITDIR/agent-workspaces.service"
if [ "$ENABLE" = 1 ]; then
  run systemctl --user daemon-reload
  run systemctl --user enable --now agent-workspaces.service
else
  run systemctl --user daemon-reload || true
  echo "  daemon unit installed but not enabled (the CLI starts the daemon on demand)."
  echo "  to run it as a service:  systemctl --user enable --now agent-workspaces.service"
fi

cat <<MSG

Installed. Try:
  agent-workspaces create --name demo --agent claude-code
  agent-workspaces launch demo -- gnome-text-editor
  agent-workspaces screenshot demo -o demo.png
  agent-workspaces enter demo

Not done on purpose (each is opt-in and reversible):
  * agent integrations:   agent-workspaces integrate <claude-code|codex|antigravity|hermes> install
  * GNOME Shell extension: $PREFIX/gnome-extension/install.sh   (then enable it yourself)
  * no GNOME shortcut, setting or extension has been changed.
MSG
