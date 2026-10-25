import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from claude_roulette import credentials
from claude_roulette.config import DEFAULTS
from claude_roulette.proxy import FORCE_HEADER, ProxyHandler
from claude_roulette.router import Router


class FakeUpstream(BaseHTTPRequestHandler):
    seen: list = []
    limited: set = set()

    def log_message(self, *a):
        pass

    def do_POST(self):
        token = self.headers.get("Authorization", "")
        FakeUpstream.seen.append((self.path, token))
        status = 429 if token in FakeUpstream.limited else 200
        body = json.dumps({"token": token}).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("anthropic-ratelimit-unified-5h-utilization", "0.5")
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST


def serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def proxy():
    for name in ("a", "b"):
        credentials.write_secret(name, json.dumps({"claudeAiOauth": {
            "accessToken": f"tok-{name}", "refreshToken": "r", "expiresAt": int((time.time() + 3600) * 1000)}}))
    credentials._creds.clear()
    upstream = serve(FakeUpstream)
    cfg = {**DEFAULTS, "accounts": ["a", "b"], "primary": "a"}
    ProxyHandler.router = Router(cfg)
    ProxyHandler.upstream = urllib.parse.urlsplit(f"http://127.0.0.1:{upstream.server_port}")
    server = serve(ProxyHandler)
    FakeUpstream.seen, FakeUpstream.limited = [], set()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    upstream.shutdown()


def call(base, path, headers=None):
    req = urllib.request.Request(base + path, data=b"{}", method="POST",
                                 headers={"Authorization": "Bearer user-login", **(headers or {})})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.load(resp)["token"]
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)["token"]


def test_messages_get_the_chosen_accounts_token(proxy):
    assert call(proxy, "/v1/messages") == (200, "Bearer tok-a")


def test_other_paths_pass_through_untouched(proxy):
    assert call(proxy, "/api/oauth/profile") == (200, "Bearer user-login")


def test_429_fails_over_to_the_other_account(proxy):
    FakeUpstream.limited = {"Bearer tok-a"}
    assert call(proxy, "/v1/messages") == (200, "Bearer tok-b")
    assert [t for _, t in FakeUpstream.seen] == ["Bearer tok-a", "Bearer tok-b"]


def test_pinned_account_is_used_and_header_stripped(proxy):
    assert call(proxy, "/v1/messages", {FORCE_HEADER: "b"}) == (200, "Bearer tok-b")
    FakeUpstream.limited = {"Bearer tok-b"}
    assert call(proxy, "/v1/messages", {FORCE_HEADER: "b"}) == (429, "Bearer tok-b")  # no failover when pinned
