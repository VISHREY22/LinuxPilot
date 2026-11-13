#!/bin/sh
# One-time setup (GNOME 42-44, no sudo): installs the "LinuxPilot window positions" GNOME Shell extension so
# LinuxPilot can outline the app it works in. Run: scripts/install-gnome-extension.sh, then log out and back in.
set -eu
UUID=linuxpilot-windows@linuxpilot
SRC="$(dirname "$0")/../src/linuxpilot/data/gnome-extension/$UUID"
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/$UUID"
mkdir -p "$DEST"
cp "$SRC/metadata.json" "$SRC/extension.js" "$DEST/"
if gnome-extensions enable "$UUID" 2>/dev/null; then
  echo "ok: enabled $UUID. If LinuxPilot still says it is missing, log out and back in once."
else
  echo "installed to $DEST. Log out and back in once (Wayland loads new extensions at login), then run:"
  echo "  gnome-extensions enable $UUID"
fi
echo "Undo: gnome-extensions disable $UUID && rm -r \"$DEST\""
