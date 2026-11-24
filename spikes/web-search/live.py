"""Spike web-search § 5b (never shipped): ONE live WebSearch run through the Agent SDK's bundled CLI, behind a local
recording reverse proxy, to check H-WS-1..6 and H-FRESH-2 against the real upstream. Run by the owner only, through
spikes/web-search/run-live.sh, which pipes the token on stdin (never printed, never written).

Inside the container (image reader-spike-agent-proxy-handshake: claude-agent-sdk 0.2.187, SRV-AGENT's pin):
  python -I /spike/live.py           with CLAUDE_CODE_OAUTH_TOKEN exported by the entry shell, CAP_USD in the env
  /out/numbers/live.json             numbers, key names and shapes only (no token, header value, prompt, query text,
                                     result title, URL or model text)

The proxy on 127.0.0.1:8080 (ANTHROPIC_BASE_URL) forwards only `POST /v1/messages` to https://api.anthropic.com:
  - the Authorization header passes through untouched and is never read beyond "is it present";
  - `accept-encoding` is dropped so the SSE stream comes back as identity and can be parsed;
  - a nested search request (a `tools` entry whose type starts with `web_search`) gets `max_uses` rewritten to 1
    (SRV-AGENT proposed FR-AGENT-16) and at most SEARCHES_MAX (1) such requests are forwarded; more get 403;
  - every request is refused with 403 once spent + its worst case would pass CAP_USD (default 0.10): worst case =
    body characters / 2.0 tokens at the model's 1-hour cache-write rate + max_tokens at the output rate, plus, for a
    search request, $0.01 and T_MAX = 20,000 result tokens at the 1-hour rate (H-WS-3's chosen upper end);
  - only Haiku 5.5 is forwarded; anything else gets 403 and is recorded;
  - a request whose `tools` hold any server tool (an entry with a `type`) other than `web_search_20250305`, or a
    non-empty `mcp_servers`, gets 403;
  - before forwarding, a `reserve` line (the request's worst case) is appended to /out/numbers/ledger.jsonl and
    flushed; after the response, a `settle` line with the cost. A 200 that is not an SSE stream is parsed as JSON for
    its usage; if no usage can be read, or the upstream call fails after sending, the settle line carries the worst
    case. A run killed in between leaves its reserve lines, which run-live.sh counts as spent.
The SDK run aborts as soon as the init tool list is not exactly ["WebSearch"]. The proxy server's error handler prints
only the exception type, never a traceback with request data. The SDK's own `max_budget_usd` is the same cap as a
second guard.

Hardened 2026-11-23 after the owner's run at d9f96f0 (deep-reviewer round 2 list); the run's results came from the
unhardened version.
"""

import asyncio
import http.client
import json
import os
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

UPSTREAM = "api.anthropic.com"
# Zero-credit dry run only: DRY_UPSTREAM_PORT=8080 sends to the spike stub over plain HTTP on 127.0.0.1 instead.
DRY_PORT = int(os.environ.get("DRY_UPSTREAM_PORT", "0") or 0)
PROXY_PORT = int(os.environ.get("PROXY_PORT", "8080"))
CAP_USD = float(os.environ.get("CAP_USD", "0.10"))
MODEL = "claude-haiku-5-5"            # fixed: the owner approved Haiku only (2026-11-23)
SEARCH_TOOL_TYPE = "web_search_20250305"
SEARCHES_MAX = 1
REQUESTS_MAX = 6
T_MAX = 20000
MAX_OUT = "1000"
OUT = Path("/out/numbers")
# $/MTok (spikes/web-search/RESULT.md § 2, platform pricing page fetched 2026-11-23)
# Haiku 5.5's higher tier (prompts over 100k tokens) is used for every worst case, so the cap holds in either tier.
RATES = {
    "claude-haiku-5-5": {"in": 0.10, "w5m": 0.125, "w1h": 0.20, "read": 0.01, "out": 0.50},
}
RATES_HIGH = {"claude-haiku-5-5": {"in": 0.50, "w5m": 0.625, "w1h": 1.00, "read": 0.05, "out": 2.50}}
LEDGER = OUT / "ledger.jsonl"
FEE = 0.01
FORWARD_HEADERS = {"accept", "anthropic-beta", "anthropic-dangerous-direct-browser-access", "anthropic-version",
                   "authorization", "content-type", "user-agent", "x-app", "x-claude-code-session-id"}
