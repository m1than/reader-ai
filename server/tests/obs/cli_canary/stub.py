"""CLI canary harness stub (SRV-OBS INV-OBS-2, FR-OBS-6 b; moved from spikes/cli-otel-content).

Fake Anthropic upstream, logging forward proxy and capturing OTLP/HTTP receiver, all on 127.0.0.1:
- upstream :8080: POST /v1/messages answers a minimal SSE stream whose text is the response canary;
- forward proxy :8081 (HTTP_PROXY / HTTPS_PROXY): logs and refuses every target, so any other host shows up;
- OTLP/HTTP receiver :4318: stores every request body (gunzipped) as /out/otlp/<n>.bin and logs path, header names
  and whether an Authorization header was present.
The current probe mode is read from /tmp/mode, so captures are attributed to a mode.
In a `websearch-*` mode the upstream answers the CLI's WebSearch flow instead (moved from spikes/web-search-otel,
the shapes of spikes/web-search § 3a): a main-loop request offering WebSearch with no tool_result yet gets a
`tool_use` of WebSearch with the query canary; the nested search request (a tool of type web_search_*) gets a
server_tool_use, a web_search_tool_result whose title and URL hold the title and URL canaries, and the summary
canary as text; any other request (the final main-loop turn) gets "stub-final". Each such upstream log line
names the kind (`main`, `search`, `final`).
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
QUERY = os.environ["CANARY_QUERY"]
TITLE = os.environ["CANARY_TITLE"]
URL = os.environ["CANARY_URL"]
SUMMARY = os.environ["CANARY_SUMMARY"]
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


def is_search_request(payload: dict) -> bool:
    return any(str(t.get("type", "")).startswith("web_search") for t in payload.get("tools") or [])


def offers_websearch(payload: dict) -> bool:
    return any(t.get("name") == "WebSearch" for t in payload.get("tools") or [])


def has_tool_result(payload: dict) -> bool:
    for m in payload.get("messages") or []:
        if isinstance(m.get("content"), list) and any(b.get("type") == "tool_result" for b in m["content"]):
            return True
    return False


def text_block(index: int, text: str) -> list[bytes]:
    return [
        sse("content_block_start", {"type": "content_block_start", "index": index,
                                    "content_block": {"type": "text", "text": ""}}),
        sse("content_block_delta", {"type": "content_block_delta", "index": index,
                                    "delta": {"type": "text_delta", "text": text}}),
        sse("content_block_stop", {"type": "content_block_stop", "index": index}),
    ]


def websearch_stream(payload: dict) -> tuple[bytes, str]:
    msg = {"id": "msg_stub", "type": "message", "role": "assistant", "model": payload.get("model", "stub"),
           "content": [], "stop_reason": None, "stop_sequence": None,
           "usage": {"input_tokens": 10, "output_tokens": 1, "cache_creation_input_tokens": 0,
                     "cache_read_input_tokens": 0}}
    parts = [sse("message_start", {"type": "message_start", "message": msg})]
    usage: dict = {"output_tokens": 5}
    if is_search_request(payload):
        kind = "search"
        parts += [
            sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {
                "type": "server_tool_use", "id": "srvtoolu_stub01", "name": "web_search", "input": {}}}),
            sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {
                "type": "input_json_delta", "partial_json": json.dumps({"query": QUERY})}}),
            sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
            sse("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {
                "type": "web_search_tool_result", "tool_use_id": "srvtoolu_stub01", "content": [
                    {"type": "web_search_result", "url": f"https://{URL}.example/page", "title": f"Title {TITLE}",
                     "encrypted_content": "c3R1Yg", "page_age": "March 3, 2026"}]}}),
            sse("content_block_stop", {"type": "content_block_stop", "index": 1}),
            *text_block(2, f"Summary {SUMMARY}"),
        ]
        usage["server_tool_use"] = {"web_search_requests": 1}
        stop = "end_turn"
    elif offers_websearch(payload) and not has_tool_result(payload):
        kind = "main"
        parts += [
            sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {
                "type": "tool_use", "id": "toolu_stub01", "name": "WebSearch", "input": {}}}),
            sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {
                "type": "input_json_delta", "partial_json": json.dumps({"query": QUERY})}}),
            sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ]
        stop = "tool_use"
    else:
        kind = "final"
        parts += text_block(0, "stub-final")
        stop = "end_turn"
    parts += [
        sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                              "usage": usage}),
        sse("message_stop", {"type": "message_stop"}),
    ]
    return b"".join(parts), kind


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

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
        if path == "/v1/messages" and mode().startswith("websearch"):
            try:
                payload = json.loads(body)
            except ValueError:
                payload = {}
            data, kind = websearch_stream(payload)
            log({"server": "upstream", "mode": mode(), "method": "POST", "path": path, "kind": kind,
                 "body_bytes": len(body)})
            self._send(200, data, "text/event-stream")
            return
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
