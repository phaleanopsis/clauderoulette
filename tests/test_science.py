import json
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from claude_roulette import addons, credentials
from claude_roulette.config import CONFIG_FILE, DEFAULTS, save_json
from claude_roulette.conversations import PINS_FILE, ConversationPins
from claude_roulette.proxy import ProxyHandler
from claude_roulette.router import Router
from claude_roulette.science import DEFAULTS as SCIENCE_DEFAULTS
from claude_roulette.science import science_conversation


class FakeUpstream(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        FakeUpstream.seen.append((self.headers.get("Authorization"), self.headers.get("Accept-Encoding")))
        body = json.dumps({"usage": {"input_tokens": 5, "cache_read_input_tokens": 1000}}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def science_body(session: str) -> bytes:
    return json.dumps({"model": "m", "metadata": {"user_id": json.dumps({"session_id": session})},
                       "system": [{"type": "text", "text": "x", "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
                       }).encode()


@pytest.fixture
def setup():
    for name in ("a", "b"):
        credentials.write_secret(name, json.dumps({"claudeAiOauth": {
            "accessToken": f"tok-{name}", "refreshToken": "r", "expiresAt": int((time.time() + 3600) * 1000)}}))
    credentials._creds.clear()
    PINS_FILE.unlink(missing_ok=True)
    upstream = serve(FakeUpstream)
    cfg = {**DEFAULTS, **SCIENCE_DEFAULTS, "accounts": ["a", "b"], "primary": "a"}
    router = Router(cfg)
    save_json(CONFIG_FILE, {"addons": ["science"]})
    addons._loaded = None
    addon = addons.load()["science"]
    addon.start(router)
    ProxyHandler.router = router
    ProxyHandler.pins = pins = ConversationPins(router, ask=lambda *a, **k: None)
    ProxyHandler.upstream = urllib.parse.urlsplit(f"http://127.0.0.1:{upstream.server_port}")
    server = serve(ProxyHandler)
    FakeUpstream.seen = []
    yield f"http://127.0.0.1:{server.server_port}", cfg, pins
    server.shutdown()
    upstream.shutdown()
    CONFIG_FILE.unlink()
    addons._loaded = None


def call(base, body, surface="operon"):
    req = urllib.request.Request(base + "/v1/messages", data=body, method="POST", headers={
        "Authorization": "Bearer user-login", "X-Anthropic-Surface": surface, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req) as resp:
        return resp.status


def test_parses_conversation_and_lane():
    conv = science_conversation({"X-Anthropic-Surface": "operon"}, science_body("conv1:abc"))
    assert (conv.key, conv.lane, conv.main, conv.client) == ("conv1", "abc", False, "science")
    assert science_conversation({"X-Anthropic-Surface": "operon"}, science_body("conv1")).main
    assert science_conversation({}, science_body("conv1")) is None


def test_conversation_stays_on_its_account(setup):
    base, cfg, pins = setup
    call(base, science_body("conv1"))
    first = pins.pins["conv1"]["account"]
    pins.router.current = "b" if first == "a" else "a"  # the router moving on doesn't move the conversation
    call(base, science_body("conv1:frame:abc"))
    assert [t for t, _ in FakeUpstream.seen] == [f"Bearer tok-{first}"] * 2
    assert FakeUpstream.seen[0][1] in (None, "identity")  # asked for uncompressed, to read usage
    assert pins.pins["conv1"]["context"] == 1005 and pins.pins["conv1"]["client"] == "science"
    assert pins.pins["conv1"]["partial_until"] - time.time() > 3000  # the 1h mark


def test_observe_mode_passes_through(setup):
    base, cfg, pins = setup
    cfg["science_mode"] = "observe"
    call(base, science_body("conv1"))
    assert FakeUpstream.seen[0][0] == "Bearer user-login"
    assert "conv1" not in pins.pins


def test_other_clients_are_not_claimed(setup):
    base, cfg, pins = setup
    call(base, science_body("conv1"), surface="cli")
    assert pins.pins.get("conv1", {}).get("client") != "science"


def test_science_is_off_unless_enabled():
    addons._loaded = None
    assert addons.load() == {}
