"""The local HTTP proxy that swaps in the chosen account's access token."""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler

from . import addons, measure
from .config import log
from .conversations import (
    Conversation,
    ConversationPins,
    Question,
    cache_ttls,
    claude_code_conversation,
    how_to_start_fresh,
    parse,
    response_usage,
)
from .credentials import AuthDead, access_token
from .router import Router

FORCE_HEADER = "x-roulette-account"  # set via ANTHROPIC_CUSTOM_HEADERS to pin an account

HEAD_BYTES = 16384  # enough of a response to hold its usage block
TAIL_BYTES = 4096  # the final message_delta, with output_tokens
PING_EVERY_S = 10  # keep-alive while a held request waits for an answer

HOP_HEADERS = {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "te",
               "trailer", "upgrade", "host", "content-length"}


def is_inference(path: str) -> bool:
    return path.split("?")[0].startswith("/v1/messages")


def is_turn(path: str) -> bool:
    """A model turn, as opposed to count_tokens or batches."""
    return path.split("?")[0] == "/v1/messages"


class ProxyHandler(BaseHTTPRequestHandler):
    router: Router
    pins: ConversationPins | None = None
    upstream: urllib.parse.SplitResult
    debug = False

    def log_message(self, fmt, *args):
        pass

    def _connect(self):
        cls = http.client.HTTPSConnection if self.upstream.scheme == "https" else http.client.HTTPConnection
        return cls(self.upstream.netloc, timeout=600)

    def _send_upstream(self, name: str | None, body: bytes, plain: bool = False):
        """name=None passes the request through with Claude Code's own credentials.

        plain=True asks for an uncompressed response, so its token usage can be read.
        """
        drop = {FORCE_HEADER, "accept-encoding"} if plain else {FORCE_HEADER}
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in HOP_HEADERS and k.lower() not in drop}
        if name is not None:
            auth_key = next((k for k in headers if k.lower() == "authorization"), None)
            if auth_key and headers[auth_key].startswith("Bearer "):
                headers[auth_key] = f"Bearer {access_token(name)}"
        conn = self._connect()
        conn.request(self.command, self.path, body=body or None, headers=headers)
        return conn, conn.getresponse()

    def _open_stream(self) -> None:
        """Answer now with an event stream, so the client's first-byte watchdog stays quiet."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.stream_open = True

    def _event(self, event: str, data: dict) -> None:
        self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
        self.wfile.flush()

    def _fail(self, status: int, error: dict) -> None:
        """An Anthropic-style error, as a stream event once the stream is open."""
        try:
            if self.stream_open:
                self._event("error", {"type": "error", "error": error})
                return
            body = json.dumps({"type": "error", "error": error}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _hold(self, q: Question, streaming: bool) -> str | None:
        """Ask `q` while the request waits (up to pin_hold_s). Returns the account to send it
        to, or None if it was cancelled or the client went away."""
        answer: dict = {}
        done = threading.Event()

        def answered(button):
            answer.setdefault("button", button)
            done.set()

        self.pins.ask_question(q, answered)
        deadline = time.time() + self.router.cfg["pin_hold_s"]
        try:
            if streaming:
                self._open_stream()
            while not done.wait(PING_EVERY_S if streaming else 1) and time.time() < deadline:
                if streaming:
                    self._event("ping", {"type": "ping"})
        except (BrokenPipeError, ConnectionResetError):
            self.pins.resolve(q, None)
            log(f"conv={q.key[:8]}: client gave up while waiting for the answer")
            return None
        account = self.pins.resolve(q, answer.get("button") if done.is_set() else None)
        if account is None:
            self._fail(400, {"type": "invalid_request_error",
                             "message": "Message not sent: you cancelled it in Claude Roulette. To start fresh, "
                                        f"{how_to_start_fresh(q.client)}. To continue this conversation, "
                                        "send the message again."})
        return account

    def _forward(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        conn = resp = name = None
        self.stream_open = False
        cfg = self.router.cfg
        route = addons.route(self.headers, body) if is_inference(self.path) else None
        turn = is_turn(self.path)
        req = parse(body) if turn else None
        conv: Conversation | None = None
        if req is not None:
            conv = route.conversation if route else (
                claude_code_conversation(req) if cfg["code_mode"] != "off" else None)
        pinned = bool(conv and self.pins and not (route and route.passthrough)
                      and (route or cfg["code_mode"] == "pin"))
        plain = turn
        if not is_inference(self.path) or (route and route.passthrough):
            try:
                conn, resp = self._send_upstream(None, body, plain)
            except (OSError, http.client.HTTPException) as e:
                log(f"passthrough error: {e}")
        else:
            forced = self.headers.get(FORCE_HEADER) or (route and route.account)
            if not forced and pinned:
                forced, question = self.pins.account_for(conv, cache_ttls(req))
                if question:
                    forced = self._hold(question, req.get("stream") is True)
                    if forced is None:
                        return
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
                    conn, resp = self._send_upstream(candidate, body, plain)
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
            if self.stream_open:
                self._fail(502, {"type": "api_error", "message": "claude-roulette: no upstream account reachable"})
            else:
                self.send_error(502, "no upstream account reachable")
            return
        log(f"{self.command} {self.path.split('?')[0]} -> {name or 'passthrough'} {resp.status}")
        if self.stream_open:  # headers already sent while the request was held
            if resp.status != 200:
                data = resp.read()
                conn.close()
                try:
                    error = json.loads(data)["error"]
                except (ValueError, KeyError, TypeError):
                    error = {"type": "api_error", "message": data[:500].decode(errors="replace")}
                self._fail(resp.status, error)
                return
        else:
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in HOP_HEADERS:
                    self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()
        head = tail = b""
        try:
            while chunk := resp.read1(65536):  # stream SSE through as it arrives
                self.wfile.write(chunk)
                self.wfile.flush()
                if plain:
                    if len(head) < HEAD_BYTES:
                        head += chunk
                    tail = (tail + chunk)[-TAIL_BYTES:]
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            conn.close()
        usage = response_usage(head[:HEAD_BYTES], tail) if plain else {}
        if plain:
            self._after_turn(name, resp, req, conv, pinned, usage)
        if route and route.done:
            try:
                route.done(name, resp.status, usage)
            except Exception as e:
                log(f"add-on error after the response: {e!r}")

    def _after_turn(self, name, resp, req, conv, pinned, usage) -> None:
        model = (req or {}).get("model", "?")
        try:
            if name and resp.status == 200 and usage:
                if pinned:
                    self.pins.record(conv, usage)
                measure.record(name, model, usage, dict(resp.getheaders()))
        except Exception as e:  # bookkeeping must never break the proxy
            log(f"after-turn error: {e!r}")
        if conv:
            ttls = sorted({t for t in cache_ttls(req)})
            ttl = f" ttl={','.join(f'{t // 60}m' for t in ttls)}" if ttls else ""
            log(f"  {conv.client} conv={conv.key[:8]} lane={conv.lane} {model}{ttl} "
                + " ".join(f"{k}={v}" for k, v in usage.items()))

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _forward


def proxy_running(cfg: dict) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", cfg["proxy_port"]), timeout=0.5):
            return True
    except OSError:
        return False
