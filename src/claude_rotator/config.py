"""Paths, defaults and the on-disk config/state files under ~/.claude-rotator."""

from __future__ import annotations

import fcntl
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

HOME = Path(os.environ.get("CLAUDE_ROTATOR_HOME", Path.home() / ".claude-rotator"))
CONFIG_FILE = HOME / "config.json"
STATE_FILE = HOME / "state.json"
LOCK_FILE = HOME / ".lock"
TOKENS_FILE = HOME / "tokens.json"  # only used when the Keychain is unavailable
LOGIN_DIR = HOME / "login"  # throwaway CLAUDE_CONFIG_DIRs used by `add`
PRIME_CWD = HOME / "prime"  # pings run here so they don't clutter your project history

DEFAULTS = {
    "accounts": [],
    "emails": {},
    "primary": None,  # preferred account
    "claude_bin": "claude",
    "upstream": "https://api.anthropic.com",
    "proxy_port": 8789,
    "session_cap": 95.0,  # % of 5h window at which an account is considered full
    "weekly_cap": 98.0,  # % of weekly limit at which an account is considered full
    "account_caps": {},  # per-account overrides, e.g. {"main": {"session": 80, "weekly": 90}}
    "weekly_factor_max": 50.0,  # upper bound on the weekly "use it before reset" boost
    "primary_bias": 1.2,  # primary's score is multiplied by this
    "switch_ratio": 1.5,  # only leave the current account if another scores this much higher...
    "min_dwell_s": 900,  # ...and we've been on the current one at least this long (prompt cache)
    "probe_interval_s": 300,
    "prime_model": "haiku",
    "prime_prompt": "Say hello",
    "schedule": ["07:00"],  # local times to ping the least-used account
    "schedule_grace_min": 120,  # still fire if the Mac wakes up this long after the due time
    "auto_prime": False,  # also ping any account as soon as its 5h window has expired
    "prime_cooldown_s": 900,
    "quiet_hours": [],  # e.g. [0, 7] -> no auto_prime from 00:00 to 06:59
}


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(path)


def load_config() -> dict:
    cfg = {**DEFAULTS, **load_json(CONFIG_FILE, {})}
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
    HOME.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
