import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from claude_roulette import credentials
from claude_roulette import proxy as proxy_mod
from claude_roulette.config import DEFAULTS
from claude_roulette.proxy import FORCE_HEADER, ProxyHandler
from claude_roulette.router import Router

KEY = "test-key"


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
    ProxyHandler.key = KEY
    server = serve(ProxyHandler)
    FakeUpstream.seen, FakeUpstream.limited = [], set()
    yield f"http://127.0.0.1:{server.server_port}/k/{KEY}"
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


@pytest.mark.parametrize("host", ["attacker.example:{port}", "attacker.example", "127.0.0.1.nip.io:{port}", ""])
def test_foreign_host_is_refused_before_any_token_is_used(proxy, host):
    """DNS rebinding: a web page's request reaches 127.0.0.1 but names its own domain."""
    port = urllib.parse.urlsplit(proxy).port
    req = urllib.request.Request(proxy + "/v1/messages", data=b"{}", method="POST",
                                 headers={"Authorization": "Bearer x", "Host": host.format(port=port)})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403
    assert json.load(e.value)["error"]["type"] == "permission_error"
    assert FakeUpstream.seen == []


def test_localhost_host_is_accepted(proxy):
    port = urllib.parse.urlsplit(proxy).port
    assert call(proxy, "/v1/messages", {"Host": f"localhost:{port}"}) == (200, "Bearer tok-a")


def test_unknown_pinned_account_is_refused(proxy):
    status, _ = call_error(proxy, {FORCE_HEADER: "nobody"})
    assert status == 400
    assert FakeUpstream.seen == []
    assert ProxyHandler.router.dead == {}


def call_error(base, headers):
    req = urllib.request.Request(base + "/v1/messages", data=b"{}", method="POST",
                                 headers={"Authorization": "Bearer user-login", **headers})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    return e.value.code, json.load(e.value)


@pytest.mark.parametrize("prefix", ["", "/k/wrong", "/k", f"/k/{KEY}x"])
def test_requests_without_the_key_are_refused(proxy, prefix):
    """Containers, other users and web pages can reach the port, but don't know the key."""
    origin = proxy.removesuffix(f"/k/{KEY}")
    req = urllib.request.Request(origin + prefix + "/v1/messages", data=b"{}", method="POST",
                                 headers={"Authorization": "Bearer x"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403
    assert FakeUpstream.seen == []


def test_key_is_not_forwarded(proxy):
    assert call(proxy, "/v1/messages?beta=true") == (200, "Bearer tok-a")
    assert FakeUpstream.seen[-1][0] == "/v1/messages?beta=true"


def test_count_tokens_gets_an_account_token(proxy):
    assert call(proxy, "/v1/messages/count_tokens") == (200, "Bearer tok-a")


@pytest.mark.parametrize("path", ["/v1/messages/../../api/oauth/profile", "/v1/messages/%2e%2e/x",
                                  "/v1/messages/batches", "/v1/messagesx", "/v1//messages"])
def test_only_exact_inference_paths_get_an_account_token(proxy, path):
    assert call(proxy, path)[1] == "Bearer user-login"


def test_proxy_status_tells_this_proxy_from_others(proxy, monkeypatch):
    monkeypatch.setattr(proxy_mod, "proxy_key", lambda: KEY)
    assert proxy_mod.proxy_status({"proxy_port": urllib.parse.urlsplit(proxy).port}) == "ours"
    assert proxy_mod.proxy_status({"proxy_port": ProxyHandler.upstream.port}) == "other"  # answers, can't prove
    monkeypatch.setattr(proxy_mod, "proxy_key", lambda: "another-key")
    assert proxy_mod.proxy_status({"proxy_port": urllib.parse.urlsplit(proxy).port}) == "other"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    assert proxy_mod.proxy_status({"proxy_port": free}) == "down"
