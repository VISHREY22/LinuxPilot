#!/bin/sh
# One-time keyboard setup; the script ships inside the package. Prefer: linuxpilot setup
exec sh "$(dirname "$0")/../src/linuxpilot/data/enable-uinput.sh" "$@"
