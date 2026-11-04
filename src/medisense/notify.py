"""System notifications: macOS Notification Center (works on macOS 11, no installs).

macOS shows these as coming from "Script Editor". If none appear, allow them
once: System Preferences → Notifications → Script Editor → Allow Notifications.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys

log = logging.getLogger("medisense.notify")

# arguments are passed separately, so no quoting problems with any text
_SCRIPT = [
    "on run argv",
    "set theMessage to item 1 of argv",
    "set theTitle to item 2 of argv",
    "set theSound to item 3 of argv",
    'if theSound is "" then',
    "display notification theMessage with title theTitle",
    "else",
    "display notification theMessage with title theTitle sound name theSound",
    "end if",
    "end run",
]


def send(title: str, message: str, sound: bool = True, sound_name: str = "Tink") -> bool:
    """Show a notification. Returns True if the system accepted it."""
    message = message[:240]
    try:
        if sys.platform == "darwin":
            cmd = ["osascript"]
            for line in _SCRIPT:
                cmd += ["-e", line]
            cmd += [message, title, sound_name if sound else ""]
            r = subprocess.run(cmd, capture_output=True, timeout=10)
            return r.returncode == 0
        if shutil.which("notify-send"):
            r = subprocess.run(["notify-send", title, message], capture_output=True, timeout=10)
            return r.returncode == 0
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("notification failed: %s", e)
        return False
    log.info("notification: %s: %s", title, message)
    return False
