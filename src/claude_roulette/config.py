"""Paths, defaults and the on-disk config/state files under ~/.claude-roulette."""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

# A custom home is for tests: it never reaches the Keychain (see credentials._has_keychain).
CUSTOM_HOME = "CLAUDE_ROULETTE_HOME" in os.environ
HOME = Path(os.environ.get("CLAUDE_ROULETTE_HOME", Path.home() / ".claude-roulette"))
CONFIG_FILE = HOME / "config.json"
STATE_FILE = HOME / "state.json"
LOCK_FILE = HOME / ".lock"
TOKENS_FILE = HOME / "tokens.json"  # only used when the Keychain is unavailable
LOGIN_DIR = HOME / "login"  # throwaway CLAUDE_CONFIG_DIRs used by `add`
PRIME_CWD = HOME / "prime"  # pings run here so they don't clutter your project history
KEY_FILE = HOME / "proxy-key"  # the secret clients put in the proxy URL (see proxy_key)
# Where access tokens go. Fixed, not a setting: whoever could change it would receive them.
UPSTREAM = "https://api.anthropic.com"

DEFAULTS = {
    "accounts": [],
    "addons": [],  # optional add-ons to load, e.g. ["science"] (see addons.py)
    "emails": {},
    "primary": None,  # preferred account
    "claude_bin": "claude",
    "proxy_port": 8789,
    "session_cap": 95.0,  # % of 5h window at which an account is considered full
    "weekly_cap": 98.0,  # % of weekly limit at which an account is considered full
    "account_caps": {},  # per-account overrides, e.g. {"main": {"session": 80, "weekly": 90}}
    "weekly_factor_max": 50.0,  # upper bound on the weekly "use it before reset" boost
    "primary_bias": 1.2,  # primary's score is multiplied by this
    # Requests without a conversation id share one current account: it only changes when another
    # scores switch_ratio times higher and min_dwell_s has passed. switch_ratio also gates moving a
    # conversation whose long-TTL cache part may still be warm.
    "switch_ratio": 1.5,
    "min_dwell_s": 900,
    "probe_interval_s": 300,
    "prime_model": "haiku",
    "prime_prompt": "Say hello",
    "schedule": ["07:00"],  # local times to ping the least-used account
    "schedule_grace_min": 120,  # still fire if the Mac wakes up this long after the due time
    "auto_prime": False,  # also ping any account as soon as its 5h window has expired
    "prime_cooldown_s": 900,
    "quiet_hours": [],  # e.g. [0, 7] -> no auto_prime from 00:00 to 06:59
    # Conversation pinning (conversations.py). Claude Code: "pin" keeps each session on one
    # account; "observe" only logs its session ids and uses the global router; "off" ignores ids.
    "code_mode": "pin",
    "pin_active_s": 3600,  # a conversation counts toward its account's load this long after its last turn
    "pin_load_scale": 100000,  # context tokens of active conversations that halve an account's score
    "pin_warn_pct": 85.0,  # ask what to do once a warm conversation's account passes this 5h usage
    "pin_warn_min_context": 20000,  # ...but only for conversations at least this big
    "pin_ask_every_s": 600,  # don't ask about the same conversation more often than this
    "pin_hold_s": 120,  # how long a request waits for an answer before it's sent as before
    "pin_hint_tokens": 150000,  # ask once (Continue / Start fresh) when a context passes this (0: never)
}


# Account names end up in paths (login/NAME) and in `security -i` commands: keep them plain.
ACCOUNT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")


def check_account_name(name: str) -> str:
    if not ACCOUNT_NAME.fullmatch(name):
        raise SystemExit(f"invalid account name {name!r}: use 1-32 letters, digits, '-' or '_'")
    return name


def ensure_home() -> None:
    """Create ~/.claude-roulette readable by its owner only (it holds emails, logs and,
    without a Keychain, the tokens), and tighten it if an older version left it open."""
    HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    if HOME.stat().st_mode & 0o077:
        HOME.chmod(0o700)


def open_private(path: Path, flags: int):
    """os.open opener that creates files as 0600 from the start."""
    return os.open(path, flags, 0o600)


def proxy_key() -> str:
    """The proxy's secret, created on first use. Only requests whose URL carries it get an
    account's token, so other processes, containers and web pages that can reach the port can't."""
    ensure_home()
    if not KEY_FILE.exists():
        tmp = KEY_FILE.with_name(f"{KEY_FILE.name}.{os.getpid()}")
        with open(tmp, "w", opener=open_private) as f:
            f.write(secrets.token_urlsafe(24))
        try:
            os.link(tmp, KEY_FILE)  # fails if another process created it first: theirs wins
        except FileExistsError:
            pass
        finally:
            tmp.unlink()
    return KEY_FILE.read_text().strip()


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data) -> None:
    ensure_home()
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)  # a leftover could carry looser permissions
    with open(tmp, "w", opener=open_private) as f:
        f.write(json.dumps(data, indent=2, default=str))
    tmp.replace(path)


def load_config() -> dict:
    from . import addons  # imported here: add-ons import this module

    cfg = {**DEFAULTS, **addons.defaults(), **load_json(CONFIG_FILE, {})}
    if not cfg["primary"] and cfg["accounts"]:
        cfg["primary"] = cfg["accounts"][0]
    return cfg


def save_config_keys(**changes) -> None:
    """Persist only the given keys, so untouched settings keep following DEFAULTS."""
    saved = load_json(CONFIG_FILE, {})
    saved.update(changes)
    save_json(CONFIG_FILE, saved)


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", file=sys.stderr, flush=True)


@contextmanager
def file_lock():
    """Serialises token refreshes across `serve`, `status` and `prime` processes."""
    ensure_home()
    with open(LOCK_FILE, "w", opener=open_private) as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
