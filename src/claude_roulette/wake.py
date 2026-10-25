"""Wake the Mac for the morning ping with a repeating pmset event.

macOS allows only one repeating wake/power-on event (plus one sleep/power-off), and setting
it needs root, so `install` asks sudo for it. It only replaces a repeating schedule it set
itself (recorded in WAKE_FILE); one you set up yourself is left alone and the command is
printed instead. The wake is at the earliest `schedule` time: with the lid closed the Mac
only wakes briefly in the dark, so the ping has to be due the moment it wakes.
"""

from __future__ import annotations

import re
import subprocess
import sys

from .config import HOME

WAKE_FILE = HOME / "wake-time"  # the repeating wake `install` set, as HH:MM:SS


def wake_time(cfg: dict) -> str | None:
    """HH:MM:SS of the earliest scheduled ping, None without a schedule."""
    if not cfg["schedule"]:
        return None
    hh, mm = min(tuple(map(int, at.split(":"))) for at in cfg["schedule"])
    return f"{hh:02d}:{mm:02d}:00"


def repeating_events(sched: str) -> list[str]:
    """The lines under 'Repeating power events:' in `pmset -g sched` output."""
    lines, inside = [], False
    for line in sched.splitlines():
        if line.startswith("Repeating power events"):
            inside = True
        elif inside and line[:1].isspace() and line.strip():
            lines.append(line.strip())
        elif inside:
            break
    return lines


def _matches(event: str, at: str) -> bool:
    """Whether a pmset line like 'wakepoweron at 5:00AM every day' is a daily wake at `at`."""
    hh, mm = int(at[:2]), int(at[3:5])
    twelve = f"{hh % 12 or 12}:{mm:02d}{'AM' if hh < 12 else 'PM'}"
    m = re.match(r"wake\w* at (\S+) every day$", event)
    return bool(m) and m.group(1) in (twelve, at, at[:5])


def _ours(events: list[str]) -> bool:
    try:
        recorded = WAKE_FILE.read_text().strip()
    except OSError:
        return False
    return len(events) == 1 and _matches(events[0], recorded)


def _pmset_sched() -> str:
    return subprocess.run(["pmset", "-g", "sched"], capture_output=True, text=True).stdout


def _sudo_pmset(args: list[str]) -> bool:
    # A password prompt in a terminal; never block without one.
    sudo = ["sudo"] if sys.stdin.isatty() else ["sudo", "-n"]
    return subprocess.run([*sudo, "pmset", *args]).returncode == 0


def set_wake(cfg: dict) -> str:
    at = wake_time(cfg)
    events = repeating_events(_pmset_sched())
    if at is None:
        return clear_wake() or "wake: not changed (no schedule)"
    if events and not _ours(events):
        if len(events) == 1 and _matches(events[0], at):
            return f"wake: a daily wake at {at[:5]} is already set"
        return (f"wake: not changed, you have your own repeating power events ({'; '.join(events)}). "
                f"To wake for the {at[:5]} ping instead: sudo pmset repeat wakeorpoweron MTWRFSU {at}")
    if events and _matches(events[0], at):
        return f"wake: daily at {at[:5]} (unchanged)"
    print(f"wake: setting a daily wake at {at[:5]} for the morning ping (sudo asks for your password)")
    if not _sudo_pmset(["repeat", "wakeorpoweron", "MTWRFSU", at]):
        return f"wake: not set. To set it yourself: sudo pmset repeat wakeorpoweron MTWRFSU {at}"
    WAKE_FILE.write_text(at + "\n")
    if not _ours(repeating_events(_pmset_sched())):
        return f"wake: set daily at {at[:5]}, but `pmset -g sched` doesn't show it as expected"
    return f"wake: daily at {at[:5]}"


def clear_wake() -> str | None:
    """Cancel the repeating wake if `install` set it and nothing else was added since."""
    events = repeating_events(_pmset_sched())
    if not _ours(events):
        return None
    if not _sudo_pmset(["repeat", "cancel"]):
        return "wake: not removed. To remove it yourself: sudo pmset repeat cancel"
    WAKE_FILE.unlink(missing_ok=True)
    return "wake: removed the daily wake"
