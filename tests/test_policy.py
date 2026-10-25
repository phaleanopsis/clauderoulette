from datetime import timedelta

from claude_roulette.config import DEFAULTS
from claude_roulette.router import Router
from claude_roulette.usage import Usage, least_used, now, score, usage_from_headers

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


def test_expiring_capacity_scores_higher():
    soon = score(usage("a", 10, 0.5, 10, 3), CFG)
    later = score(usage("a", 10, 4.5, 10, 3), CFG)
    assert soon > later


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
