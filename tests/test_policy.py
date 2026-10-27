from datetime import datetime, timedelta

import pytest

from claude_roulette.config import DEFAULTS
from claude_roulette.router import Router
from claude_roulette import usage as usage_mod
from claude_roulette.usage import Usage, least_used, left_today, now, score, usage_from_headers

CFG = {**DEFAULTS, "accounts": ["a", "b"], "primary": "a"}


def usage(name, s_pct, s_hours, w_pct, w_days):
    return Usage(name, session_pct=s_pct, session_reset=now() + timedelta(hours=s_hours),
                 weekly_pct=w_pct, weekly_reset=now() + timedelta(days=w_days))


def test_full_accounts_are_excluded():
    assert score(usage("a", 96, 1, 10, 3), CFG) is None
    assert score(usage("a", 10, 1, 99, 3), CFG) is None
    assert score(Usage("a", error="boom"), CFG) is None


def test_account_caps_reserve_capacity():
    cfg = {**CFG, "account_caps": {"a": {"session": 80}}}
    assert score(usage("a", 85, 1, 10, 3), cfg) is None
    assert score(usage("b", 85, 1, 10, 3), cfg) is not None


RESET = datetime(2026, 11, 15, 5).astimezone()  # the week runs Sun 8 Nov 05:00 -> Sun 15 Nov 05:00
RESERVE = {**CFG, "weekly_cap": 100.0, "account_caps": {"a": {"daily_reserve": 5}}}


@pytest.fixture
def clock(monkeypatch):
    def at(day, hour):
        t = datetime(2026, 11, day, hour).astimezone()
        monkeypatch.setattr(usage_mod, "now", lambda: t)
    return at


def weekly(name, w_pct, s_pct=None):
    return Usage(name, session_pct=s_pct, weekly_pct=w_pct, weekly_reset=RESET,
                 session_reset=None if s_pct is None else usage_mod.now() + timedelta(hours=2))


def test_left_today_accrues_a_day_at_a_time_less_todays_reserve(clock):
    clock(8, 12)  # day 1
    assert abs(left_today(weekly("a", 0), RESERVE) - (100 / 7 - 5)) < 0.01
    assert abs(left_today(weekly("b", 0), RESERVE) - 100 / 7) < 0.01  # no reserve
    clock(9, 4)  # still day 1 before 5am
    assert abs(left_today(weekly("a", 0), RESERVE) - (100 / 7 - 5)) < 0.01
    clock(9, 12)  # day 2, nothing used on day 1: it carries over
    assert abs(left_today(weekly("a", 0), RESERVE) - (200 / 7 - 5)) < 0.01
    assert left_today(weekly("a", 30), RESERVE) < 0  # overspent
    clock(15, 3)  # last day: everything but today's reserve
    assert abs(left_today(weekly("a", 60), RESERVE) - 35) < 0.01


def test_daily_reserve_is_never_used(clock):
    clock(11, 12)  # 4 days left counting today: 20% stays free
    assert score(weekly("a", 79), RESERVE) == 0  # today's share used up, still usable
    assert score(weekly("a", 80), RESERVE) is None


def test_score_follows_left_today_then_session_headroom(clock):
    clock(9, 12)
    more_left = score(weekly("a", 0, s_pct=50), RESERVE)
    less_left = score(weekly("b", 20, s_pct=0), RESERVE)
    assert more_left > less_left
    fresh = score(weekly("b", 10, s_pct=10), RESERVE)
    busy = score(weekly("b", 10, s_pct=80), RESERVE)
    assert fresh > busy


def test_router_falls_back_to_primary_when_all_used_up(clock):
    clock(9, 12)
    r = Router({**RESERVE, "primary": "b"})
    r.usages = {"a": weekly("a", 50), "b": weekly("b", 50)}
    assert r.scores(["a", "b"]) == {"a": 0.0, "b": 1.0}
    r.current = None
    assert r.choose() == "b"


def test_least_used_prefers_lower_weekly():
    assert least_used([usage("a", 0, 1, 50, 3), usage("b", 90, 1, 20, 3)], CFG).name == "b"


def test_router_is_sticky_until_ratio_and_dwell():
    r = Router(CFG)
    r.usages = {"a": usage("a", 50, 4, 50, 5), "b": usage("b", 10, 1, 10, 5)}
    r.switched_at = 0  # dwell time long over
    assert r.choose() == "b"
    r.usages["a"] = usage("a", 0, 0.3, 0, 0.5)  # a now much better, but we just switched
    assert r.choose() == "b"
    r.switched_at = 0
    assert r.choose() == "a"


def test_router_skips_rate_limited():
    r = Router(CFG)
    r.observe("a", 429, {"anthropic-ratelimit-unified-reset": str(now().timestamp() + 600)})
    assert r.choose() == "b"


def test_usage_from_headers():
    reset = now().timestamp() + 3600
    u = usage_from_headers("a", {"Anthropic-Ratelimit-Unified-5h-Utilization": "0.25",
                                 "anthropic-ratelimit-unified-5h-reset": str(reset)}, None)
    assert u.session_pct == 25 and abs(u.session_reset.timestamp() - reset) < 1 and u.source == "live"
    assert usage_from_headers("a", {"x": "y"}, None) is None
