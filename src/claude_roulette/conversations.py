"""Keeps each conversation on one account, because its prompt cache lives there.

The API keeps no conversation state: every request re-sends the whole conversation. Only
the prompt cache is per account (each subscription is its own organization), so the first
turn after a move re-sends the context uncached. Hence, per request:

  new conversation          -> the best account after discounting the load already on it
  subagent                  -> its parent's account
  cache warm                -> stay; near or at the account's limit, ask (holding the request)
  cache cold (idle > TTL)   -> free to move to the best account

Clients are told apart by the conversation id in `metadata.user_id`. Claude Code is read
here; add-ons (e.g. Claude Science) hand over their own `Conversation`.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import notify
from .config import HOME, load_json, log, save_json
from .router import Router

PINS_FILE = HOME / "conversation-pins.json"
KEEP_DAYS = 30
TTL_S = {"5m": 300, "1h": 3600}
CLIENT_NAMES = {"code": "Claude Code", "science": "Claude Science"}
SESSION_ID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def how_to_start_fresh(client: str) -> str:
    return "type /clear (or /compact to keep a summary)" if client == "code" else "open a new conversation"


@dataclass
class Question:
    """Asked before a request is sent; the request waits for the answer (see proxy._hold)."""

    key: str
    kind: str  # "warn" (account nearly full), "full", or "hint" (large context)
    message: str
    buttons: list[str]  # the first is the default
    target: str | None = None  # where a "Switch" answer sends the conversation
    client: str = "code"


@dataclass
class Conversation:
    """Who a request belongs to."""

    key: str  # pin key: one conversation, or one subagent
    client: str  # "code" or an add-on's name
    main: bool = False  # the main agent's turn: its token count is the conversation's context
    parent: str | None = None  # a subagent starts on its parent's account
    lane: str = "agent"  # for the log: agent, sub, side, or an add-on's lane name


def parse(body: bytes) -> dict | None:
    try:
        req = json.loads(body)
    except ValueError:
        return None
    return req if isinstance(req, dict) else None


def claude_code_conversation(req: dict) -> Conversation | None:
    """Claude Code sends metadata.user_id as JSON with session_id (and parent_session_id for
    a subagent); before 2.1 it was a string ending in `_session_<uuid>`."""
    user_id = (req.get("metadata") or {}).get("user_id")
    if not isinstance(user_id, str):
        return None
    parent = None
    try:
        ids = json.loads(user_id)
        session, parent = ids.get("session_id"), ids.get("parent_session_id")
    except (ValueError, AttributeError):
        m = re.search(r"_session_([0-9a-fA-F-]{36})", user_id)
        session = m and m.group(1)
    if not isinstance(session, str) or not session:
        return None
    # Side calls (titles, summaries) share the session id but carry no tools and a tiny context.
    main = parent is None and bool(req.get("tools"))
    return Conversation(session, "code", main=main, parent=parent,
                        lane="sub" if parent else "agent" if main else "side")


def claude_code_title(session: str) -> str | None:
    """The session's name as Claude Code lists it: a /rename title, else the generated one.

    Read from the transcript (~/.claude/projects/<project>/<session>.jsonl), where Claude Code
    appends a custom-title or ai-title entry whenever it changes; the last one counts.
    """
    if not SESSION_ID.fullmatch(session):  # it goes into a glob pattern
        return None
    projects = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    titles: dict[str, str] = {}
    for path in projects.glob(f"*/{session}.jsonl"):
        with open(path, errors="replace") as f:
            for line in f:
                if '-title"' not in line:  # skip parsing the (large) message entries
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                kind = {"custom-title": "customTitle", "ai-title": "aiTitle"}.get(entry.get("type"))
                if kind and isinstance(entry.get(kind), str):
                    titles[kind] = entry[kind]
    return titles.get("customTitle") or titles.get("aiTitle")


def conversation_title(client: str, key: str) -> str | None:
    try:
        if client == "code":
            return claude_code_title(key)
        from . import addons  # addons imports this module
        return getattr(addons.load().get(client), "title", lambda key: None)(key)
    except Exception as e:  # a name is a nicety: never let it break a request
        log(f"title of {client} conv={key[:8]}: {e!r}")
        return None


def describe(client: str, key: str) -> str:
    """How a conversation is named in questions, e.g. 'Claude Code conversation "Fix login" (4af495b0)'."""
    title = conversation_title(client, key)
    name = CLIENT_NAMES.get(client, client)
    return f'{name} conversation "{title}" ({key[:8]})' if title else f"{name} conversation {key[:8]}"


def cache_ttls(req: dict) -> list[int]:
    """TTL in seconds of each cache_control mark, in prefix order (tools, system, messages).

    The last mark covers the longest prefix, so it says when most of the cache expires.
    """
    out: list[int] = []

    def walk(node):
        if isinstance(node, dict):
            cc = node.get("cache_control")
            if isinstance(cc, dict):
                out.append(TTL_S.get(cc.get("ttl", "5m"), 300))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for part in ("tools", "system", "messages"):
        walk(req.get(part))
    return out


def response_usage(head: bytes, tail: bytes = b"") -> dict[str, int]:
    """Token counts of a Messages response, from its start (JSON body or SSE message_start)
    and, for output_tokens, its end (the final message_delta)."""
    found = {}
    for key, short in (("input_tokens", "input"), ("cache_read_input_tokens", "cache_read"),
                       ("cache_creation_input_tokens", "cache_creation")):
        m = re.search(rb'"' + key.encode() + rb'"\s*:\s*(\d+)', head)
        if m:
            found[short] = int(m.group(1))
    outs = re.findall(rb'"output_tokens"\s*:\s*(\d+)', head + tail)
    if outs:
        found["output"] = int(outs[-1])
    return found


def context_tokens(usage: dict[str, int]) -> int:
    return usage.get("input", 0) + usage.get("cache_read", 0) + usage.get("cache_creation", 0)


class ConversationPins:
    """Which account each conversation runs on, saved across restarts."""

    def __init__(self, router: Router, ask=None):
        self.router = router
        self.cfg = router.cfg
        self.ask = ask or notify.ask
        self.lock = threading.RLock()
        self.pins: dict[str, dict] = self._load()
        self.asking: set[str] = set()  # conversations with a question on screen
        self.fresh_on: tuple[str, float] | None = None  # (account, until): where to start fresh after a cancel

    @staticmethod
    def _load() -> dict[str, dict]:
        pins = load_json(PINS_FILE, {})
        cutoff = time.time() - KEEP_DAYS * 86400
        return {k: p for k, p in pins.items() if p.get("last_seen", 0) > cutoff}

    def _save(self) -> None:
        save_json(PINS_FILE, self.pins)

    # ---------------------------------------------------------------- placement

    def load(self, name: str, skip: str | None = None) -> int:
        """Context tokens of the conversations on `name` that are still in use."""
        since = time.time() - self.cfg["pin_active_s"]
        return sum(p.get("context", 0) for k, p in self.pins.items()
                   if k != skip and p["account"] == name and p.get("last_seen", 0) > since)

    def placements(self, skip: str | None = None) -> dict[str, float]:
        """Usable accounts by score, discounted by the load of their active conversations."""
        names = [n for n in self.cfg["accounts"] if n not in self.router.dead]
        with self.router.lock:
            scores = self.router.scores(names)
        scale = self.cfg["pin_load_scale"]
        return {n: s / (1 + self.load(n, skip) / scale) for n, s in scores.items()}

    def _place(self, skip: str | None = None, avoid: str | None = None) -> str | None:
        placed = {n: s for n, s in self.placements(skip).items() if n != avoid}
        if placed:
            return max(placed, key=placed.get)
        if avoid is not None:
            return None
        names = [n for n in self.cfg["accounts"] if n not in self.router.dead]
        # nothing known-good: let upstream decide
        return min(names, key=lambda n: self.router.blocked_until.get(n, 0)) if names else None

    def _usable(self, name: str) -> bool:
        with self.router.lock:
            return name in self.router.scores([name])

    def _move(self, key: str, pin: dict, to: str, why: str) -> None:
        log(f"{pin.get('client', '?')} conv={key[:8]} {pin['account']} -> {to} ({why}, "
            f"{pin.get('context', 0) // 1000}k context)")
        pin.update(account=to, pinned_at=time.time(), move_ok=None, asked_at=0)

    # ---------------------------------------------------------------- per request

    def account_for(self, conv: Conversation, ttls: list[int]) -> tuple[str | None, Question | None]:
        """The account for this request, and a question to ask before sending it (or None).

        With a question, the caller holds the request, asks, and sends it wherever
        `resolve` says.
        """
        t = time.time()
        question = None
        with self.lock:
            pin = self.pins.get(conv.key)
            if pin is not None and pin["account"] not in self.cfg["accounts"]:
                pin = None
            if pin is None:
                parent = self.pins.get(conv.parent) if conv.parent else None
                name = parent["account"] if parent else (self._take_fresh_on() or self._place())
                if name is None:
                    return None, None
                pin = self.pins[conv.key] = {"account": name, "client": conv.client, "pinned_at": t,
                                             "context": 0, "warm_until": 0, "partial_until": 0}
                loads = ", ".join(f"{n}={self.load(n) // 1000}k" for n in self.cfg["accounts"])
                log(f"{conv.client} conv={conv.key[:8]} pinned to {name} "
                    f"({'parent' if parent else 'new'}; load {loads})")
            elif conv.parent is None:
                question = self._reconsider(conv, pin, t)
            if ttls:  # every cache hit refreshes the TTL
                pin["warm_until"] = max(pin.get("warm_until", 0), t + ttls[-1])
                pin["partial_until"] = max(pin.get("partial_until", 0), t + max(ttls))
            pin["last_seen"] = t
            self._save()
            return pin["account"], question

    def _reconsider(self, conv: Conversation, pin: dict, t: float) -> Question | None:
        key, cur = conv.key, pin["account"]
        if t > pin.get("warm_until", 0):  # cold: moving costs nothing extra
            best = self._place(skip=key)
            if best and best != cur:
                placed = self.placements(skip=key)
                if (t > pin.get("partial_until", 0)  # nothing cached at all
                        or cur not in placed
                        or placed[best] >= placed[cur] * self.cfg["switch_ratio"]):
                    self._move(key, pin, best, "cache cold")
            return None
        # Only the main agent's turn is held for a question; side calls and subagents go on.
        if not conv.main or key in self.asking or t - pin.get("asked_at", 0) < self.cfg["pin_ask_every_s"]:
            return None
        ctx = pin.get("context", 0) // 1000
        fresh = how_to_start_fresh(conv.client)
        if not self._usable(cur):  # warm, but the account is full or blocked
            target = self._place(skip=key, avoid=cur)
            if target is None:
                return None
            return self._question(key, pin, conv.client, "full", target,
                                  [f"Switch and send on {target} account", "Cancel sending"],
                                  f"The {cur} Claude account is full. The {describe(conv.client, key)} "
                                  f"({ctx}k tokens) can continue on the {target} account; its cache is rebuilt "
                                  f"there once.\n\nCancel sending: the message is not sent. To start fresh, "
                                  f"{fresh}; the new conversation goes to {target}.")
        big = pin.get("context", 0) >= self.cfg["pin_warn_min_context"]
        if pin.get("move_ok") is None and big and self._near_full(cur):
            target = self._place(skip=key, avoid=cur)
            if target:
                pct = self.router.usages[cur].session_pct or 0
                return self._question(key, pin, conv.client, "warn", target,
                                      [f"Switch and send on {target} account", "Send on current account",
                                       "Cancel sending"],
                                      f"The {cur} Claude account is at {pct:.0f}% of its 5h window. The "
                                      f"{describe(conv.client, key)} ({ctx}k tokens) runs on it.\n\n"
                                      f"Switch: this message and the rest of the conversation go to the {target} "
                                      "account; the cache is rebuilt there once.\n"
                                      f"Send on current account: stays on {cur}; you'll be asked again when it is "
                                      f"full.\nCancel sending: the message is not sent. To start fresh, {fresh}; "
                                      f"the new conversation goes to {target}.")
        hint = self.cfg["pin_hint_tokens"]
        if hint and pin.get("context", 0) >= hint and not pin.get("hinted"):
            pin["hinted"] = True
            return self._question(key, pin, conv.client, "hint", None, ["Send prompt anyway", "Cancel sending"],
                                  f"The {describe(conv.client, key)} is quite large and re-reads {ctx}k tokens "
                                  "with every message. If you don't need its full history, a fresh conversation "
                                  "uses far less of your limits.\n\n"
                                  f"Cancel sending: the message is not sent. To start fresh, {fresh}.")
        return None

    def _question(self, key, pin, client, kind, target, buttons, message) -> Question:
        pin["asked_at"] = time.time()
        self.asking.add(key)
        return Question(key, kind, message, buttons, target, client)

    def _near_full(self, name: str) -> bool:
        u = self.router.usages.get(name)
        return bool(u and u.window_active and (u.session_pct or 0) >= self.cfg["pin_warn_pct"])

    def ask_question(self, q: Question, answered) -> None:
        log(f"conv={q.key[:8]} asking ({q.kind}): {' / '.join(q.buttons)}")
        self.ask("Claude Roulette", q.message, q.buttons, answered, timeout=self.cfg["pin_hold_s"])

    def _take_fresh_on(self) -> str | None:
        """The account to start fresh on after a recent "Cancel sending", once, if it still has room."""
        if self.fresh_on is None:
            return None
        name, until = self.fresh_on
        self.fresh_on = None
        return name if time.time() < until and self._usable(name) else None

    def resolve(self, q: Question, button: str | None) -> str | None:
        """Apply an answer (None: no answer in time). Returns the account to send the held
        request to, or None to cancel it."""
        with self.lock:
            self.asking.discard(q.key)
            pin = self.pins.get(q.key)
            if pin is None:
                return None
            log(f"conv={q.key[:8]} {q.kind}: {button or 'no answer, sending as before'}")
            if button and button.startswith("Cancel"):
                if q.target:  # a fresh conversation goes to the account with room
                    self.fresh_on = (q.target, time.time() + self.cfg["pin_ask_every_s"])
                if q.kind == "full":  # nothing more fits here: /compact or a resend goes there too
                    self._move(q.key, pin, q.target, "account full, message cancelled")
                    self._save()
                return None
            if button and button.startswith("Switch"):
                self._move(q.key, pin, q.target, "user chose to switch")
            elif button and button.startswith("Send on current"):
                pin["move_ok"] = False  # ask again only once the account is actually full
            self._save()
            return pin["account"]

    def record(self, conv: Conversation, usage: dict[str, int]) -> None:
        """After a main-agent turn: remember the context size the next turn will re-read."""
        if not conv.main or not usage:
            return
        with self.lock:
            pin = self.pins.get(conv.key)
            if pin is None:
                return
            pin["context"] = context_tokens(usage)
            if pin.get("move_ok") is False and not self._near_full(pin["account"]):
                pin["move_ok"] = None  # the account has room again: warn afresh next time
            self._save()

    def snapshot(self) -> dict:
        with self.lock:
            return {k: dict(p) for k, p in self.pins.items()}


def short_title(title: str | None, width: int = 12) -> str:
    title = " ".join((title or "").split())
    return title if len(title) <= width else title[:width - 1] + "…"


def status_lines(pins: dict, limit: int = 10) -> list[str]:
    from datetime import datetime

    recent = sorted(pins.items(), key=lambda kv: -kv[1].get("last_seen", 0))[:limit]
    if not recent:
        return []
    now_s = time.time()
    lines = ["", "  conversation           client   account       context  cache  last turn"]
    for key, p in recent:
        cache = "warm" if p.get("warm_until", 0) > now_s else "cold"
        title = short_title(conversation_title(p.get("client", "?"), key))
        lines.append(f"  {key[:8]}  {title:<12}  {p.get('client', '?'):<7}  {p['account']:<12} "
                     f"{p.get('context', 0) / 1000:6.0f}k  {cache:<5}  "
                     f"{datetime.fromtimestamp(p.get('last_seen', 0)):%a %H:%M}")
    return lines