LOCK = threading.RLock()   # re-entrant: refuse() is also called while the lock is held
STATE: dict = {"spent_usd": 0.0, "searches": 0, "requests": 0, "records": [], "refused": []}

# For comparison only (the gate decides on the verbatim planned query): the vocabulary of the prompt's metadata plus
# fixed query words, integers, ordinals and version tokens. Records counts, never the query.
META = {"title": "Mastering Blockchain", "authors": ["Imran Bashir"], "edition": "4th", "publisher": "Packt",
        "year": 2023}
QUERY_WORDS = {"edition", "new", "newer", "latest", "official", "documentation", "docs", "course", "release",
               "released", "book", "books", "by", "the", "of", "a", "an", "and", "or", "vs", "is", "there", "after",
               "published", "publication", "date", "fifth", "5th", "update", "updated", "second", "third", "fourth"}
STATS: dict = {"gate_allowed": 0, "gate_denied": 0, "denied_oov_tokens": [], "query_chars": []}
# The code-built query (SRV-FRESH FR-FRESH-6 template "edition"): metadata words only.
PLANNED_QUERY = "Mastering Blockchain Imran Bashir new edition after 4th edition 2023"


def tokens(text: str) -> list[str]:
    return [t for t in "".join(c.lower() if c.isalnum() or c in ".-" else " " for c in text).split() if t]


VOCAB = set(tokens(" ".join([META["title"], *META["authors"], META["edition"], META["publisher"],
                             str(META["year"])]))) | QUERY_WORDS


def in_vocab(tok: str) -> bool:
    core = tok.strip(".-")
    return (core in VOCAB or core.isdigit()
            or (core[:-2].isdigit() and core[-2:] in ("st", "nd", "rd", "th"))
            or all(p.isdigit() for p in core.split(".") if p))


def worst_usd(payload: dict, body_len: int) -> float:
    r = RATES_HIGH[payload.get("model", "")]
    w = body_len / 2.0 * r["w1h"] / 1e6 + int(payload.get("max_tokens") or 0) * r["out"] / 1e6
    if is_search(payload):
        w += FEE + T_MAX * r["w1h"] / 1e6
    return w


def is_search(payload: dict) -> bool:
    return any(str(t.get("type", "")).startswith("web_search") for t in payload.get("tools") or [])


def foreign_server_tool(payload: dict) -> bool:
    return bool(payload.get("mcp_servers")) or any(
        "type" in t and t.get("type") != SEARCH_TOOL_TYPE for t in payload.get("tools") or [])


def ledger(line: dict) -> None:
    with LOCK, open(LEDGER, "a") as f:
        f.write(json.dumps(line) + "\n")
        f.flush()
        os.fsync(f.fileno())


def prompt_tokens(usage: dict) -> int:
    return ((usage.get("input_tokens") or 0) + (usage.get("cache_creation_input_tokens") or 0)
            + (usage.get("cache_read_input_tokens") or 0))


def cost_of(model: str, usage: dict) -> float:
    r = (RATES_HIGH if prompt_tokens(usage) > 100_000 else RATES)[model]
    cc = usage.get("cache_creation") or {}
    w5, w1 = cc.get("ephemeral_5m_input_tokens"), cc.get("ephemeral_1h_input_tokens")
    if w5 is None and w1 is None:
        w1 = usage.get("cache_creation_input_tokens") or 0
    return ((usage.get("input_tokens") or 0) * r["in"] + (w5 or 0) * r["w5m"] + (w1 or 0) * r["w1h"]
            + (usage.get("cache_read_input_tokens") or 0) * r["read"] + (usage.get("output_tokens") or 0) * r["out"]
            ) / 1e6 + ((usage.get("server_tool_use") or {}).get("web_search_requests") or 0) * FEE


