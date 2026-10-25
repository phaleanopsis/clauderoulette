"""Secret storage (macOS Keychain or a chmod-600 file) and OAuth token refresh."""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__
from .config import LOGIN_DIR, TOKENS_FILE, file_lock, load_json, log, save_json

KEYCHAIN_SERVICE = "claude-roulette"
OAUTH_BETA = "oauth-2025-04-20"
# Claude Code's public OAuth client, as used by its own token refresh.
OAUTH_TOKEN_URL = os.environ.get("CLAUDE_ROULETTE_TOKEN_URL", "https://platform.claude.com/v1/oauth/token")
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
REFRESH_MARGIN_S = 300
USER_AGENT = f"claude-roulette/{__version__}"


def _has_keychain() -> bool:
    return (sys.platform == "darwin" and shutil.which("security") is not None
            and not os.environ.get("CLAUDE_ROULETTE_NO_KEYCHAIN"))


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
