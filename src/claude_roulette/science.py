"""Add-on for Claude Science: tell the core which conversation a request is.

Off unless config.json has `"addons": ["science"]`; `install` sets that when Claude Science is installed.

The core (claude_roulette.conversations) keeps each conversation on one account, places
new ones by load and asks before moving a warm one. This add-on only recognises Science's
requests and reads their conversation id.

Science's background service reads its API base URL only from its own environment
(ANTHROPIC_BASE_URL; plain http is allowed for 127.0.0.1), which it inherits from the app,
which inherits it from launchd. No settings file is read, so the URL goes into launchd's
environment with `launchctl setenv`. That doesn't survive a reboot, so `serve` sets it
again at every login, and Science must be quit and reopened to pick it up.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from pathlib import Path

from .addons import Route
from .client_settings import PROXY_VAR, _ours
from .conversations import Conversation
from .router import Router

SURFACE = "operon"  # X-Anthropic-Surface sent by Claude Science
APP = "Claude Science.app"
DAEMON = "claude-science serve"  # the background service that sends the requests
SCIENCE_HOME = Path.home() / ".claude-science"

DEFAULTS = {
    # "pin" keeps each conversation on one account; "observe" passes it through on
    # Science's own login (the core still logs its conversation id and token counts).
    "science_mode": "pin",
}


def app_installed() -> bool:
    return any((d / APP).exists() for d in (Path("/Applications"), Path.home() / "Applications"))


def launchd_url() -> str | None:
    out = subprocess.run(["launchctl", "getenv", PROXY_VAR], capture_output=True, text=True)
    return out.stdout.strip() or None


def connect_launchd(url: str) -> str:
    """Put the proxy URL into launchd's environment, inherited by apps opened from now on."""
    if launchd_url() == url:
        return "unchanged"
    out = subprocess.run(["launchctl", "setenv", PROXY_VAR, url], capture_output=True, text=True)
    return "set" if out.returncode == 0 else f"failed: {out.stderr.strip()}"


def disconnect_launchd(base: str) -> bool:
    """Remove the URL from launchd's environment if it points at this proxy."""
    if not _ours(launchd_url(), base):
        return False  # unset, or set to something else by hand: leave it
    subprocess.run(["launchctl", "unsetenv", PROXY_VAR], capture_output=True)
    return True


def daemon_urls() -> list[str | None]:
    """ANTHROPIC_BASE_URL of each running Science background service (None: not set)."""
    pids = subprocess.run(["pgrep", "-f", DAEMON], capture_output=True, text=True).stdout.split()
    urls = []
    for pid in pids:
        # -E appends the environment to the command line as NAME=value words.
        out = subprocess.run(["ps", "-E", "-ww", "-o", "command=", "-p", pid], capture_output=True, text=True)
        if not out.stdout.strip():
            continue  # exited in the meantime
        words = [w for w in out.stdout.split() if w.startswith(f"{PROXY_VAR}=")]
        urls.append(words[-1].split("=", 1)[1] if words else None)
    return urls


def daemon_warning(url: str, base: str) -> str | None:
    """A warning if a running Science service doesn't use `url`, None if all is well."""
    urls = daemon_urls()
    if any(not _ours(u, base) for u in urls):
        problem = "is running without the proxy, so its requests use its own login"
    elif any(u != url for u in urls):
        problem = "has an outdated proxy URL, so the proxy refuses its requests"
    else:
        return None
    return f"Claude Science {problem}. Quit Claude Science completely and open it again."


def conversation_title(key: str) -> str | None:
    """The conversation's name in Science, read (read-only) from the signed-in org's database.

    The conversation is a root frame; requests carry its id without dashes.
    """
    if not re.fullmatch(r"[0-9a-f]{32}", key.replace("-", "")):
        return None
    k = key.replace("-", "")
    frame_id = f"{k[:8]}-{k[8:12]}-{k[12:16]}-{k[16:20]}-{k[20:]}"
    try:
        org = json.loads((SCIENCE_HOME / "active-org.json").read_text())["org_uuid"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    db = SCIENCE_HOME / "orgs" / str(org) / "operon-cli.db"
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
    try:
        row = con.execute("SELECT name FROM frames WHERE id = ?", (frame_id,)).fetchone()
    finally:
        con.close()
    return row[0] if row and isinstance(row[0], str) and row[0].strip() else None


def science_conversation(headers, body: bytes) -> Conversation | None:
    """The conversation of a Claude Science request, None for any other client.

    Science sends metadata.user_id = '{"session_id": "<root>[:<frame24>][:<lane>]", ...}':
    <root> is the conversation, <frame24> a sub-agent frame, <lane> (absent for the main
    agent) a side task like the title generator. All of them share the root's account.
    """
    if (headers.get("X-Anthropic-Surface") or "").lower() != SURFACE:
        return None
    try:
        parts = json.loads(json.loads(body)["metadata"]["user_id"])["session_id"].split(":")
    except (ValueError, KeyError, TypeError, AttributeError):
        return Conversation("?", "science", lane="?")
    lane = parts[-1] if len(parts) > 1 and len(parts[-1]) <= 6 else "agent"
    return Conversation(parts[0], "science", main=len(parts) == 1, lane=lane)


class ScienceAddon:
    """Loaded by addons.load() when "science" is enabled."""

    defaults = DEFAULTS

    def __init__(self):
        self.cfg: dict | None = None

    def start(self, router: Router) -> None:
        self.cfg = router.cfg

    def route(self, headers, body: bytes) -> Route | None:
        conv = science_conversation(headers, body)
        if conv is None:
            return None
        observe = self.cfg is None or self.cfg["science_mode"] == "observe"
        return Route(conversation=None if conv.key == "?" else conv, passthrough=observe)

    def title(self, key: str) -> str | None:
        return conversation_title(key)

    def snapshot(self) -> dict:
        return {}

    def status(self, snapshot: dict) -> list[str]:
        return []  # Science conversations are listed with all others by `status`
