"""Bell + desktop notification when the CLI waits on the user.

Used by the approval and clarify prompts when ``display.notify_on_wait`` is
on.  The bell always rings (it reaches the user's terminal over SSH too);
the desktop notification is best effort, local sessions only, and never
blocks the agent thread.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Optional, TextIO


def _applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _launch_desktop_notification(title: str, message: str) -> None:
    if sys.platform == "darwin":
        osascript = shutil.which("osascript")
        if not osascript:
            return
        script = (
            f"display notification {_applescript_string(message)} "
            f"with title {_applescript_string(title)}"
        )
        argv = [osascript, "-e", script]
    elif sys.platform.startswith("linux"):
        notify_send = shutil.which("notify-send")
        if not notify_send:
            return
        argv = [notify_send, title, message]
    else:
        return
    try:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def notify_waiting(title: str, message: str, *, stream: Optional[TextIO] = None) -> None:
    """Ring the terminal bell and, outside SSH, post a desktop notification."""
    out = stream if stream is not None else sys.stdout
    try:
        out.write("\a")
        out.flush()
    except (OSError, ValueError):
        pass
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return
    _launch_desktop_notification(title, message[:200])
