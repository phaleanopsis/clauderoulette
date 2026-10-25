import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from claude_roulette import credentials, measure
from claude_roulette import proxy as proxy_mod
from claude_roulette.config import DEFAULTS
from claude_roulette.conversations import (
    PINS_FILE,
    Conversation,
    ConversationPins,
    cache_ttls,
    claude_code_conversation,
    claude_code_title,
    describe,
    response_usage,
    short_title,
)
from claude_roulette.proxy import ProxyHandler
from claude_roulette.router import Router
from claude_roulette.usage import Usage, now

CFG = {**DEFAULTS, "accounts": ["a", "b"], "primary": "a"}


def usage(name, s_pct, s_hours=2, w_pct=10, w_days=3):
    return Usage(name, session_pct=s_pct, session_reset=now() + timedelta(hours=s_hours),
                 weekly_pct=w_pct, weekly_reset=now() + timedelta(days=w_days))


class Dialogs:
    """Stands in for notify.ask/post: records questions and answers them on demand."""

    def __init__(self):
        self.asked = []

    reply = None  # answer every question with this (None: never answer)
    delay = 0.0

    def ask(self, title, msg, buttons, answered, timeout=None):
        self.asked.append((msg, buttons, answered))
        if self.reply is not None:
            threading.Timer(self.delay, answered, [self.reply]).start()

    def answer(self, button):
        self.asked[-1][2](button)


@pytest.fixture
def pins():
    PINS_FILE.unlink(missing_ok=True)
    router = Router({**CFG})
    router.usages = {"a": usage("a", 10), "b": usage("b", 10)}
    d = Dialogs()
    return ConversationPins(router, ask=d.ask), d


def code(key, main=True, parent=None):
    return Conversation(key, "code", main=main, parent=parent)


# ------------------------------------------------------------------ identifying conversations

def test_claude_code_json_user_id():
    req = {"metadata": {"user_id": json.dumps({"device_id": "d", "session_id": "s1"})}, "tools": [{}]}
    c = claude_code_conversation(req)
    assert (c.key, c.main, c.parent, c.lane) == ("s1", True, None, "agent")
    sub = {"metadata": {"user_id": json.dumps({"session_id": "s2", "parent_session_id": "s1"})}, "tools": [{}]}
    assert (claude_code_conversation(sub).parent, claude_code_conversation(sub).main) == ("s1", False)
    side = {"metadata": {"user_id": json.dumps({"session_id": "s1"})}}  # no tools: a title or summary call
    assert claude_code_conversation(side).lane == "side"


def test_claude_code_legacy_user_id():
    uid = "user_abc_account_123_session_0f3c2a9b-e4d1-4a6f-9c1e-7b2d5a8f3e10"
    assert claude_code_conversation({"metadata": {"user_id": uid}}).key == "0f3c2a9b-e4d1-4a6f-9c1e-7b2d5a8f3e10"
    assert claude_code_conversation({"metadata": {}}) is None


def test_cache_ttls_in_prefix_order():
    req = {"messages": [{"content": [{"cache_control": {"type": "ephemeral"}}]}],
           "system": [{"cache_control": {"type": "ephemeral", "ttl": "1h"}}]}
    assert cache_ttls(req) == [3600, 300]  # system before messages, whatever the JSON order


def test_response_usage_reads_output_from_the_end():
    head = b'data: {"message":{"usage":{"input_tokens":3,"cache_read_input_tokens":9,"output_tokens":1}}}'
    tail = b'data: {"type":"message_delta","usage":{"output_tokens":420}}'
    assert response_usage(head, tail) == {"input": 3, "cache_read": 9, "output": 420}


# ------------------------------------------------------------------ placement and moving

def acc(p, conv, ttls=(300,)):
    return p.account_for(conv, list(ttls))[0]


def question(p, conv):
    return p.account_for(conv, [300])[1]


def test_new_conversations_spread_by_load(pins):
    p, _ = pins
    assert acc(p, code("c1")) == "a"  # primary bias
    p.record(code("c1"), {"cache_read": 200_000})
    assert acc(p, code("c2")) == "b"  # a carries c1's 200k


def test_subagent_follows_parent(pins):
    p, _ = pins
    acc(p, code("c1"))
    p.pins["c1"]["account"] = "b"
    assert acc(p, code("s1", main=False, parent="c1"), ()) == "b"


