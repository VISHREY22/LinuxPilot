#!/bin/sh
# One-time setup: lets the logged-in desktop user create LinuxPilot's virtual keyboard (/dev/uinput),
# so keyboard input works without the GNOME "Remote desktop" consent dialog. Run with: sudo scripts/enable-uinput.sh
set -eu
if [ "$(id -u)" -ne 0 ]; then echo "run with sudo: sudo $0" >&2; exit 1; fi
RULE=/etc/udev/rules.d/70-linuxpilot-uinput.rules
echo 'KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"' > "$RULE"
echo uinput > /etc/modules-load.d/linuxpilot-uinput.conf
modprobe uinput
udevadm control --reload-rules
udevadm trigger --subsystem-match=misc --action=change
sleep 1
USER_NAME="${SUDO_USER:-}"
if [ -n "$USER_NAME" ] && su "$USER_NAME" -c 'test -w /dev/uinput'; then
  echo "ok: $USER_NAME can write /dev/uinput (rule: $RULE). Undo with: sudo rm $RULE"
else
  echo "rule installed ($RULE); log out and back in (or reboot) so the desktop session gets access."
fi
