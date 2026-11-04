#!/usr/bin/env bash
# Remove MediSense from this Mac, so a new version can be installed clean.
#
#   bash uninstall.sh        (or double-click "Uninstall MediSense.command")
#
# Removes the program (~/Applications/MediSense), its private Python and tools
# (~/.medisense), the Desktop icons and the downloaded camera models. Your
# recordings, posture history, calibrations and board setup in ~/MediSense
# are kept unless you type "yes" when asked. Nothing outside these folders is
# touched (the WiFi password stays in your Keychain as "MediSense WiFi").
set -u

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok() { printf '  ✓ %s\n' "$*"; }

say "Removing MediSense from this Mac"

# Running MediSense windows (and any camera engine they started) first.
if pkill -f "Applications/MediSense/\\.venv(-posture)?/bin/python" 2>/dev/null; then
  ok "closed MediSense windows that were still running"
  sleep 1
fi

if [ -d "$HOME/Applications/MediSense" ]; then
  rm -rf "$HOME/Applications/MediSense"
  ok "removed the program (~/Applications/MediSense)"
fi
if [ -d "$HOME/.medisense" ]; then
  rm -rf "$HOME/.medisense"
  ok "removed its private Python and tools (~/.medisense)"
fi
removed_icons=0
for name in "MediSense" "MediSense Demo" "MediSense Posture" "MediSense Pro"; do
  if [ -f "$HOME/Desktop/$name.command" ]; then
    rm -f "$HOME/Desktop/$name.command"
    removed_icons=1
  fi
done
[ "$removed_icons" = 1 ] && ok "removed the Desktop icons"
if [ -d "$HOME/MediSense/cache" ]; then
  rm -rf "$HOME/MediSense/cache"
  ok "removed downloaded camera models (they download again when needed)"
fi

if [ -d "$HOME/MediSense" ]; then
  printf '\nYour recordings, posture history and board setup are in ~/MediSense.\n'
  printf 'Type yes to delete them too, or just press Return to keep them: '
  read -r answer || answer=""
  if [ "$answer" = "yes" ]; then
    rm -rf "$HOME/MediSense"
    ok "deleted ~/MediSense"
  else
    ok "kept ~/MediSense"
  fi
fi

say "Done. MediSense is removed."
printf 'To install again: open the folder you downloaded and double-click "Install MediSense.command".\n'
printf 'Tip: in your browser, close any old MediSense tabs before you start it again.\n\n'
