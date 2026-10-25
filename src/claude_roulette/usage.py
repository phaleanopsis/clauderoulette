"""Per-account usage (5h session and weekly) and the policy that scores accounts."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

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


def score(u: Usage, cfg: dict) -> float | None:
    """Higher = use this account first. None = not usable right now.

    "Use it or lose it": an active 5h window with lots of headroom and little time left is
    the most urgent capacity to burn. An idle account scores a flat 10 (using it opens a
    fresh window). Weekly pacing scales everything: ahead of an even 100%/7 per day is
    damped, behind it is boosted. Headroom is measured up to the account's own caps.
    """
    if u.error:
        return None
    s_cap, w_cap = caps(u.name, cfg)
    s_pct, w_pct = u.session_pct or 0.0, u.weekly_pct or 0.0
    if w_pct >= w_cap:
        return None
    if u.window_active:
        if s_pct >= s_cap:
            return None
        hours_left = max((u.session_reset - now()).total_seconds() / 3600, 0.25)
        session_urgency = (s_cap - s_pct) / hours_left
    else:
        session_urgency = 10.0

    if u.weekly_reset and u.weekly_reset > now():
        days_left = max((u.weekly_reset - now()).total_seconds() / 86400, 0.1)
        weekly_factor = min(cfg["weekly_factor_max"], ((w_cap - w_pct) / days_left) / (100 / 7))
    else:
        weekly_factor = 1.0
    return session_urgency * weekly_factor


def least_used(usages: list[Usage], cfg: dict) -> Usage | None:
    ok = [u for u in usages if not u.error and (u.weekly_pct or 0) < caps(u.name, cfg)[1]]
    return min(ok, key=lambda u: (u.weekly_pct or 0, u.session_pct or 0), default=None)
