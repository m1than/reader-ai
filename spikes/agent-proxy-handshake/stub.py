"""Spike stub (never shipped): a fake Anthropic upstream plus a logging forward proxy, both on 127.0.0.1.

- Upstream on :8080. Every request is logged as {method, path, header names, auth scheme, auth value is the
  handle?}. POST /v1/messages answers a minimal valid SSE message stream; anything else gets 404 JSON.
- Forward proxy on :8081 (HTTP_PROXY / HTTPS_PROXY point at it). It logs the target of every CONNECT or absolute-URI
  request and refuses it, so any host the CLI tries besides ANTHROPIC_BASE_URL shows up in the log.

Log lines go to /out/requests.jsonl. No header values are logged except the auth scheme and whether the credential
equals the expected handle.
"""

import json
import os
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OUT = "/out/requests.jsonl"
HANDLE = os.environ.get("EXPECT_HANDLE", "")
ERROR_STATUS = int(os.environ.get("STUB_ERROR_STATUS", "0") or 0)  # 0: success; 429, 529 or 500: always that error
LOCK = threading.Lock()


def log(entry: dict) -> None:
    with LOCK, open(OUT, "a") as f:
        f.write(json.dumps(entry) + "\n")


def sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def message_stream(model: str) -> bytes:
    msg = {"id": "msg_stub", "type": "message", "role": "assistant", "model": model, "content": [],
           "stop_reason": None, "stop_sequence": None,
           "usage": {"input_tokens": 10, "output_tokens": 1, "cache_creation_input_tokens": 0,
                     "cache_read_input_tokens": 0}}
    return b"".join([
        sse("message_start", {"type": "message_start", "message": msg}),
        sse("content_block_start", {"type": "content_block_start", "index": 0,
                                    "content_block": {"type": "text", "text": ""}}),
        sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                                    "delta": {"type": "text_delta", "text": "stub-ok"}}),
        sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
        sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                              "usage": {"output_tokens": 3}}),
        sse("message_stop", {"type": "message_stop"}),
    ])


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:  # silence stderr access log
        pass

    def _record(self, body: bytes) -> None:
        auth = self.headers.get("Authorization", "")
        xkey = self.headers.get("x-api-key", "")
        scheme = auth.split(" ", 1)[0] if auth else ("x-api-key" if xkey else "")
        cred = auth.split(" ", 1)[1] if " " in auth else xkey
        model = ""
        tools = None
        max_tokens = None
        try:
            payload = json.loads(body or b"{}")
            model = payload.get("model", "")
            tools = [t.get("name") for t in payload.get("tools", [])] if isinstance(payload, dict) else None
            max_tokens = payload.get("max_tokens") if isinstance(payload, dict) else None
        except ValueError:
            pass
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        log({"server": "upstream", "t": time.time(), "method": self.command, "path": self.path.split("?")[0],
             "query": "?" in self.path, "query_string": query,
             "query_keys": sorted(p.split("=", 1)[0] for p in query.split("&")) if query else [],
             "max_tokens": max_tokens,
             "header_names": sorted(k.lower() for k in self.headers.keys()),
             "auth_scheme": scheme, "credential_is_handle": bool(HANDLE) and cred == HANDLE,
             "anthropic_beta": self.headers.get("anthropic-beta", ""),
             "anthropic_version": self.headers.get("anthropic-version", ""),
             "model": model, "request_tools": tools, "body_bytes": len(body)})

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_POST(self) -> None:
        body = self._body()
        self._record(body)
        if self.path.split("?")[0] == "/v1/messages" and ERROR_STATUS:
            kinds = {429: "rate_limit_error", 529: "overloaded_error", 500: "api_error"}
            data = json.dumps({"type": "error", "error": {"type": kinds.get(ERROR_STATUS, "api_error"),
                                                          "message": "stub"}}).encode()
            self.send_response(ERROR_STATUS)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if ERROR_STATUS == 429:
                self.send_header("retry-after", "1")
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.split("?")[0] == "/v1/messages":
            try:
                model = json.loads(body).get("model", "stub")
            except ValueError:
                model = "stub"
            data = message_stream(model)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._not_found()

    def do_GET(self) -> None:
        self._record(b"")
        self._not_found()

    def do_HEAD(self) -> None:
        self._record(b"")
        self._not_found()

    def _not_found(self) -> None:
        data = json.dumps({"type": "error", "error": {"type": "not_found_error", "message": "stub"}}).encode()
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Proxy(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline(65536).decode("latin-1").strip()
        method, _, rest = line.partition(" ")
        target = rest.rsplit(" ", 1)[0]
        log({"server": "forward_proxy", "method": method, "target": target})
        self.wfile.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")


def main() -> None:
    up = ThreadingHTTPServer(("127.0.0.1", 8080), Upstream)
    px = socketserver.ThreadingTCPServer(("127.0.0.1", 8081), Proxy)
    for srv in (up, px):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("stub-ready", flush=True)
    sys.stdin.read()  # run until stdin closes


if __name__ == "__main__":
    main()
