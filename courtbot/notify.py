"""Telling the user what happened.

A bot that books at 08:00 while you sleep is only useful if you find out. This
writes a durable log line either way, and on macOS raises a notification —
without adding a dependency, since `osascript` is always present.
"""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

HISTORY = Path("captured/history.log")


def record(line: str, path: Path = HISTORY) -> None:
    """Append a timestamped line to the run history."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp}  {line}\n")
    except OSError as exc:
        log.warning("could not write history to %s: %s", path, exc)


def desktop(title: str, message: str, *, sound: bool = True) -> bool:
    """Raise a desktop notification. Returns False if none could be shown."""
    if platform.system() != "Darwin":
        log.debug("desktop notifications are macOS-only; skipping")
        return False

    # terminal-notifier survives being run from a launchd agent more reliably
    # than osascript does, so prefer it when installed.
    if shutil.which("terminal-notifier"):
        cmd = ["terminal-notifier", "-title", title, "-message", message]
        if sound:
            cmd += ["-sound", "Glass"]
    else:
        safe_t = title.replace('"', "'")
        safe_m = message.replace('"', "'")
        script = f'display notification "{safe_m}" with title "{safe_t}"'
        if sound:
            script += ' sound name "Glass"'
        cmd = ["osascript", "-e", script]

    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=10)
        return True
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("notification failed: %s", exc)
        return False


def announce(outcome, *, enabled: bool = True) -> None:
    """Log and notify for a SnipeOutcome."""
    line = outcome.summary()
    record(line)
    if not enabled:
        return
    if outcome.booked:
        desktop("🎾 Court booked", line)
    elif not outcome.deferred:
        desktop("Court booking failed", line, sound=False)
