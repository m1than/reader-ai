"""Spike stub (never shipped): a fake Anthropic upstream plus a logging forward proxy, both on 127.0.0.1.

- Upstream on :8080. Every request is logged as {method, path, header names, auth scheme, auth value is the
  handle?}. POST /v1/messages answers a minimal valid SSE message stream; anything else gets 404 JSON.
- Forward proxy on :8081 (HTTP_PROXY / HTTPS_PROXY point at it). It logs the target of every CONNECT or absolute-URI
  request and refuses it, so any host the CLI tries besides ANTHROPIC_BASE_URL shows up in the log.

Log lines go to /out/requests.jsonl. No header values are logged except the auth scheme and whether the credential
equals the expected handle.

Scenario `websearch` (STUB_SCENARIO=websearch; spikes/web-search § 5a, 2026-11-23): a main-loop request that offers
the client tool `WebSearch` and holds no tool_result yet is answered with a `tool_use` of WebSearch; a request whose
`tools` hold a `web_search_*` server tool (the CLI's nested search request, hypothesis H-WS-1) is answered with a
`server_tool_use` + `web_search_tool_result` (two synthetic results with page_age; STUB_WS_ERROR=<code> answers an
error result instead) and a short text; any other request gets the final text. For these requests the log also holds
the request's `tools` in full (the CLI's own definitions, no secret), their canonical sha256s, the top-level keys,
the system prompt's size only, and a skeleton of the messages (roles, block types, the first 200 characters of each
text and tool_result). Everything is synthetic: no book text and no real token exist in the container.
"""

import hashlib
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
DELAY_S = float(os.environ.get("STUB_DELAY_S", "0") or 0)  # seconds before answering /v1/messages (deadline tests)
LOCK = threading.Lock()
SCENARIO = os.environ.get("STUB_SCENARIO", "")
WS_ERROR = os.environ.get("STUB_WS_ERROR", "")
QUERY = "synthetic freshness query"


def canonical_sha256(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                          .encode()).hexdigest()


def skeleton(messages: list) -> list:
    """Roles and block types of each message, with at most 200 characters of text per block."""
    out = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            out.append({"role": m.get("role"), "text": content[:200]})
            continue
        blocks = []
        for b in content or []:
            e = {"type": b.get("type")}
            if b.get("type") == "text":
                e["text"] = b.get("text", "")[:200]
            elif b.get("type") in ("tool_use", "server_tool_use"):
                e["name"], e["input"] = b.get("name"), b.get("input")
            elif b.get("type") == "tool_result":
                c = b.get("content")
                e["content"] = (json.dumps(c, ensure_ascii=False) if not isinstance(c, str) else c)[:200]
                e["is_error"] = b.get("is_error")
            blocks.append(e)
        out.append({"role": m.get("role"), "blocks": blocks})
    return out


def describe(payload: dict) -> dict:
    tools = payload.get("tools") or []
    system = payload.get("system")
    return {
        "top_level_keys": sorted(payload.keys()),
        "tools": tools,
        "tools_sha256": [canonical_sha256(t) for t in tools],
        "tool_choice": payload.get("tool_choice"),
        "thinking": payload.get("thinking"),
        "system_chars": len(json.dumps(system, ensure_ascii=False)) if system is not None else 0,
        # the nested search request's own short system prompt (the CLI's, no secret); never the main loop's
        "search_system": system if is_search_request(payload) else None,
        "output_config": payload.get("output_config"),
        "context_management": payload.get("context_management"),
        "metadata_keys": sorted((payload.get("metadata") or {}).keys()),
        "stream": payload.get("stream"),
        "messages": skeleton(payload.get("messages") or []),
        "query_in_body": QUERY in json.dumps(payload, ensure_ascii=False),
    }


def is_search_request(payload: dict) -> bool:
    return any(str(t.get("type", "")).startswith("web_search") for t in payload.get("tools") or [])


def offers_websearch(payload: dict) -> bool:
    return any(t.get("name") == "WebSearch" for t in payload.get("tools") or [])


def has_tool_result(payload: dict) -> bool:
    for m in payload.get("messages") or []:
        if isinstance(m.get("content"), list) and any(b.get("type") == "tool_result" for b in m["content"]):
            return True
    return False


def websearch_stream(payload: dict) -> bytes:
    model = payload.get("model", "stub")
    msg = {"id": "msg_stub", "type": "message", "role": "assistant", "model": model, "content": [],
           "stop_reason": None, "stop_sequence": None,
           "usage": {"input_tokens": 10, "output_tokens": 1, "cache_creation_input_tokens": 0,
                     "cache_read_input_tokens": 0}}
    parts = [sse("message_start", {"type": "message_start", "message": msg})]
    usage: dict = {"output_tokens": 5}
    if is_search_request(payload):
        parts += [
            sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {
                "type": "server_tool_use", "id": "srvtoolu_stub01", "name": "web_search", "input": {}}}),
            sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {
                "type": "input_json_delta", "partial_json": json.dumps({"query": QUERY})}}),
            sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ]
        result = ({"type": "web_search_tool_result_error", "error_code": WS_ERROR} if WS_ERROR else [
            {"type": "web_search_result", "url": "https://example.com/new-edition", "title": "Synthetic Book, 5th ed.",
             "encrypted_content": "c3R1Yg", "page_age": "March 3, 2026"},
            {"type": "web_search_result", "url": "https://docs.example.org/guide", "title": "Synthetic Docs",
             "encrypted_content": "c3R1Yg", "page_age": None},
        ])
        parts += [
            sse("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {
                "type": "web_search_tool_result", "tool_use_id": "srvtoolu_stub01", "content": result}}),
            sse("content_block_stop", {"type": "content_block_stop", "index": 1}),
            sse("content_block_start", {"type": "content_block_start", "index": 2,
                                        "content_block": {"type": "text", "text": ""}}),
            sse("content_block_delta", {"type": "content_block_delta", "index": 2,
                                        "delta": {"type": "text_delta", "text": "stub search summary"}}),
            sse("content_block_stop", {"type": "content_block_stop", "index": 2}),
        ]
        usage["server_tool_use"] = {"web_search_requests": 0 if WS_ERROR else 1}
        stop = "end_turn"
    elif offers_websearch(payload) and not has_tool_result(payload):
        parts += [
            sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {
                "type": "tool_use", "id": "toolu_stub01", "name": "WebSearch", "input": {}}}),
            sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {
                "type": "input_json_delta", "partial_json": json.dumps({"query": QUERY})}}),
            sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ]
        stop = "tool_use"
    else:
        parts += [
            sse("content_block_start", {"type": "content_block_start", "index": 0,
                                        "content_block": {"type": "text", "text": ""}}),
            sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                                        "delta": {"type": "text_delta", "text": "stub-final"}}),
            sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ]
        stop = "end_turn"
    parts += [
        sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                              "usage": usage}),
        sse("message_stop", {"type": "message_stop"}),
    ]
    return b"".join(parts)


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
        if DELAY_S and self.path.split("?")[0] == "/v1/messages":
            time.sleep(DELAY_S)
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
                payload = json.loads(body)
            except ValueError:
                payload = {}
            model = payload.get("model", "stub")
            if SCENARIO == "websearch":
                kind = ("search" if is_search_request(payload)
                        else "main" if offers_websearch(payload) else "other")
                log({"server": "upstream", "t": time.time(), "websearch_kind": kind, "model": model,
                     "max_tokens": payload.get("max_tokens"), **describe(payload)})
                data = websearch_stream(payload)
            else:
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
