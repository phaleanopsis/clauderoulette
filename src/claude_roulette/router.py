"""Chooses which account serves each request."""

from __future__ import annotations

import threading
import time
from datetime import datetime

from .config import log
from .usage import Usage, now, score, usage_from_headers, usage_to_json


class Router:
    """Holds live usage for every account and decides which token each request gets.

    Sticky on purpose: switching accounts throws away the prompt cache, so the first
    request on the new account bills the whole context again.
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.usages: dict[str, Usage] = {n: Usage(n) for n in cfg["accounts"]}
        self.blocked_until: dict[str, float] = {}
        self.dead: dict[str, str] = {}
        self.current: str | None = cfg["primary"]
        self.switched_at = 0.0
        self.requests: dict[str, int] = {}
        self.last_request_at: str | None = None

    def usable(self, name: str) -> bool:
        return name not in self.dead and self.blocked_until.get(name, 0) <= time.time()

    def scores(self, names) -> dict[str, float]:
        out = {}
        for n in names:
            if not self.usable(n):
                continue
            s = score(self.usages.get(n) or Usage(n), self.cfg)
            if s is not None:
                out[n] = s * (self.cfg["primary_bias"] if n == self.cfg["primary"] else 1.0)
        return out

    def choose(self, exclude=()) -> str | None:
        with self.lock:
            names = [n for n in self.cfg["accounts"] if n not in exclude and n not in self.dead]
            if not names:
                return None
            scores = self.scores(names)
            if not scores:  # nothing known-good: let upstream decide
                return min(names, key=lambda n: self.blocked_until.get(n, 0))
            best = max(scores, key=scores.get)
            cur = self.current
            if cur in scores and cur != best:
                stay = (scores[best] < scores[cur] * self.cfg["switch_ratio"]
                        or time.time() - self.switched_at < self.cfg["min_dwell_s"])
                if stay:
                    return cur
            if best != cur:
                log(f"switching {cur} -> {best} (scores {', '.join(f'{k}={v:.0f}' for k, v in scores.items())})")
                self.current, self.switched_at = best, time.time()
            return best

    def mark_dead(self, name: str, reason: str) -> None:
        with self.lock:
            if name not in self.dead:
                log(f"{name} disabled: {reason}")
            self.dead[name] = reason

    def observe(self, name: str, status: int, headers: dict[str, str]) -> None:
        with self.lock:
            self.requests[name] = self.requests.get(name, 0) + 1
            self.last_request_at = now().isoformat(timespec="seconds")
            u = usage_from_headers(name, headers, self.usages.get(name))
            if u:
                self.usages[name] = u
            if status == 429:
                h = {k.lower(): v for k, v in headers.items()}
                reset = h.get("anthropic-ratelimit-unified-reset")
                try:
                    until = float(reset) if reset else time.time() + 900
                except ValueError:
                    until = time.time() + 900
                self.blocked_until[name] = until
                log(f"{name} rate-limited until {datetime.fromtimestamp(until):%H:%M}")

    def merge_probe(self, u: Usage) -> None:
        with self.lock:
            prev = self.usages.get(u.name)
            if u.error and prev and prev.has_data:
                return  # keep the last good numbers
            self.usages[u.name] = u

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "current": self.current,
                "usages": {n: usage_to_json(u) for n, u in self.usages.items()},
                "blocked_until": dict(self.blocked_until),
                "dead": dict(self.dead),
                "requests": dict(self.requests),
                "last_request_at": self.last_request_at,
                "updated_at": now().isoformat(timespec="seconds"),
            }