def parse_sse(raw: bytes) -> dict:
    """Event names, content-block types and key names, usage numbers, search result counts. No text, no URLs."""
    events, block_types, usage, results = [], [], {}, {"results": 0, "with_page_age": 0, "errors": []}
    keys: dict = {}
    for chunk in raw.decode("utf-8", "replace").split("\n\n"):
        data = next((ln[6:] for ln in chunk.split("\n") if ln.startswith("data: ")), None)
        name = next((ln[7:] for ln in chunk.split("\n") if ln.startswith("event: ")), None)
        if data is None:
            continue
        events.append(name)
        try:
            d = json.loads(data)
        except ValueError:
            continue
        if d.get("type") == "message_start":
            usage.update(d["message"].get("usage") or {})
        if d.get("type") == "message_delta":
            usage.update(d.get("usage") or {})
            keys["stop_reason"] = (d.get("delta") or {}).get("stop_reason")
        if d.get("type") == "content_block_start":
            b = d.get("content_block") or {}
            block_types.append(b.get("type"))
            keys.setdefault(b.get("type"), sorted(b.keys()))
            if b.get("type") == "web_search_tool_result":
                c = b.get("content")
                if isinstance(c, list):
                    results["results"] += len(c)
                    results["with_page_age"] += sum(1 for x in c if x.get("page_age"))
                    if c:
                        keys["web_search_result"] = sorted(c[0].keys())
                elif isinstance(c, dict):
                    results["errors"].append(c.get("error_code"))
        if d.get("type") == "content_block_delta":
            delta = d.get("delta") or {}
            if delta.get("type") == "citations_delta":
                keys.setdefault("citation", sorted((delta.get("citation") or {}).keys()))
    return {"events": sorted(set(e for e in events if e)), "block_types": block_types, "usage": usage,
            "search_results": results, "keys": keys}


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request: object, client_address: object) -> None:
        # The default prints a traceback, whose frames could hold request data; print the type only.
        print(f"proxy error: {sys.exc_info()[0].__name__ if sys.exc_info()[0] else 'unknown'}", file=sys.stderr)


class Proxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:
        pass

    def refuse(self, code: str) -> None:
        with LOCK:
            STATE["refused"].append(code)
        data = json.dumps({"type": "error", "error": {"type": "permission_error", "message": code}}).encode()
        self.send_response(403)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self.refuse("path")

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path.split("?")[0] != "/v1/messages":
            return self.refuse("path")
        try:
            payload = json.loads(body)
        except ValueError:
            return self.refuse("body_not_json")
        model = payload.get("model", "")
        if model not in RATES:
            return self.refuse("model")
        if foreign_server_tool(payload):
            return self.refuse("server_tool")
        search = is_search(payload)
        rec = {"kind": "search" if search else "main", "model": model, "max_tokens": payload.get("max_tokens"),
               "top_level_keys": sorted(payload.keys()), "thinking": payload.get("thinking"),
               "tool_choice": payload.get("tool_choice"), "output_config": payload.get("output_config"),
               "tools": [{"name": t.get("name"), "type": t.get("type"), "keys": sorted(t.keys()),
                          "max_uses_sent": t.get("max_uses")} for t in payload.get("tools") or []],
               "cache_control_present": "cache_control" in body.decode("utf-8", "replace")}
        with LOCK:
            if STATE["requests"] >= REQUESTS_MAX:
                return self.refuse("request_cap")
            if search and STATE["searches"] >= SEARCHES_MAX:
                return self.refuse("search_cap")
            worst = worst_usd(payload, len(body))
            if STATE["spent_usd"] + worst > CAP_USD:
                return self.refuse("cap_usd")
            STATE["requests"] += 1
            n = STATE["requests"]
            STATE["spent_usd"] += worst          # held at the worst case until the response settles it
            if search:
                STATE["searches"] += 1
                for t in payload["tools"]:
                    if str(t.get("type", "")).startswith("web_search"):
                        t["max_uses"] = 1
                body = json.dumps(payload).encode()
        ledger({"reserve": n, "usd": round(worst, 6)})
        headers = {k: v for k, v in self.headers.items() if k.lower() in FORWARD_HEADERS
                   or k.lower().startswith("x-stainless-")}
        headers["Content-Length"] = str(len(body))
        t0 = time.monotonic()
        conn = (http.client.HTTPConnection("127.0.0.1", DRY_PORT, timeout=300) if DRY_PORT else
                http.client.HTTPSConnection(UPSTREAM, 443, timeout=300, context=ssl.create_default_context()))
        try:
            conn.request("POST", self.path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
        except Exception as exc:  # sent or not, the outcome is unknown: settle at the worst case
            rec.update({"status": None, "error_type": type(exc).__name__, "usd": round(worst, 6),
                        "usd_basis": "worst_case"})
            with LOCK:
                STATE["records"].append(rec)
            ledger({"settle": n, "usd": round(worst, 6), "basis": "worst_case"})
            conn.close()
            return self.refuse("upstream_failed")
        rec["status"] = resp.status
        rec["seconds"] = round(time.monotonic() - t0, 2)
        ctype = resp.getheader("Content-Type") or ""
        if resp.status == 200 and "event-stream" in ctype:
            rec.update(parse_sse(raw))
        elif resp.status == 200:
            try:
                rec["usage"] = json.loads(raw).get("usage") or {}
            except (ValueError, AttributeError):
                rec["usage"] = {}
        else:
            try:
                rec["error_type"] = (json.loads(raw).get("error") or {}).get("type")
            except (ValueError, AttributeError):
                rec["error_type"] = None
        if resp.status == 200 and rec.get("usage"):
            rec["usd"], rec["usd_basis"] = round(cost_of(model, rec["usage"]), 6), "usage"
        elif resp.status == 200:
            rec["usd"], rec["usd_basis"] = round(worst, 6), "worst_case"   # a 200 without readable usage
        else:
            rec["usd"], rec["usd_basis"] = 0.0, "error_status"
        with LOCK:
            STATE["spent_usd"] += rec["usd"] - worst
            STATE["records"].append(rec)
        ledger({"settle": n, "usd": rec["usd"], "basis": rec["usd_basis"]})
        self.send_response(resp.status)
        for k in ("Content-Type", "request-id", "retry-after"):
            if resp.getheader(k):
                self.send_header(k, resp.getheader(k))
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
        conn.close()


async def run() -> dict:
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, ResultMessage, UserMessage, query

    async def gate(input_data, tool_use_id, context):  # type: ignore[no-untyped-def]
        # SRV-FRESH FR-FRESH-6: allow only the planned query, verbatim (after trimming), and no domain lists.
        # The vocabulary check is recorded alongside, for comparison only.
        q = str((input_data.get("tool_input") or {}).get("query", ""))
        domains = any(k in (input_data.get("tool_input") or {}) for k in ("allowed_domains", "blocked_domains"))
        oov = [t for t in tokens(q) if not in_vocab(t)]
        STATS["query_chars"].append(len(q))
        STATS.setdefault("verbatim", []).append(q.strip() == PLANNED_QUERY)
        STATS.setdefault("domains_passed", []).append(domains)
        ok = q.strip() == PLANNED_QUERY and not domains
        STATS["gate_allowed" if ok else "gate_denied"] += 1
        STATS["denied_oov_tokens"].append(len(oov))
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "permissionDecision": "allow" if ok else "deny",
                                       "permissionDecisionReason": "Use exactly the given query and no domain lists."}}

    system = ("You run one web search for a freshness check. Call WebSearch exactly once with exactly this query and "
              f"no other parameters: {PLANNED_QUERY}\nThen answer in one line: is there an edition newer than the "
              f"one in this metadata {json.dumps(META)}? Give the source URL as plain text, not as a markdown link.")
    opts = ClaudeAgentOptions(
        system_prompt=system, tools=["WebSearch"], allowed_tools=[], setting_sources=[], max_turns=4, model=MODEL,
        max_budget_usd=CAP_USD, hooks={"PreToolUse": [HookMatcher(matcher="WebSearch", hooks=[gate])]},
        env={"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{PROXY_PORT}", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
             "DISABLE_TELEMETRY": "1", "CLAUDE_CODE_MAX_OUTPUT_TOKENS": MAX_OUT, "MAX_THINKING_TOKENS": "0",
             "CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION": str(SEARCHES_MAX)})
    out: dict = {"init_tools": None, "tool_use_result_keys": [], "tool_use_result_items": [], "result": None}
    async for msg in query(prompt="Is there an edition newer than the one in the metadata? One line.",
                           options=opts):
        if type(msg).__name__ == "SystemMessage" and getattr(msg, "subtype", None) == "init":
            out["init_tools"] = (getattr(msg, "data", {}) or {}).get("tools")
            if out["init_tools"] != ["WebSearch"]:
                out["aborted"] = "init_tools_mismatch"
                break                          # stop before reading any assistant output (SRV-AGENT FR-AGENT-7)
        if isinstance(msg, UserMessage) and isinstance(getattr(msg, "tool_use_result", None), dict):
            tur = msg.tool_use_result
            out["tool_use_result_keys"].append(sorted(tur.keys()))
            out["tool_use_result_items"].append(
                [("links", len(i.get("content") or [])) if isinstance(i, dict) else ("text", len(str(i)))
                 for i in tur.get("results") or []])
        if isinstance(msg, ResultMessage):
            out["result"] = {"subtype": msg.subtype, "is_error": msg.is_error,
                             "total_cost_usd": msg.total_cost_usd, "num_turns": msg.num_turns,
                             "usage": msg.usage, "model_usage": msg.model_usage,
                             "terminal_reason": getattr(msg, "terminal_reason", None)}
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    srv = QuietServer(("127.0.0.1", PROXY_PORT), Proxy)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    t0 = time.monotonic()
    try:
        sdk = asyncio.run(run())
        error = None
    except Exception as exc:  # recorded: the spike reports what happened
        sdk, error = {}, type(exc).__name__
    srv.shutdown()
    recs = STATE["records"]
    proxy_sum = round(sum(r.get("usd", 0.0) for r in recs), 6)
    if (sdk.get("aborted")) and error is None:
        error = sdk["aborted"]
    by_model: dict = {}
    for r in recs:
        m = by_model.setdefault(r["model"], {"requests": 0, "input": 0, "output": 0, "web_search_requests": 0})
        u = r.get("usage") or {}
        m["requests"] += 1
        m["input"] += (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) \
            + (u.get("cache_read_input_tokens") or 0)
        m["output"] += u.get("output_tokens") or 0
        m["web_search_requests"] += (u.get("server_tool_use") or {}).get("web_search_requests") or 0
    sdk_cost = (sdk.get("result") or {}).get("total_cost_usd")
    summary = {
        "model": MODEL, "cap_usd": CAP_USD, "wall_s": round(time.monotonic() - t0, 1), "error": error,
        "requests": recs, "refused": STATE["refused"], "sdk": sdk, "gate": STATS,
        "checks": {
            "H-WS-1 nested search request seen": any(r["kind"] == "search" for r in recs),
            "H-WS-2 nested models": sorted({r["model"] for r in recs if r["kind"] == "search"}),
            "H-WS-3 t: nested input tokens per search request": [
                (r.get("usage") or {}).get("input_tokens") for r in recs if r["kind"] == "search"],
            "H-WS-4 sdk cost includes fee (sdk - proxy token-only sum)":
                None if sdk_cost is None else round(sdk_cost - (proxy_sum - FEE * sum(
                    v["web_search_requests"] for v in by_model.values())), 6),
            "H-WS-6 per-model proxy sums": by_model,
            "H-WS-6 sdk vs proxy usd": {"sdk": sdk_cost, "proxy": proxy_sum},
            "H-FRESH-2 denial rate (query not verbatim)": (STATS["gate_denied"] / max(1, STATS["gate_allowed"] + STATS["gate_denied"])),
            "max_uses rewrite accepted (search request status)": [r["status"] for r in recs if r["kind"] == "search"],
        },
    }
    (OUT / "live.json").write_text(json.dumps(summary, indent=1, default=str))
    print(json.dumps({"total_usd_proxy": proxy_sum, "sdk_usd": sdk_cost, "requests": len(recs),
                      "refused": STATE["refused"], "error": error}))


if __name__ == "__main__":
    main()
