"""Add-on for Claude Science: tell the core which conversation a request is.

Off unless config.json has `"addons": ["science"]`; `install` sets that when Claude Science is installed.

The core (claude_roulette.conversations) keeps each conversation on one account, places
new ones by load and asks before moving a warm one. This add-on only recognises Science's
requests and reads their conversation id.
"""

from __future__ import annotations

import json
from pathlib import Path

from .addons import Route
from .conversations import Conversation
from .router import Router

SURFACE = "operon"  # X-Anthropic-Surface sent by Claude Science
APP = "Claude Science.app"

DEFAULTS = {
    # "pin" keeps each conversation on one account; "observe" passes it through on
    # Science's own login (the core still logs its conversation id and token counts).
    "science_mode": "pin",
}


def app_installed() -> bool:
    return any((d / APP).exists() for d in (Path("/Applications"), Path.home() / "Applications"))


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

    def snapshot(self) -> dict:
        return {}

    def status(self, snapshot: dict) -> list[str]:
        return []  # Science conversations are listed with all others by `status`
