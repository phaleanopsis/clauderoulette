"""macOS LaunchAgents for `serve` and the menu bar app."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import time
from importlib.resources import as_file, files
from pathlib import Path

from . import __version__

SERVE_LABEL = "com.user.claude-rotator"
BAR_LABEL = "com.user.claude-rotator.bar"
AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
APP_DIR = Path.home() / "Applications" / "Claude Rotator.app"
APP_EXE = APP_DIR / "Contents" / "MacOS" / "ClaudeRotatorBar"


def cli_command() -> list[str]:
    """argv that runs this installation's CLI; launchd and the menu bar app use it."""
    return [sys.executable, "-m", "claude_rotator"]


def build_app() -> None:
    """Compile the SwiftUI menu bar app into ~/Applications (needs Xcode command line tools)."""
    if shutil.which("xcrun") is None:
        sys.exit("xcrun not found; install the command line tools with `xcode-select --install`")
    APP_EXE.parent.mkdir(parents=True, exist_ok=True)
    with open(APP_DIR / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump({
            "CFBundleName": "Claude Rotator",
            "CFBundleIdentifier": BAR_LABEL,
            "CFBundleExecutable": APP_EXE.name,
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": __version__,
            "LSMinimumSystemVersion": "13.0",
            "LSUIElement": True,  # menu bar only, no Dock icon
        }, f)
    with as_file(files("claude_rotator") / "menubar" / "ClaudeRotatorBar.swift") as swift_src:
        subprocess.run(["xcrun", "swiftc", "-parse-as-library", "-swift-version", "5", "-O",
                        "-target", f"{os.uname().machine}-apple-macos13", "-o", str(APP_EXE), str(swift_src)],
                       check=True)
    subprocess.run(["codesign", "--force", "--sign", "-", str(APP_DIR)], check=True, capture_output=True)


def agent_plist(label: str, program: list[str], keep_alive, log_file: Path | None) -> dict:
    # launchd starts agents with a minimal PATH; keep the user's so `claude` is found for pings.
    path = ":".join(dict.fromkeys(os.environ.get("PATH", "").split(":") +
                                  ["/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local/bin"),
                                   "/usr/bin", "/bin", "/usr/sbin", "/sbin"]))
    plist = {
        "Label": label,
        "ProgramArguments": program,
        "EnvironmentVariables": {"PATH": path},
        "RunAtLoad": True,  # every login, i.e. after each restart of the Mac
        "KeepAlive": keep_alive,
        "ThrottleInterval": 10,
        "LimitLoadToSessionType": "Aqua",
    }
    if log_file:
        plist["StandardOutPath"] = plist["StandardErrorPath"] = str(log_file)
    return plist


def bootout(label: str) -> None:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True)


def launchctl_reload(label: str, plist_path: Path) -> None:
    domain = f"gui/{os.getuid()}"
    bootout(label)
    # bootout returns before the old job is gone; bootstrapping too early fails with error 5.
    for _ in range(20):
        if subprocess.run(["launchctl", "print", f"{domain}/{label}"], capture_output=True).returncode != 0:
            break
        time.sleep(0.25)
    for _ in range(3):
        out = subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], capture_output=True, text=True)
        if out.returncode == 0:
            return
        time.sleep(1)
    sys.exit(f"launchctl bootstrap {label} failed: {out.stderr.strip()}")


def write_agent(label: str, plist: dict) -> Path:
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    path = AGENTS_DIR / f"{label}.plist"
    with open(path, "wb") as f:
        plistlib.dump(plist, f)
    launchctl_reload(label, path)
    return path


def remove_agents() -> None:
    for label in (BAR_LABEL, SERVE_LABEL):
        bootout(label)
        (AGENTS_DIR / f"{label}.plist").unlink(missing_ok=True)
    shutil.rmtree(APP_DIR, ignore_errors=True)


def service_mismatch() -> str | None:
    """Why the installed service isn't running this installation, if it isn't."""
    try:
        with open(AGENTS_DIR / f"{SERVE_LABEL}.plist", "rb") as f:
            program = plistlib.load(f).get("ProgramArguments", [])
    except (FileNotFoundError, plistlib.InvalidFileException):
        return None
    if program[1:3] != ["-m", "claude_rotator"]:
        return f"the background service was installed by an older version ({' '.join(program)})"
    if not Path(program[0]).exists():
        return f"the background service runs {program[0]}, which no longer exists"
    if program[0] != sys.executable:
        return f"the background service runs a different installation ({program[0]})"
    return None
