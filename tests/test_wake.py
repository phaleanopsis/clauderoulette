import subprocess

import pytest

from claude_roulette import wake
from claude_roulette.config import DEFAULTS

NONE = "Scheduled power events:\n [0]  wake at 10/25/2026 23:00:00 by 'com.apple.alarm'\n"


def sched(*repeating):
    return "Repeating power events:\n" + "".join(f"  {e}\n" for e in repeating) + NONE


class FakePmset:
    """Stands in for pmset and sudo: holds the repeating events as pmset prints them."""

    def __init__(self, *repeating):
        self.repeating = list(repeating)
        self.calls = []

    def run(self, cmd, **kw):
        if cmd == ["pmset", "-g", "sched"]:
            out = sched(*self.repeating) if self.repeating else NONE
            return subprocess.CompletedProcess(cmd, 0, out, "")
        args = cmd[cmd.index("pmset") + 1:]
        self.calls.append(args)
        if args[:2] == ["repeat", "wakeorpoweron"]:
            hh, mm = int(args[3][:2]), int(args[3][3:5])
            self.repeating = [f"wakepoweron at {hh % 12 or 12}:{mm:02d}{'AM' if hh < 12 else 'PM'} every day"]
        elif args == ["repeat", "cancel"]:
            self.repeating = []
        return subprocess.CompletedProcess(cmd, 0, "", "")


@pytest.fixture
def pmset(monkeypatch):
    wake.WAKE_FILE.unlink(missing_ok=True)
    fake = FakePmset()
    monkeypatch.setattr(wake.subprocess, "run", fake.run)
    yield fake
    wake.WAKE_FILE.unlink(missing_ok=True)


def cfg(*schedule):
    return {**DEFAULTS, "schedule": list(schedule)}


def test_wakes_at_the_earliest_ping():
    assert wake.wake_time(cfg("07:00", "05:30")) == "05:30:00"
    assert wake.wake_time(cfg()) is None


def test_parses_repeating_events():
    assert wake.repeating_events(sched("wakepoweron at 5:00AM every day", "sleep at 11:00PM weekdays only")) == [
        "wakepoweron at 5:00AM every day", "sleep at 11:00PM weekdays only"]
    assert wake.repeating_events(NONE) == []


def test_sets_moves_and_removes_its_own_wake(pmset):
    assert wake.set_wake(cfg("05:00")) == "wake: daily at 05:00"
    assert wake.set_wake(cfg("05:00")) == "wake: daily at 05:00 (unchanged)"
    assert wake.set_wake(cfg("17:30")) == "wake: daily at 17:30"
    assert pmset.repeating == ["wakepoweron at 5:30PM every day"]
    assert wake.clear_wake() == "wake: removed the daily wake"
    assert pmset.repeating == [] and not wake.WAKE_FILE.exists()


def test_leaves_your_own_repeating_events_alone(pmset):
    pmset.repeating = ["wakepoweron at 6:00AM every day", "sleep at 11:00PM every day"]
    report = wake.set_wake(cfg("05:00"))
    assert "not changed" in report and "sudo pmset repeat wakeorpoweron MTWRFSU 05:00:00" in report
    assert wake.clear_wake() is None
    assert pmset.calls == []


def test_an_empty_schedule_removes_its_wake(pmset):
    wake.set_wake(cfg("05:00"))
    assert wake.set_wake(cfg()) == "wake: removed the daily wake"
