"""Pings that start an account's 5h window: the daily schedule and auto-prime."""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from datetime import datetime, timedelta

from .config import HOME, PRIME_CWD, ensure_home, log
from .credentials import AuthDead, access_token
from .usage import Usage, caps, least_used


def claude_env(**extra) -> dict:
    env = dict(os.environ)
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"):
        env.pop(k, None)  # each of these would change which credential Claude Code uses
    env.update(extra)
    return env


def prime_account(name: str, cfg: dict) -> tuple[bool, str]:
    """One tiny Haiku prompt, sent by Claude Code itself with that account's access token.

    It runs with a fresh, empty CLAUDE_CONFIG_DIR: with ~/.claude it would load your hooks,
    MCP servers and plugins, which inherit the token from its environment, and the proxy's
    ANTHROPIC_BASE_URL, which could send the ping on another account.
    """
    ensure_home()
    PRIME_CWD.mkdir(parents=True, exist_ok=True)
    cmd = [cfg["claude_bin"], "-p", cfg["prime_prompt"], "--model", cfg["prime_model"]]
    try:
        with tempfile.TemporaryDirectory(prefix="prime-config-", dir=HOME) as config_dir:
            env = claude_env(CLAUDE_CODE_OAUTH_TOKEN=access_token(name), CLAUDE_CONFIG_DIR=config_dir)
            out = subprocess.run(cmd, env=env, cwd=PRIME_CWD, capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, FileNotFoundError, AuthDead, OSError) as e:
        return False, str(e)
    return out.returncode == 0, (out.stdout or out.stderr).strip()[:200]


def in_quiet_hours(cfg: dict) -> bool:
    if not cfg["quiet_hours"]:
        return False
    start, end = cfg["quiet_hours"]
    h = datetime.now().hour
    return start <= h < end if start <= end else (h >= start or h < end)


def run_schedule(cfg: dict, usages: list[Usage], state: dict) -> None:
    """At each configured time, ping the least-used account once per day."""
    done = state.setdefault("schedule_done", {})
    local = datetime.now()
    for at in cfg["schedule"]:
        hh, mm = map(int, at.split(":"))
        due = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if done.get(at) == local.date().isoformat():
            continue
        if not due <= local < due + timedelta(minutes=cfg["schedule_grace_min"]):
            continue
        done[at] = local.date().isoformat()
        target = least_used(usages, cfg)
        if target is None:
            log(f"{at} ping: no eligible account")
        elif target.window_active:
            log(f"{at} ping: {target.name} already has an active window, skipping")
        else:
            ok, msg = prime_account(target.name, cfg)
            log(f"{at} ping {target.name}: {'ok' if ok else 'FAILED'} {msg!r}")


def next_ping_text(cfg: dict, state: dict) -> str | None:
    """Human-readable time of the next scheduled ping, e.g. 'tomorrow 07:00'."""
    local, done = datetime.now(), state.get("schedule_done", {})
    upcoming = []
    for at in cfg["schedule"]:
        hh, mm = map(int, at.split(":"))
        due = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if done.get(at) == local.date().isoformat() or local >= due + timedelta(minutes=cfg["schedule_grace_min"]):
            due += timedelta(days=1)
        upcoming.append(due)
    if not upcoming:
        return None
    nxt = min(upcoming)
    return f"{'today' if nxt.date() == local.date() else 'tomorrow'} {nxt:%H:%M}"


def run_auto_prime(cfg: dict, usages: list[Usage], state: dict) -> None:
    if not cfg["auto_prime"] or in_quiet_hours(cfg):
        return
    last = state.setdefault("last_prime", {})
    for u in usages:
        if u.error or u.window_active or (u.weekly_pct or 0) >= caps(u.name, cfg)[1]:
            continue
        if time.time() - last.get(u.name, 0) < cfg["prime_cooldown_s"]:
            continue
        ok, msg = prime_account(u.name, cfg)
        log(f"auto-prime {u.name}: {'ok' if ok else 'FAILED'} {msg!r}")
        if ok:
            last[u.name] = time.time()
