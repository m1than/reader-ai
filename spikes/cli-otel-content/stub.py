"""Spike stub (never shipped): fake Anthropic upstream, logging forward proxy and capturing OTLP/HTTP receiver.

- Upstream :8080. POST /v1/messages answers a minimal SSE stream whose text is the response canary.
- Forward proxy :8081 (HTTP_PROXY / HTTPS_PROXY). Logs and refuses every target, so any other host shows up.
- OTLP/HTTP receiver :4318. Stores every request body (gunzipped) as /out/otlp/<n>.bin and logs path, header names
  and whether an Authorization header was present. Answers 200 with an empty protobuf body.

The current probe mode is read from /tmp/mode (written by probe.py), so captures are attributed to a mode.
"""

import gzip
import json
import os
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OUT = "/out/requests.jsonl"
CANARY_RESPONSE = os.environ["CANARY_RESPONSE"]
LOCK = threading.Lock()
COUNTER = [0]


def mode() -> str:
    try:
        with open("/tmp/mode") as f:
            return f.read().strip()
    except OSError:
        return ""


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
                                    "delta": {"type": "text_delta", "text": f"stub {CANARY_RESPONSE}"}}),
        sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
        sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                              "usage": {"output_tokens": 3}}),
        sse("message_stop", {"type": "message_stop"}),
    ])


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_name = ""

    def log_message(self, *args: object) -> None:
        pass

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        if self.headers.get("Content-Encoding", "").lower() == "gzip":
            body = gzip.decompress(body)
        return body

    def _send(self, status: int, data: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Upstream(Handler):
    def do_POST(self) -> None:
        body = self._body()
        path = self.path.split("?")[0]
        log({"server": "upstream", "mode": mode(), "method": "POST", "path": path, "body_bytes": len(body)})
        if path == "/v1/messages":
            try:
                model = json.loads(body).get("model", "stub")
            except ValueError:
                model = "stub"
            self._send(200, message_stream(model), "text/event-stream")
            return
        self._send(404, b'{"type":"error","error":{"type":"not_found_error","message":"stub"}}', "application/json")

    def do_GET(self) -> None:
        log({"server": "upstream", "mode": mode(), "method": "GET", "path": self.path.split("?")[0]})
        self._send(404, b'{"type":"error","error":{"type":"not_found_error","message":"stub"}}', "application/json")


class Otlp(Handler):
    def do_POST(self) -> None:
        body = self._body()
        with LOCK:
            COUNTER[0] += 1
            n = COUNTER[0]
        name = f"/out/otlp/{n:04d}.bin"
        with open(name, "wb") as f:
            f.write(body)
        log({"server": "otlp", "mode": mode(), "n": n, "path": self.path, "file": name, "bytes": len(body),
             "content_type": self.headers.get("Content-Type", ""),
             "header_names": sorted(k.lower() for k in self.headers.keys()),
             "authorization_present": "authorization" in {k.lower() for k in self.headers.keys()}})
        self._send(200, b"", "application/x-protobuf")


class Proxy(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline(65536).decode("latin-1").strip()
        method, _, rest = line.partition(" ")
        log({"server": "forward_proxy", "mode": mode(), "method": method, "target": rest.rsplit(" ", 1)[0]})
        self.wfile.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")


def main() -> None:
    servers = [ThreadingHTTPServer(("127.0.0.1", 8080), Upstream), ThreadingHTTPServer(("127.0.0.1", 4318), Otlp),
               socketserver.ThreadingTCPServer(("127.0.0.1", 8081), Proxy)]
    for srv in servers:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("stub-ready", flush=True)
    sys.stdin.read()
    time.sleep(0.1)


if __name__ == "__main__":
    main()
