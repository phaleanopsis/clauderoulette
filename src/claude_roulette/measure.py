"""How much each kind of token counts toward the subscription limits.

Anthropic doesn't document how cache reads, cache writes, uncached input and output weigh
against the 5h and weekly limits. Every proxied response carries the account's current
utilization in its rate-limit headers, so `serve` appends one row per response to
usage-log.jsonl, and `claude-roulette measure` fits

    Δ utilization (this response -> the next one on the same account)
        ≈ a·input + b·cache_read + c·cache_creation + d·output      (of this response's tokens)

by least squares, per model family. Traffic that doesn't go through the proxy (claude.ai,
the desktop app) also moves utilization and shows up as noise.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import defaultdict

from .config import HOME

LOG_FILE = HOME / "usage-log.jsonl"
FIELDS = ("input", "cache_read", "cache_creation", "output")
_lock = threading.Lock()


def family(model: str) -> str:
    m = re.search(r"(opus|sonnet|haiku|fable)", model or "")
    return m.group(1) if m else "other"


def record(account: str, model: str, usage: dict[str, int], headers: dict[str, str]) -> None:
    h = {k.lower(): v for k, v in headers.items()}
    pre = "anthropic-ratelimit-unified-"
    row = {"t": round(time.time(), 3), "account": account, "model": model,
           **{f: usage.get(f, 0) for f in FIELDS},
           "h5": h.get(pre + "5h-utilization"), "d7": h.get(pre + "7d-utilization"),
           "h5_reset": h.get(pre + "5h-reset"), "d7_reset": h.get(pre + "7d-reset")}
    with _lock, open(LOG_FILE, "a") as f:
        f.write(json.dumps(row) + "\n")


def load_rows() -> list[dict]:
    try:
        with open(LOG_FILE) as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def samples(rows: list[dict], window: str) -> list[tuple[str, list[float], float]]:
    """(model family, tokens, Δ%) for consecutive responses on one account within one window."""
    by_account = defaultdict(list)
    for r in rows:
        if r.get(window) is not None:
            by_account[r["account"]].append(r)
    out = []
    for seq in by_account.values():
        seq.sort(key=lambda r: r["t"])
        for a, b in zip(seq, seq[1:], strict=False):
            if a.get(f"{window}_reset") != b.get(f"{window}_reset"):
                continue  # the window reset in between
            delta = (float(b[window]) - float(a[window])) * 100
            out.append((family(a["model"]), [float(a[f]) for f in FIELDS], delta))
    return out


def solve(x: list[list[float]], y: list[float]) -> list[float] | None:
    """Least squares via the normal equations; None if the data can't separate the terms."""
    n = len(x[0])
    a = [[sum(r[i] * r[j] for r in x) for j in range(n)] + [sum(r[i] * v for r, v in zip(x, y, strict=False))]
         for i in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[piv][col]) < 1e-9:
            return None
        a[col], a[piv] = a[piv], a[col]
        for r in range(n):
            if r != col:
                f = a[r][col] / a[col][col]
                a[r] = [v - f * w for v, w in zip(a[r], a[col], strict=False)]
    return [a[i][n] / a[i][i] for i in range(n)]


def report(rows: list[dict], min_samples: int = 30) -> list[str]:
    if not rows:
        return [f"no data yet: {LOG_FILE} fills as requests go through the proxy"]
    lines = [f"{len(rows)} responses logged since "
             f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(min(r['t'] for r in rows)))}"]
    for window, label in (("h5", "5h window"), ("d7", "weekly")):
        groups = defaultdict(list)
        for fam, toks, delta in samples(rows, window):
            groups[fam].append((toks, delta))
        for fam, data in sorted(groups.items()):
            moved = sum(1 for _, d in data if d)
            head = f"\n{label}, {fam}: {len(data)} samples, {moved} with a change in %"
            if len(data) < min_samples or moved < 5:
                lines.append(head + f" (need {min_samples}+ samples and 5+ changes for a fit)")
                continue
            # Fit per million tokens so the coefficients are readable.
            coef = solve([[v / 1e6 for v in t] for t, _ in data], [d for _, d in data])
            if coef is None:
                lines.append(head + " (token types don't vary enough yet to tell them apart)")
                continue
            lines.append(head)
            base = coef[0] if coef[0] > 0 else None
            for f, c in zip(FIELDS, coef, strict=False):
                rel = f"  = {c / base:5.2f}x uncached input" if base else ""
                lines.append(f"  {f:<15} {c:8.3f} % per 1M tokens{rel}")
            totals = [sum(t[i] for t, _ in data) for i in range(len(FIELDS))]
            seen = ", ".join(f"{f}={v / 1e6:.2f}M" for f, v in zip(FIELDS, totals, strict=True))
            lines.append(f"  tokens seen     {seen}")
    lines.append("\nUtilization headers are coarse and other traffic adds noise: trust the fit only "
                 "after a few hundred samples, and compare cache_read with input rather than reading "
                 "absolute numbers.")
    return lines
