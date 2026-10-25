#!/usr/bin/env python3
"""claude-rotator: run several Claude subscription accounts side by side.

  add NAME [--primary]  log in once as that account and keep its OAuth credential
  status                show 5h-session and weekly usage for every account
  serve                 local proxy that picks the account per request + 07:00 ping
  run [-- ARGS]         exec `claude ARGS` through the proxy
  prime [--account N]   ping now (default: least-used account without an active window)
  launchd               print a macOS LaunchAgent plist that keeps `serve` running

How credentials work: `add` runs Claude Code once in a throwaway config dir so you can
/login as the account. The resulting OAuth credential (access + refresh token) is moved
into this tool's own Keychain item and the throwaway copy is deleted, so this tool is
the only holder of that refresh chain and never races Claude Code's own login.
Claude Code itself stays logged in as usual and keeps all data in ~/.claude; the proxy
only replaces the bearer token on /v1/messages requests.

Stdlib only. Secrets never go on a command line or into logs.
"""
from __future__ import annotations

import argparse
import fcntl
import getpass
import hashlib
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path(os.environ.get("CLAUDE_ROTATOR_HOME", Path.home() / ".claude-rotator"))
CONFIG_FILE = HOME / "config.json"
STATE_FILE = HOME / "state.json"
LOCK_FILE = HOME / ".lock"
TOKENS_FILE = HOME / "tokens.json"  # only used when the Keychain is unavailable
LOGIN_DIR = HOME / "login"  # throwaway CLAUDE_CONFIG_DIRs used by `add`
PRIME_CWD = HOME / "prime"  # pings run here so they don't clutter your project history

KEYCHAIN_SERVICE = "claude-rotator"
OAUTH_BETA = "oauth-2025-04-20"
# Claude Code's public OAuth client, as used by its own token refresh.
OAUTH_TOKEN_URL = os.environ.get("CLAUDE_ROTATOR_TOKEN_URL", "https://platform.claude.com/v1/oauth/token")
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
REFRESH_MARGIN_S = 300
USER_AGENT = "claude-rotator/0.3"
FORCE_HEADER = "x-rotator-account"  # set via ANTHROPIC_CUSTOM_HEADERS to pin an account

