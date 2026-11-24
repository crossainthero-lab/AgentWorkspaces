#!/usr/bin/env bash
# Reverses install.sh. Leaves your GNOME settings and other extensions alone.
#   ./uninstall.sh [--prefix DIR] [--bin-dir DIR] [--stop-workspaces] [--remove-integrations] [--dry-run]
set -euo pipefail
PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}/agent-workspaces"
BINDIR="$HOME/.local/bin"
STOP=0; INTEG=0; DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2;;
    --bin-dir) BINDIR="$2"; shift 2;;
    --stop-workspaces) STOP=1; shift;;
    --remove-integrations) INTEG=1; shift;;
    --dry-run) DRY=1; shift;;
    -h|--help) sed -n '2,3p' "$0"; exit 0;;
    *) echo "unknown option $1" >&2; exit 2;;
  esac
done
run() { if [ "$DRY" = 1 ]; then echo "[dry-run] $*"; else "$@" || true; fi; }
CLI="$PREFIX/bin/agent-workspaces"

if [ "$INTEG" = 1 ] && [ -x "$CLI" ]; then
  for a in claude-code codex antigravity hermes; do run "$CLI" integrate "$a" uninstall; done
fi
if [ "$STOP" = 1 ] && [ -x "$CLI" ]; then
  # destroy every workspace: stops its compositor and apps (its own systemd slice)
  for id in $(AW_NO_AUTOSTART=1 "$CLI" --json list 2>/dev/null | python3 -c 'import sys,json
try:
    [print(w["id"]) for w in json.load(sys.stdin)["workspaces"]]
except Exception: pass'); do run "$CLI" destroy "$id"; done
fi
run systemctl --user disable --now agent-workspaces.service
run rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/agent-workspaces.service"
run systemctl --user daemon-reload
# only remove the symlink if it points at us
if [ -L "$BINDIR/agent-workspaces" ] && [ "$(readlink "$BINDIR/agent-workspaces")" = "$PREFIX/bin/agent-workspaces" ]; then
  run rm -f "$BINDIR/agent-workspaces"
fi
run rm -rf "$PREFIX"
echo "Removed. Workspaces still running (if you did not pass --stop-workspaces) keep running until you reboot or destroy them."
echo "Agent MCP registrations were $( [ "$INTEG" = 1 ] && echo removed || echo 'left in place; run: agent-workspaces integrate <agent> uninstall (before removing)')."
echo "GNOME extension (if you installed it): gnome-extensions disable agent-workspaces@agentworkspaces.dev && remove it with its own uninstall.sh."
