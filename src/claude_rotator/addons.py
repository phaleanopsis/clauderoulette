"""Optional add-ons, installed as separate distributions (e.g. `claude-rotator[science]`).

An add-on registers a factory under the `claude_rotator.addons` entry-point group:

    [project.entry-points."claude_rotator.addons"]
    science = "claude_rotator_science:ScienceAddon"

The factory is called with no arguments and returns an object with these attributes:

    defaults: dict                         extra config keys and their default values
    start(router) -> None                  called once by `serve` before the proxy starts
    route(headers, body) -> Route | None   claim an inference request (None: not ours)
    snapshot() -> dict                     saved in state.json under state["addons"][name]
    status(snapshot) -> list[str]          extra lines for `status`, given that saved snapshot
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import entry_points

from .config import log
from .conversations import Conversation

GROUP = "claude_rotator.addons"


@dataclass
class Route:
    """What an add-on wants done with one inference request."""

    conversation: Conversation | None = None  # pinned to one account by the core (see conversations.py)
    account: str | None = None  # or force this account; None lets the core decide
    passthrough: bool = False  # send it with the client's own login instead
    done: Callable[[str | None, int, dict], None] | None = None  # (account or None, status, token usage)


_loaded: dict[str, object] | None = None


def load() -> dict[str, object]:
    """Installed add-ons by name; one that fails to import is logged and skipped."""
    global _loaded
    if _loaded is None:
        _loaded = {}
        for ep in entry_points(group=GROUP):
            try:
                _loaded[ep.name] = ep.load()()
            except Exception as e:  # a broken add-on must not take the proxy down
                log(f"add-on {ep.name} not loaded: {e!r}")
    return _loaded


def defaults() -> dict:
    out: dict = {}
    for addon in load().values():
        out.update(addon.defaults)
    return out


def route(headers, body: bytes) -> Route | None:
    for addon in load().values():
        if (r := addon.route(headers, body)) is not None:
            return r
    return None
