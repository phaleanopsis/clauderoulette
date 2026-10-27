"""Per-account usage (5h session and weekly) and the policy that scores accounts."""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from .config import UPSTREAM
from .credentials import OAUTH_BETA, USER_AGENT, AuthDead, access_token


def now() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


@dataclass
class Usage:
    name: str
    session_pct: float | None = None
    session_reset: datetime | None = None
    weekly_pct: float | None = None
    weekly_reset: datetime | None = None
    error: str | None = None
    source: str = "-"  # "probe" or "live" (response headers)

    @property
    def window_active(self) -> bool:
        return self.session_reset is not None and self.session_reset > now()

    @property
    def has_data(self) -> bool:
        return self.session_pct is not None or self.weekly_pct is not None


def probe(name: str, cfg: dict) -> Usage:
    """Read usage from the (undocumented) endpoint behind Claude Code's /usage screen."""
    try:
        token = access_token(name)
    except AuthDead as e:
        return Usage(name, error=str(e), source="dead")
    except OSError as e:
        return Usage(name, error=str(e))
    req = urllib.request.Request(UPSTREAM + "/api/oauth/usage", headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": OAUTH_BETA,
        "User-Agent": USER_AGENT,
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        return Usage(name, error=f"usage HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        return Usage(name, error=f"usage: {e}")
    return usage_from_payload(name, data)


def usage_to_json(u: Usage) -> dict:
    """Usage with ISO timestamps (seconds precision), as read by `status` and the menu bar app."""
    d = asdict(u)
    for k in ("session_reset", "weekly_reset"):
        if d[k] is not None:
            d[k] = d[k].isoformat(timespec="seconds")
    return d


def usage_from_payload(name: str, data: dict) -> Usage:
    five = data.get("five_hour") or {}
    week = data.get("seven_day") or {}
    return Usage(
        name,
        session_pct=five.get("utilization"),
        session_reset=parse_ts(five.get("resets_at")),
        weekly_pct=week.get("utilization"),
        weekly_reset=parse_ts(week.get("resets_at")),
        source="probe",
    )


def usage_from_headers(name: str, headers: dict[str, str], prev: Usage | None) -> Usage | None:
    """Pull live usage out of the rate-limit headers on a /v1/messages response.

    Header names and units (utilization as a 0-1 fraction, reset as unix seconds) are
    undocumented; anything missing is left unchanged. Run `serve --debug` to see them.
    """
    h = {k.lower(): v for k, v in headers.items()}
    pre = "anthropic-ratelimit-unified-"

    def pct(key):
        v = h.get(pre + key)
        return None if v is None else float(v) * 100

    def ts(key):
        v = h.get(pre + key)
        return None if v is None else datetime.fromtimestamp(float(v), timezone.utc)

    try:
        fields = dict(session_pct=pct("5h-utilization"), session_reset=ts("5h-reset"),
                      weekly_pct=pct("7d-utilization"), weekly_reset=ts("7d-reset"))
    except ValueError:
        return None
    if all(v is None for v in fields.values()):
        return None
    base = asdict(prev) if prev else {"name": name}
    base.update({k: v for k, v in fields.items() if v is not None}, error=None, source="live")
    return Usage(**base)


# --------------------------------------------------------------------------- selection policy

def caps(name: str, cfg: dict) -> tuple[float, float]:
    """(session_cap, weekly_cap) for an account; lower caps reserve capacity for other use."""
    own = cfg["account_caps"].get(name, {})
    return own.get("session", cfg["session_cap"]), own.get("weekly", cfg["weekly_cap"])


def daily_reserve(name: str, cfg: dict) -> float:
    """Weekly % kept free for every day left in the week, e.g. for the chat app."""
    return cfg["account_caps"].get(name, {}).get("daily_reserve", cfg["daily_reserve"])


DAY_START_HOUR = 5  # days run from 5am to 5am, local time


def day_start(t: datetime) -> datetime:
    """The 5am that began t's day."""
    local = t.astimezone()
    start = local.replace(hour=DAY_START_HOUR, minute=0, second=0, microsecond=0)
    return start if local >= start else start - timedelta(days=1)


def days_left(reset: datetime) -> int:
    """Days until the weekly reset, counting today as a whole day."""
    return max(math.ceil((reset - day_start(now())) / timedelta(days=1)), 1)


def left_today(u: Usage, cfg: dict) -> float | None:
    """Weekly % the account may still use today, negative once today's share is overspent.

    The week's cap accrues evenly, a day at a time from 5am: by the end of today the account
    may have used the share of the week up to then, less today's daily reserve. Unused days
    carry over, and so does a reserve the other day didn't need; overspending comes out of the
    next days. Today's reserve stays free whatever has carried over.
    """
    if u.weekly_reset is None or u.weekly_reset <= now():
        return None
    week_start = u.weekly_reset - timedelta(days=7)
    until = min(day_start(now()) + timedelta(days=1), u.weekly_reset)
    accrued = min(max((until - week_start) / timedelta(days=7), 0.0), 1.0)
    w_cap = caps(u.name, cfg)[1]
    return w_cap * accrued - daily_reserve(u.name, cfg) - (u.weekly_pct or 0.0)


def score(u: Usage, cfg: dict) -> float | None:
    """Higher = use this account first. 0 = today's share is used up. None = not usable.

    Mainly what's left of today's share (left_today), times the 5h window's headroom, so when
    both accounts have about as much left today, work spreads to the one further from its 5h
    limit. Never usable past the cap less the daily reserve for every day left, today
    included as a whole day.
    """
    if u.error:
        return None
    s_cap, w_cap = caps(u.name, cfg)
    s_pct, w_pct = u.session_pct or 0.0, u.weekly_pct or 0.0
    left = left_today(u, cfg)
    if left is None:  # no weekly window running
        if w_pct >= w_cap:
            return None
        left = w_cap - w_pct
    elif w_pct >= w_cap - daily_reserve(u.name, cfg) * days_left(u.weekly_reset):
        return None
    if u.window_active:
        if s_pct >= s_cap:
            return None
        headroom = (s_cap - s_pct) / s_cap
    else:
        headroom = 1.0
    return max(left, 0.0) * headroom


def least_used(usages: list[Usage], cfg: dict) -> Usage | None:
    ok = [u for u in usages if not u.error and (u.weekly_pct or 0) < caps(u.name, cfg)[1]]
    return min(ok, key=lambda u: (u.weekly_pct or 0, u.session_pct or 0), default=None)