DEFAULTS = {
    "accounts": [],
    "emails": {},
    "primary": None,  # preferred account
    "claude_bin": "claude",
    "upstream": "https://api.anthropic.com",
    "proxy_port": 8789,
    "session_cap": 95.0,  # % of 5h window at which an account is considered full
    "weekly_cap": 98.0,  # % of weekly limit at which an account is considered full
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


# --------------------------------------------------------------------------- config / state

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


# --------------------------------------------------------------------------- secret store

def _has_keychain() -> bool:
    return (sys.platform == "darwin" and shutil.which("security") is not None
            and not os.environ.get("CLAUDE_ROTATOR_NO_KEYCHAIN"))


def kc_read(service: str, account: str) -> str | None:
    out = subprocess.run(["security", "find-generic-password", "-s", service, "-a", account, "-w"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        return None
    value = out.stdout.rstrip("\n")
    # `security -w` prints non-ASCII data as hex; JSON blobs normally come back as text.
    if value and all(c in "0123456789abcdef" for c in value) and len(value) % 2 == 0:
        try:
            decoded = bytes.fromhex(value).decode()
            if decoded.lstrip().startswith("{"):
                return decoded
        except (ValueError, UnicodeDecodeError):
            pass
    return value


def kc_write(service: str, account: str, secret: str) -> None:
    # `security -i` reads the command from stdin, so the secret never shows up in `ps`.
    cmd = f'add-generic-password -U -s "{service}" -a "{account}" -X {secret.encode().hex()}\n'
    out = subprocess.run(["security", "-i"], input=cmd, capture_output=True, text=True)
    if out.returncode != 0 or "error" in out.stderr.lower():
        raise OSError(f"Keychain write failed: {out.stderr.strip()[:200]}")


def kc_delete(service: str, account: str) -> None:
    subprocess.run(["security", "delete-generic-password", "-s", service, "-a", account],
                   capture_output=True)


def read_secret(name: str) -> str | None:
    if _has_keychain():
        return kc_read(KEYCHAIN_SERVICE, name)
    return load_json(TOKENS_FILE, {}).get(name)


def write_secret(name: str, secret: str) -> None:
    if _has_keychain():
        kc_write(KEYCHAIN_SERVICE, name, secret)
        return
    tokens = load_json(TOKENS_FILE, {})
    tokens[name] = secret
    save_json(TOKENS_FILE, tokens)
    TOKENS_FILE.chmod(0o600)


# --------------------------------------------------------------------------- OAuth credentials

class AuthDead(Exception):
    """The stored credential can't be used any more; the account needs `add` again."""


_creds: dict[str, dict] = {}
_creds_lock = threading.Lock()


def load_cred(name: str) -> dict:
    raw = read_secret(name)
    if raw is None:
        raise AuthDead(f"no credential stored for {name!r}; run `add {name}`")
    try:
        cred = json.loads(raw)
    except json.JSONDecodeError:
        raise AuthDead(f"{name!r} holds an old setup-token; run `add {name}` again") from None
    if not (cred.get("claudeAiOauth") or {}).get("refreshToken"):
        raise AuthDead(f"{name!r} has no refresh token; run `add {name}` again")
    return cred


def _fresh(cred: dict) -> bool:
    expires_ms = cred["claudeAiOauth"].get("expiresAt") or 0
    return expires_ms / 1000 - time.time() > REFRESH_MARGIN_S


def refresh_cred(name: str, cred: dict) -> None:
    """Exchange the refresh token for a new access token (mutates cred)."""
    oauth = cred["claudeAiOauth"]
    body = json.dumps({"grant_type": "refresh_token", "refresh_token": oauth["refreshToken"],
                       "client_id": OAUTH_CLIENT_ID}).encode()
    req = urllib.request.Request(OAUTH_TOKEN_URL, data=body, method="POST", headers={
        "Content-Type": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            try:
                err = json.loads(e.read()).get("error")
            except (json.JSONDecodeError, AttributeError):
                err = None
            # Only an explicit invalid_grant is permanent; anything else is retried later.
            if err == "invalid_grant":
                raise AuthDead(f"refresh token for {name!r} was revoked; run `add {name}` again") from None
        raise OSError(f"token refresh for {name} failed: HTTP {e.code}") from None
    oauth["accessToken"] = data["access_token"]
    oauth["expiresAt"] = int((time.time() + data["expires_in"]) * 1000)
    if data.get("refresh_token"):
        oauth["refreshToken"] = data["refresh_token"]
    if data.get("scope"):
        oauth["scopes"] = data["scope"].split()


def access_token(name: str) -> str:
    """A valid access token for the account, refreshing (and persisting) it if needed."""
    cred = _creds.get(name)
    if cred and _fresh(cred):
        return cred["claudeAiOauth"]["accessToken"]
    with _creds_lock, file_lock():
        cred = load_cred(name)  # another process may have refreshed it already
        if not _fresh(cred):
            refresh_cred(name, cred)
            # The old refresh token is now spent: persisting the new one is critical.
            write_secret(name, json.dumps(cred))
            log(f"refreshed access token for {name}")
        _creds[name] = cred
    return cred["claudeAiOauth"]["accessToken"]


def claude_keychain_service(config_dir: str) -> str:
    """Keychain service name Claude Code uses for a CLAUDE_CONFIG_DIR (hash of the raw string)."""
    digest = hashlib.sha256(unicodedata.normalize("NFC", config_dir).encode()).hexdigest()[:8]
    return f"Claude Code-credentials-{digest}"


def import_login(name: str, config_dir: Path) -> str | None:
    """Move the credential Claude Code created in config_dir into our store, then wipe it."""
    service, account = claude_keychain_service(str(config_dir)), getpass.getuser()
    raw = kc_read(service, account) if _has_keychain() else None
    file_cred = config_dir / ".credentials.json"
    if raw is None and file_cred.exists():
        raw = file_cred.read_text()
    if raw is None:
        raise SystemExit("no login found; did you finish /login before exiting?")
    oauth = json.loads(raw).get("claudeAiOauth") or {}
    if not oauth.get("refreshToken"):
        raise SystemExit("the login has no refresh token; was it a subscription (Pro/Max) login?")
    if "user:profile" not in oauth.get("scopes", []):
        log(f"warning: credential lacks user:profile scope ({oauth.get('scopes')}); usage reads may fail")
    email = (load_json(config_dir / ".claude.json", {}).get("oauthAccount") or {}).get("emailAddress")

    write_secret(name, json.dumps({"claudeAiOauth": oauth}))
    _creds.pop(name, None)
    if _has_keychain():
        kc_delete(service, account)
    if config_dir.resolve().parent == LOGIN_DIR.resolve():
        shutil.rmtree(config_dir, ignore_errors=True)
    return email


# --------------------------------------------------------------------------- usage

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
    req = urllib.request.Request(cfg["upstream"].rstrip("/") + "/api/oauth/usage", headers={
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

def score(u: Usage, cfg: dict) -> float | None:
    """Higher = use this account first. None = not usable right now.

    "Use it or lose it": an active 5h window with lots of headroom and little time left is
    the most urgent capacity to burn. An idle account scores a flat 10 (using it opens a
    fresh window). Weekly pacing scales everything: ahead of an even 100%/7 per day is
    damped, behind it is boosted.
    """
    if u.error:
        return None
    s_pct, w_pct = u.session_pct or 0.0, u.weekly_pct or 0.0
    if w_pct >= cfg["weekly_cap"]:
        return None
    if u.window_active:
        if s_pct >= cfg["session_cap"]:
            return None
        hours_left = max((u.session_reset - now()).total_seconds() / 3600, 0.25)
        session_urgency = (100 - s_pct) / hours_left
    else:
        session_urgency = 10.0

    if u.weekly_reset and u.weekly_reset > now():
        days_left = max((u.weekly_reset - now()).total_seconds() / 86400, 0.1)
        weekly_factor = min(2.0, ((100 - w_pct) / days_left) / (100 / 7))
    else:
        weekly_factor = 1.0
    return session_urgency * weekly_factor


def least_used(usages: list[Usage], cfg: dict) -> Usage | None:
    ok = [u for u in usages if not u.error and (u.weekly_pct or 0) < cfg["weekly_cap"]]
    return min(ok, key=lambda u: (u.weekly_pct or 0, u.session_pct or 0), default=None)


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
                "usages": {n: asdict(u) for n, u in self.usages.items()},
                "blocked_until": dict(self.blocked_until),
                "dead": dict(self.dead),
                "updated_at": now().isoformat(),
            }


# --------------------------------------------------------------------------- proxy

HOP_HEADERS = {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "te",
               "trailer", "upgrade", "host", "content-length"}


def is_inference(path: str) -> bool:
    return path.split("?")[0].startswith("/v1/messages")


class ProxyHandler(BaseHTTPRequestHandler):
    router: Router
    upstream: urllib.parse.SplitResult
    debug = False

    def log_message(self, fmt, *args):
        pass

    def _connect(self):
        cls = http.client.HTTPSConnection if self.upstream.scheme == "https" else http.client.HTTPConnection
        return cls(self.upstream.netloc, timeout=600)

    def _send_upstream(self, name: str | None, body: bytes):
        """name=None passes the request through with Claude Code's own credentials."""
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in HOP_HEADERS and k.lower() != FORCE_HEADER}
        if name is not None:
            auth_key = next((k for k in headers if k.lower() == "authorization"), None)
            if auth_key and headers[auth_key].startswith("Bearer "):
                headers[auth_key] = f"Bearer {access_token(name)}"
        conn = self._connect()
        conn.request(self.command, self.path, body=body or None, headers=headers)
        return conn, conn.getresponse()

    def _forward(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        conn = resp = name = None
        if not is_inference(self.path):
            try:
                conn, resp = self._send_upstream(None, body)
            except (OSError, http.client.HTTPException) as e:
                log(f"passthrough error: {e}")
        else:
            forced = self.headers.get(FORCE_HEADER)
            tried: set[str] = set()
            while True:
                if forced:
                    candidate = None if tried else forced
                else:
                    candidate = self.router.choose(exclude=tried)
                if candidate is None:
                    break
                if resp is not None:  # discard the previous 429 before retrying
                    resp.read()
                    conn.close()
                    conn = resp = None
                tried.add(candidate)
                try:
                    conn, resp = self._send_upstream(candidate, body)
                except AuthDead as e:
                    self.router.mark_dead(candidate, str(e))
                    continue
                except (OSError, http.client.HTTPException) as e:
                    log(f"upstream error via {candidate}: {e}")
                    continue
                name = candidate
                self.router.observe(name, resp.status, dict(resp.getheaders()))
                if self.debug:
                    rl = {k: v for k, v in resp.getheaders() if k.lower().startswith("anthropic-ratelimit")}
                    log(f"  ratelimit headers: {rl}")
                if resp.status != 429 or forced:
                    break
                log(f"429 on {name}, retrying on another account")
        if resp is None:
            self.send_error(502, "no upstream account reachable")
            return
        log(f"{self.command} {self.path.split('?')[0]} -> {name or 'passthrough'} {resp.status}")
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in HOP_HEADERS:
                self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while chunk := resp.read1(65536):  # stream SSE through as it arrives
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            conn.close()

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _forward


def proxy_running(cfg: dict) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", cfg["proxy_port"]), timeout=0.5):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------------- priming

def claude_env(**extra) -> dict:
    env = dict(os.environ)
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"):
        env.pop(k, None)  # each of these would change which credential Claude Code uses
    env.update(extra)
    return env


def prime_account(name: str, cfg: dict) -> tuple[bool, str]:
    """One tiny Haiku prompt, sent by Claude Code itself with that account's access token."""
    PRIME_CWD.mkdir(parents=True, exist_ok=True)
    cmd = [cfg["claude_bin"], "-p", cfg["prime_prompt"], "--model", cfg["prime_model"]]
    try:
        env = claude_env(CLAUDE_CODE_OAUTH_TOKEN=access_token(name))
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


def run_auto_prime(cfg: dict, usages: list[Usage], state: dict) -> None:
    if not cfg["auto_prime"] or in_quiet_hours(cfg):
        return
    last = state.setdefault("last_prime", {})
    for u in usages:
        if u.error or u.window_active or (u.weekly_pct or 0) >= cfg["weekly_cap"]:
            continue
        if time.time() - last.get(u.name, 0) < cfg["prime_cooldown_s"]:
            continue
        ok, msg = prime_account(u.name, cfg)
        log(f"auto-prime {u.name}: {'ok' if ok else 'FAILED'} {msg!r}")
        if ok:
            last[u.name] = time.time()


# --------------------------------------------------------------------------- display

def fmt_reset(ts: datetime | None) -> str:
    if ts is None:
        return "-"
    secs = (ts - now()).total_seconds()
    if secs <= 0:
        return "expired"
    h, m = divmod(int(secs // 60), 60)
    return f"{h // 24}d{h % 24:02d}h" if h >= 24 else f"{h}h{m:02d}m"


def fmt_pct(p: float | None) -> str:
    return "-" if p is None else f"{p:5.1f}%"


def print_table(usages: list[Usage], cfg: dict, current: str | None = None,
                blocked: dict[str, float] | None = None, dead: dict[str, str] | None = None) -> None:
    router = Router(cfg)
    router.usages.update({u.name: u for u in usages})
    router.blocked_until = blocked or {}
    router.dead = dead or {}
    scores = router.scores([u.name for u in usages])
    print(f"  {'account':<12}{'5h used':>9}{'resets':>9}{'week used':>11}{'resets':>9}{'score':>8}  info")
    for u in usages:
        mark = "*" if u.name == current else " "
        info = cfg["emails"].get(u.name) or ""
        if u.name == cfg["primary"]:
            info += " (primary)"
        if u.name in router.dead or u.error:
            print(f"{mark} {u.name:<12}  error: {router.dead.get(u.name) or u.error}")
            continue
        if u.name in scores:
            sc = f"{scores[u.name]:.1f}"
        elif router.blocked_until.get(u.name, 0) > time.time():
            sc = "429"
        else:
            sc = "full"
        print(f"{mark} {u.name:<12}{fmt_pct(u.session_pct):>9}{fmt_reset(u.session_reset):>9}"
              f"{fmt_pct(u.weekly_pct):>11}{fmt_reset(u.weekly_reset):>9}{sc:>8}  {u.source} {info}")


# --------------------------------------------------------------------------- CLI

def cmd_add(args, cfg):
    name = args.name
    login_dir = LOGIN_DIR / name
    if login_dir.exists():
        shutil.rmtree(login_dir)
    login_dir.mkdir(parents=True)
    print(f"""
Adding account {name!r}. Claude Code will now start with an empty, temporary profile.
  1. Go through the first-run prompts and choose the Claude subscription login.
  2. In the browser, sign in as the account you want for {name!r}.
     (Already signed in as a different account? Copy the login URL into a private window.)
  3. Once Claude Code shows its prompt, type /exit.
Your normal ~/.claude login is not touched.
""")
    input("Press Enter to start Claude Code... ")
    subprocess.run([cfg["claude_bin"]], env=claude_env(CLAUDE_CONFIG_DIR=str(login_dir)), cwd=login_dir)

    email = import_login(name, login_dir)
    others = {n: e for n, e in cfg["emails"].items() if n != name}
    if email and email in others.values():
        print(f"warning: {email} is already stored under another name; was the right account used?")
    accounts = cfg["accounts"] + ([name] if name not in cfg["accounts"] else [])
    primary = name if args.primary or not cfg["primary"] else cfg["primary"]
    save_config_keys(accounts=accounts, primary=primary, emails={**cfg["emails"], name: email})
    print(f"stored {name} ({email or 'email unknown'}){' as primary' if primary == name else ''}")


def cmd_remove(args, cfg):
    if args.name not in cfg["accounts"]:
        sys.exit(f"unknown account {args.name!r}")
    if _has_keychain():
        kc_delete(KEYCHAIN_SERVICE, args.name)
    else:
        tokens = load_json(TOKENS_FILE, {})
        tokens.pop(args.name, None)
        save_json(TOKENS_FILE, tokens)
    accounts = [a for a in cfg["accounts"] if a != args.name]
    emails = {k: v for k, v in cfg["emails"].items() if k != args.name}
    primary = cfg["primary"] if cfg["primary"] != args.name else (accounts[0] if accounts else None)
    save_config_keys(accounts=accounts, emails=emails, primary=primary)
    print(f"removed {args.name}")


def cmd_status(args, cfg):
    state = load_json(STATE_FILE, {})
    if proxy_running(cfg) and state.get("usages"):
        usages = [Usage(**{**u, "session_reset": parse_ts(u["session_reset"]),
                           "weekly_reset": parse_ts(u["weekly_reset"])})
                  for u in state["usages"].values()]
        print(f"proxy on :{cfg['proxy_port']}, state from {state['updated_at']}")
        print_table(usages, cfg, state.get("current"), state.get("blocked_until"), state.get("dead"))
    else:
        print("proxy not running, probing directly")
        print_table([probe(a, cfg) for a in cfg["accounts"]], cfg)


def cmd_serve(args, cfg):
    if not cfg["accounts"]:
        sys.exit("no accounts configured; run `add` first")
    router = Router(cfg)
    ProxyHandler.router = router
    ProxyHandler.upstream = urllib.parse.urlsplit(cfg["upstream"])
    ProxyHandler.debug = args.debug
    server = ThreadingHTTPServer(("127.0.0.1", cfg["proxy_port"]), ProxyHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"proxy on http://127.0.0.1:{cfg['proxy_port']} -> {cfg['upstream']}, "
        f"accounts {cfg['accounts']}, primary {cfg['primary']}")

    state = load_json(STATE_FILE, {})
    last_probe = 0.0
    while True:
        if time.time() - last_probe >= cfg["probe_interval_s"]:
            for a in cfg["accounts"]:
                u = probe(a, cfg)
                if u.source == "dead":
                    router.mark_dead(a, u.error)
                elif u.error:
                    log(f"probe {a}: {u.error}")
                router.merge_probe(u)
            last_probe = time.time()
        usages = [router.usages[a] for a in cfg["accounts"] if a not in router.dead]
        run_schedule(cfg, usages, state)
        run_auto_prime(cfg, usages, state)
        state.update(router.snapshot())
        save_json(STATE_FILE, state)
        time.sleep(args.tick)


def cmd_run(args, cfg):
    claude_args = args.claude_args[1:] if args.claude_args[:1] == ["--"] else args.claude_args
    if proxy_running(cfg):
        env = claude_env(ANTHROPIC_BASE_URL=f"http://127.0.0.1:{cfg['proxy_port']}")
        if args.account:
            env["ANTHROPIC_CUSTOM_HEADERS"] = f"{FORCE_HEADER}: {args.account}"
            log(f"routing through proxy, pinned to {args.account}")
        else:
            log("routing through proxy (account chosen per request)")
    else:
        if args.account:
            sys.exit("--account needs the proxy; start `serve` first")
        env = claude_env()
        log("proxy not running: starting Claude Code on its own login, no rotation")
    os.execvpe(cfg["claude_bin"], [cfg["claude_bin"], *claude_args], env)


def cmd_prime(args, cfg):
    if args.account:
        name = args.account
    else:
        u = least_used([probe(a, cfg) for a in cfg["accounts"]], cfg)
        if u is None:
            sys.exit("no eligible account")
        if u.window_active:
            print(f"note: {u.name} already has an active window")
        name = u.name
    ok, msg = prime_account(name, cfg)
    print(f"{name}: {'ok' if ok else 'FAILED'} {msg}")


def cmd_launchd(args, cfg):
    script = Path(__file__).resolve()
    path = os.environ.get("PATH", "/usr/bin:/bin")
    print(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.user.claude-rotator</string>
  <key>ProgramArguments</key>
  <array>
    <string>{sys.executable}</string>
    <string>{script}</string>
    <string>serve</string>
  </array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>{path}</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>{HOME / 'serve.log'}</string>
</dict>
</plist>""")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("name")
    a.add_argument("--primary", action="store_true")
    a.set_defaults(fn=cmd_add)
    rm = sub.add_parser("remove")
    rm.add_argument("name")
    rm.set_defaults(fn=cmd_remove)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    s = sub.add_parser("serve")
    s.add_argument("--tick", type=float, default=30, help="seconds between scheduler checks")
    s.add_argument("--debug", action="store_true", help="log every rate-limit header received")
    s.set_defaults(fn=cmd_serve)
    r = sub.add_parser("run")
    r.add_argument("--account", help="pin this session to one account (needs the proxy)")
    r.add_argument("claude_args", nargs=argparse.REMAINDER)
    r.set_defaults(fn=cmd_run)
    pr = sub.add_parser("prime")
    pr.add_argument("--account")
    pr.set_defaults(fn=cmd_prime)
    sub.add_parser("launchd").set_defaults(fn=cmd_launchd)
    args = p.parse_args(argv)
    args.fn(args, load_config())


if __name__ == "__main__":
    main()
