import json
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from claude_roulette import addons, credentials, science
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
    ProxyHandler.key = "test-key"
    server = serve(ProxyHandler)
    FakeUpstream.seen = []
    yield f"http://127.0.0.1:{server.server_port}/k/test-key", cfg, pins
    server.shutdown()
    upstream.shutdown()
    CONFIG_FILE.unlink()
    addons._loaded = None


def call(base, body, surface="operon", auth="Bearer user-login"):
    req = urllib.request.Request(base + "/v1/messages", data=body, method="POST", headers={
        "Authorization": auth, "X-Anthropic-Surface": surface, "Accept-Encoding": "gzip"})
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


def test_requests_without_a_login_pass_through(setup):
    base, cfg, pins = setup  # e.g. an API-key app that inherited the URL from launchd
    call(base, science_body("conv1"), auth="Basic not-a-login")
    assert FakeUpstream.seen[0][0] == "Basic not-a-login"
    assert "conv1" not in pins.pins


def test_other_clients_are_not_claimed(setup):
    base, cfg, pins = setup
    call(base, science_body("conv1"), surface="cli")
    assert pins.pins.get("conv1", {}).get("client") != "science"


def test_science_is_off_unless_enabled():
    addons._loaded = None
    assert addons.load() == {}


def test_install_enables_science_only_if_found_and_unset(monkeypatch):
    from claude_roulette import cli, science
    CONFIG_FILE.unlink(missing_ok=True)
    monkeypatch.setattr(science, "app_installed", lambda: False)
    assert cli.enable_detected_addons() is None and not CONFIG_FILE.exists()
    monkeypatch.setattr(science, "app_installed", lambda: True)
    assert cli.enable_detected_addons()
    assert json.loads(CONFIG_FILE.read_text())["addons"] == ["science"]
    save_json(CONFIG_FILE, {"addons": []})  # turned off by hand: stays off
    assert cli.enable_detected_addons() is None
    CONFIG_FILE.unlink()


class FakeLaunchd:
    """Stands in for launchctl, pgrep and ps, so tests never touch the real launchd."""

    def __init__(self, env=None, daemons=()):
        self.env = dict(env or {})
        self.daemons = list(daemons)  # ANTHROPIC_BASE_URL of each running service, or None

    def run(self, cmd, **kw):
        out = ""
        if cmd[:2] == ["launchctl", "getenv"]:
            out = self.env.get(cmd[2], "")
        elif cmd[:2] == ["launchctl", "setenv"]:
            self.env[cmd[2]] = cmd[3]
        elif cmd[:2] == ["launchctl", "unsetenv"]:
            self.env.pop(cmd[2], None)
        elif cmd[0] == "pgrep":
            out = "\n".join(str(100 + i) for i in range(len(self.daemons)))
        elif cmd[0] == "ps":
            url = self.daemons[int(cmd[-1]) - 100]
            out = "/x/claude-science serve --app HOME=/h" + (f" ANTHROPIC_BASE_URL={url}" if url else "")
        return subprocess.CompletedProcess(cmd, 0, out, "")


BASE = "http://127.0.0.1:8789"
URL = BASE + "/k/secret"


def test_launchd_url_is_set_and_only_our_own_removed(monkeypatch):
    fake = FakeLaunchd()
    monkeypatch.setattr(science.subprocess, "run", fake.run)
    assert science.connect_launchd(URL) == "set" and fake.env["ANTHROPIC_BASE_URL"] == URL
    assert science.connect_launchd(URL) == "unchanged"
    assert science.disconnect_launchd(BASE) and "ANTHROPIC_BASE_URL" not in fake.env
    fake.env["ANTHROPIC_BASE_URL"] = "https://gateway.example"  # set by hand: kept
    assert not science.disconnect_launchd(BASE)
    assert fake.env["ANTHROPIC_BASE_URL"] == "https://gateway.example"


@pytest.mark.parametrize("daemons, problem", [
    ([], None),
    ([URL], None),
    ([None], "without the proxy"),
    ([URL, "https://api.anthropic.com"], "without the proxy"),
    ([BASE + "/k/old"], "outdated"),
])
def test_warns_about_a_science_service_that_bypasses_the_proxy(monkeypatch, daemons, problem):
    monkeypatch.setattr(science.subprocess, "run", FakeLaunchd(daemons=daemons).run)
    warning = science.daemon_warning(URL, BASE)
    assert warning is None if problem is None else problem in warning


def test_install_sets_the_url_only_while_science_is_on(monkeypatch):
    from claude_roulette import cli, client_settings
    fake = FakeLaunchd(daemons=[None])
    monkeypatch.setattr(science.subprocess, "run", fake.run)
    cfg = {**DEFAULTS, "accounts": ["a"]}
    save_json(CONFIG_FILE, {"addons": ["science"]})
    report = cli.connect_science(cfg)
    assert fake.env["ANTHROPIC_BASE_URL"] == client_settings.proxy_url(cfg)
    assert "Quit Claude Science" in report and client_settings.proxy_key() not in report
    save_json(CONFIG_FILE, {"addons": []})
    assert "removed" in cli.connect_science(cfg) and "ANTHROPIC_BASE_URL" not in fake.env
    CONFIG_FILE.unlink()


def test_title_comes_from_sciences_database(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setattr(science, "SCIENCE_HOME", tmp_path)
    (tmp_path / "active-org.json").write_text(json.dumps({"org_uuid": "org1"}))
    (tmp_path / "orgs" / "org1").mkdir(parents=True)
    con = sqlite3.connect(tmp_path / "orgs" / "org1" / "operon-cli.db")
    con.execute("CREATE TABLE frames (id TEXT, name TEXT)")
    con.execute("INSERT INTO frames VALUES ('6176e3bb-b447-4f09-98ec-3ecfcee279df', 'Organotropism analysis')")
    con.commit()
    con.close()
    assert science.conversation_title("6176e3bbb4474f0998ec3ecfcee279df") == "Organotropism analysis"
    assert science.conversation_title("00000000b4474f0998ec3ecfcee279df") is None
    assert science.conversation_title("x' OR '1'='1") is None