def test_warm_conversation_stays_when_another_account_is_better(pins):
    p, _ = pins
    acc(p, code("c1"))
    p.router.usages["b"] = usage("b", 0, s_hours=0.3)  # b suddenly far better
    assert acc(p, code("c1")) == "a"


def test_cold_conversation_moves_to_the_best_account(pins):
    p, _ = pins
    acc(p, code("c1"))
    p.router.usages["b"] = usage("b", 0, s_hours=0.3)
    p.pins["c1"]["warm_until"] = p.pins["c1"]["partial_until"] = time.time() - 1
    assert acc(p, code("c1")) == "b"


def test_partly_warm_conversation_needs_a_clear_gain(pins):
    p, _ = pins
    acc(p, code("c1"), (3600, 300))  # 1h system part, 5m history
    p.pins["c1"]["warm_until"] = time.time() - 1  # history cold, system part still cached
    p.cfg["primary_bias"] = 1.0
    p.router.usages["a"] = usage("a", 12)  # b only slightly better
    assert acc(p, code("c1"), (3600, 300)) == "a"


def test_full_account_asks_and_moves_on_answer(pins):
    p, _ = pins
    acc(p, code("c1"))
    p.router.usages["a"] = usage("a", 99)  # over the session cap
    q = question(p, code("c1"))
    assert q.kind == "full" and q.buttons == ["Switch and send on b account", "Cancel sending"]
    assert question(p, code("c1")) is None  # one question at a time
    assert p.resolve(q, "Switch and send on b account") == "b"
    assert acc(p, code("c1")) == "b"


def test_cancel_on_full_account_sends_the_fresh_start_to_the_target(pins):
    p, _ = pins
    acc(p, code("c1"))
    p.router.usages["a"] = usage("a", 99)
    q = question(p, code("c1"))
    assert "/clear" in q.message
    assert p.resolve(q, "Cancel sending") is None
    assert acc(p, code("c2")) == "b"  # the fresh conversation (/clear, new Science chat)
    assert p.pins["c1"]["account"] == "b"  # or this one after /compact
    p.router.usages["a"] = usage("a", 10)
    assert acc(p, code("c3")) == "a"  # only the next new conversation is steered


def test_no_answer_sends_as_before_and_waits_before_asking_again(pins):
    p, _ = pins
    acc(p, code("c1"))
    p.router.usages["a"] = usage("a", 99)
    q = question(p, code("c1"))
    assert p.resolve(q, None) == "a"
    assert question(p, code("c1")) is None  # not again within pin_ask_every_s
    p.pins["c1"]["asked_at"] = 0
    assert question(p, code("c1")).kind == "full"


def test_side_calls_are_never_held(pins):
    p, _ = pins
    acc(p, code("c1"))
    p.router.usages["a"] = usage("a", 99)
    assert question(p, code("c1", main=False)) is None


def test_early_warning_moves_now_or_stays_until_full(pins):
    p, _ = pins
    acc(p, code("c1"))
    p.record(code("c1"), {"cache_read": 50_000})
    p.router.usages["a"] = usage("a", 90)  # past pin_warn_pct, still usable
    q = question(p, code("c1"))
    assert q.kind == "warn" and q.buttons == ["Switch and send on b account", "Send on current account",
                                              "Cancel sending"]
    assert p.resolve(q, "Send on current account") == "a"
    p.pins["c1"]["asked_at"] = 0
    assert question(p, code("c1")) is None  # no second warning while staying
    p.router.usages["a"] = usage("a", 99)
    assert question(p, code("c1")).kind == "full"  # asked again once actually full


def test_early_warning_switch(pins):
    p, _ = pins
    acc(p, code("c1"))
    p.record(code("c1"), {"cache_read": 50_000})
    p.router.usages["a"] = usage("a", 90)
    assert p.resolve(question(p, code("c1")), "Switch and send on b account") == "b"


def test_early_warning_cancel_keeps_the_conversation_but_starts_fresh_elsewhere(pins):
    p, _ = pins
    acc(p, code("c1"))
    p.record(code("c1"), {"cache_read": 50_000})
    p.router.usages["a"] = usage("a", 90)
    assert p.resolve(question(p, code("c1")), "Cancel sending") is None
    assert p.pins["c1"]["account"] == "a"  # resending continues where it was
    assert acc(p, code("c2")) == "b"


def test_small_conversations_get_no_early_warning(pins):
    p, _ = pins
    acc(p, code("c1"))
    p.record(code("c1"), {"cache_read": 1_000})
    p.router.usages["a"] = usage("a", 90)
    assert question(p, code("c1")) is None


