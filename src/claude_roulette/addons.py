"""Optional add-ons. They ship with the package but are off until listed in config.json:

    "addons": ["science"]

An add-on is a class in its own module, listed in BUILTIN. It's created with no arguments
and has these attributes:

    defaults: dict                         extra config keys and their default values
    start(router) -> None                  called once by `serve` before the proxy starts
    route(headers, body) -> Route | None   claim an inference request (None: not ours)
    snapshot() -> dict                     saved in state.json under state["addons"][name]
    status(snapshot) -> list[str]          extra lines for `status`, given that saved snapshot
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module

from .config import CONFIG_FILE, load_json, log
from .conversations import Conversation

BUILTIN = {"science": "claude_roulette.science:ScienceAddon"}  # name -> module:class


@dataclass
class Route:
    """What an add-on wants done with one inference request."""

    conversation: Conversation | None = None  # pinned to one account by the core (see conversations.py)
    account: str | None = None  # or force this account; None lets the core decide
    passthrough: bool = False  # send it with the client's own login instead
    done: Callable[[str | None, int, dict], None] | None = None  # (account or None, status, token usage)


_loaded: dict[str, object] | None = None


def load() -> dict[str, object]:
    """Enabled add-ons by name; an unknown or broken one is logged and skipped."""
    global _loaded
    if _loaded is None:
        _loaded = {}
        # Read straight from the file: load_config() needs the add-ons' defaults first.
        for name in load_json(CONFIG_FILE, {}).get("addons", []):
            try:
                module, cls = BUILTIN[name].split(":")
                _loaded[name] = getattr(import_module(module), cls)()
            except Exception as e:  # a broken add-on must not take the proxy down
                log(f"add-on {name} not loaded: {e!r}")
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
