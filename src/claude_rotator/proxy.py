"""The local HTTP proxy that swaps in the chosen account's access token."""

from __future__ import annotations

import http.client
import socket
import urllib.parse
from http.server import BaseHTTPRequestHandler

from .config import log
from .credentials import AuthDead, access_token
from .router import Router

FORCE_HEADER = "x-rotator-account"  # set via ANTHROPIC_CUSTOM_HEADERS to pin an account

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