def test_large_context_asks_once(pins):
    p, _ = pins
    acc(p, code("c1"))
    p.record(code("c1"), {"cache_read": 160_000})
    q = question(p, code("c1"))
    assert q.kind == "hint" and q.buttons == ["Send prompt anyway", "Cancel sending"]
    assert "160k" in q.message and "Claude Code conversation c1" in q.message  # c1: no transcript, no title
    assert p.resolve(q, "Send prompt anyway") == "a"
    p.pins["c1"]["asked_at"] = 0
    assert question(p, code("c1")) is None


def test_side_calls_dont_overwrite_context(pins):
    p, _ = pins
    acc(p, code("c1"))
    p.record(code("c1"), {"cache_read": 80_000})
    p.record(code("c1", main=False), {"input": 900})
    assert p.pins["c1"]["context"] == 80_000


def test_pins_survive_restart(pins):
    p, _ = pins
    p.account_for(code("c1"), [300])
    p.pins["c1"]["account"] = "b"
    p._save()
    assert ConversationPins(p.router).pins["c1"]["account"] == "b"


# ------------------------------------------------------------------ through the proxy

class FakeUpstream(BaseHTTPRequestHandler):
    seen: list = []
    status = 200

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        FakeUpstream.seen.append((self.path, self.headers.get("Authorization")))
        if FakeUpstream.status != 200:
            body = json.dumps({"type": "error", "error": {"type": "rate_limit_error", "message": "limit"}}).encode()
            self.send_response(FakeUpstream.status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = (b'event: message_start\ndata: {"message":{"usage":{"input_tokens":5,'
                b'"cache_read_input_tokens":1000,"output_tokens":1}}}\n\n'
                + b": padding\n" * 3000
                + b'event: message_delta\ndata: {"usage":{"output_tokens":77}}\n\n')
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("anthropic-ratelimit-unified-5h-utilization", "0.25")
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def proxy(monkeypatch):
    for name in ("a", "b"):
        credentials.write_secret(name, json.dumps({"claudeAiOauth": {
            "accessToken": f"tok-{name}", "refreshToken": "r", "expiresAt": int((time.time() + 3600) * 1000)}}))
    credentials._creds.clear()
    PINS_FILE.unlink(missing_ok=True)
    measure.LOG_FILE.unlink(missing_ok=True)
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    monkeypatch.setattr(proxy_mod, "PING_EVERY_S", 0.05)
    router = Router({**CFG})
    d = Dialogs()
    ProxyHandler.router = router
    ProxyHandler.pins = ConversationPins(router, ask=d.ask)
    ProxyHandler.upstream = urllib.parse.urlsplit(f"http://127.0.0.1:{upstream.server_port}")
    ProxyHandler.key = "test-key"
    server = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    FakeUpstream.seen, FakeUpstream.status = [], 200
    yield f"http://127.0.0.1:{server.server_port}/k/test-key", router, d
    server.shutdown()
    upstream.shutdown()


def send(base, session, path="/v1/messages", stream=True):
    body = json.dumps({"model": "claude-opus-5", "tools": [{}], "stream": stream,
                       "metadata": {"user_id": json.dumps({"session_id": session})},
                       "system": [{"text": "x", "cache_control": {"type": "ephemeral"}}]}).encode()
    req = urllib.request.Request(base + path, data=body, method="POST",
                                 headers={"Authorization": "Bearer user-login"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def turn(base, session, path="/v1/messages"):
    send(base, session, path)
    return FakeUpstream.seen[-1][1]


def test_claude_code_session_is_pinned_through_the_proxy(proxy):
    base, router, _ = proxy
    first = turn(base, "s1")
    router.current = "b" if first.endswith("a") else "a"  # the global router moving on doesn't matter
    assert turn(base, "s1") == first
    pin = ProxyHandler.pins.pins["s1"]
    assert pin["context"] == 1005 and pin["warm_until"] > time.time()
    rows = measure.load_rows()
    assert rows[-1]["output"] == 77 and rows[-1]["h5"] == "0.25"


def make_full(router):
    router.usages = {"a": usage("a", 99), "b": usage("b", 10)}


def test_held_request_goes_where_the_user_says(proxy):
    base, router, d = proxy
    assert turn(base, "s1") == "Bearer tok-a"
    make_full(router)
    d.reply, d.delay = "Switch and send on b account", 0.3
    status, body = send(base, "s1")
    assert status == 200 and FakeUpstream.seen[-1][1] == "Bearer tok-b"
    assert body.startswith("event: ping") and "message_start" in body  # kept alive, then the real reply


def test_start_fresh_cancels_before_anything_is_sent(proxy):
    base, router, d = proxy
    turn(base, "s1")
    make_full(router)
    d.reply = "Cancel sending"
    sent = len(FakeUpstream.seen)
    status, body = send(base, "s1")
    assert len(FakeUpstream.seen) == sent
    assert "event: error" in body and "Message not sent" in body and "/clear" in body


def test_unanswered_request_is_sent_as_before(proxy):
    base, router, d = proxy
    turn(base, "s1")
    make_full(router)
    router.cfg["pin_hold_s"] = 0.3
    FakeUpstream.status = 429
    status, body = send(base, "s1")
    assert FakeUpstream.seen[-1][1] == "Bearer tok-a"
    assert status == 200 and "event: error" in body and "rate_limit_error" in body  # passed on as an event


def test_non_streaming_request_is_held_without_pings(proxy):
    base, router, d = proxy
    turn(base, "s1")
    make_full(router)
    d.reply, d.delay = "Switch and send on b account", 0.2
    status, body = send(base, "s1", stream=False)
    assert status == 200 and not body.startswith("event: ping")
    assert FakeUpstream.seen[-1][1] == "Bearer tok-b"


def test_observe_mode_uses_the_global_router(proxy):
    base, router, _ = proxy
    router.cfg["code_mode"] = "observe"
    turn(base, "s1")
    assert "s1" not in ProxyHandler.pins.pins


def test_count_tokens_is_not_a_turn(proxy):
    base, _, _ = proxy
    turn(base, "s1", "/v1/messages/count_tokens")
    assert "s1" not in ProxyHandler.pins.pins


# ------------------------------------------------------------------ measurement

def test_measure_recovers_weights():
    # Synthetic: 1M uncached input = 1%, cache reads count 0.1x, writes 1.25x, output 5x.
    import random

    rng = random.Random(1)
    rows, pct = [], 0.0
    for i in range(200):
        toks = [rng.randint(0, 20_000), rng.randint(0, 400_000), rng.randint(0, 40_000), rng.randint(0, 8_000)]
        rows.append({"t": i, "account": "a", "model": "claude-opus-5", **dict(zip(measure.FIELDS, toks, strict=False)),
                     "h5": str(pct / 100), "h5_reset": "1", "d7": None})
        pct += (toks[0] + 0.1 * toks[1] + 1.25 * toks[2] + 5 * toks[3]) / 1e6
    out = "\n".join(measure.report(rows))
    assert "cache_read" in out
    fit = {line.split()[0]: float(line.split("=")[1].split("x")[0])
           for line in out.splitlines() if "x uncached input" in line}
    assert abs(fit["cache_read"] - 0.1) < 0.01 and abs(fit["output"] - 5) < 0.1


SESSION = "4af495b0-16c2-4056-a3da-9a0e2423ec31"


def test_claude_code_title_prefers_a_rename(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    transcript = tmp_path / "projects" / "-Users-x-repo" / f"{SESSION}.jsonl"
    transcript.parent.mkdir(parents=True)
    entries = [{"type": "user", "message": {"content": "ai-title\""}},
               {"type": "ai-title", "aiTitle": "First guess", "sessionId": SESSION},
               {"type": "ai-title", "aiTitle": "Fix the login", "sessionId": SESSION}]
    transcript.write_text("".join(json.dumps(e) + "\n" for e in entries) + "{broken -title\"\n")
    assert claude_code_title(SESSION) == "Fix the login"
    assert describe("code", SESSION) == 'Claude Code conversation "Fix the login" (4af495b0)'
    with open(transcript, "a") as f:
        f.write(json.dumps({"type": "custom-title", "customTitle": "Login work", "sessionId": SESSION}) + "\n")
        f.write(json.dumps({"type": "ai-title", "aiTitle": "Later guess", "sessionId": SESSION}) + "\n")
    assert claude_code_title(SESSION) == "Login work"
    assert claude_code_title("../*") is None and describe("code", "../*") == "Claude Code conversation ../*"


def test_short_title():
    assert short_title("Fix the login") == "Fix the log…"  # 12 characters, ellipsis included
    assert short_title("Echo test") == "Echo test" and short_title(None) == ""
    assert short_title("Two\nlines") == "Two lines"
