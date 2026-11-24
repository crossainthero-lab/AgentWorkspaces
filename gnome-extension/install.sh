#!/bin/sh
# User-level install of the AgentWorkspaces GNOME Shell extension. Needs no root.
# It copies the files and compiles the schema. It does NOT enable the extension: that is your decision
# (see README.md). Uninstall: ./install.sh --uninstall
#
#   ./install.sh [--dest DIR] [--uninstall]
#   --dest DIR   extensions directory (default: ${XDG_DATA_HOME:-~/.local/share}/gnome-shell/extensions)
set -eu
UUID="agent-workspaces@agentworkspaces.dev"
HERE=$(cd "$(dirname "$0")" && pwd)
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions"
MODE=install
while [ $# -gt 0 ]; do
  case "$1" in
    --dest) DEST="$2"; shift 2 ;;
    --uninstall) MODE=uninstall; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
TARGET="$DEST/$UUID"
if [ "$MODE" = uninstall ]; then
  if [ -d "$TARGET" ]; then
    rm -rf "$TARGET"
    echo "removed $TARGET"
    echo "If the extension was enabled, GNOME keeps its UUID in enabled-extensions until you run:"
    echo "  gnome-extensions disable $UUID   (a disabled extension restores the keybindings it changed)"
    echo "Log out and in (or restart the shell) to unload it completely."
  else
    echo "not installed at $TARGET"
  fi
  exit 0
fi
command -v glib-compile-schemas >/dev/null || { echo "glib-compile-schemas is required (libglib2.0-bin)" >&2; exit 1; }
mkdir -p "$TARGET/schemas"
for f in metadata.json extension.js prefs.js; do cp "$HERE/$f" "$TARGET/$f"; done
cp "$HERE"/schemas/*.gschema.xml "$TARGET/schemas/"
glib-compile-schemas --strict "$TARGET/schemas"
echo "installed to $TARGET (not enabled)"
echo "Enable when you are ready:   gnome-extensions enable $UUID"
echo "Disable (restores bindings): gnome-extensions disable $UUID"
