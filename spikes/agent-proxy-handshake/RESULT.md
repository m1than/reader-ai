# Spike: agent-proxy-handshake — RESULT

**Question (SRV-AGENT review B1; ADR-0025 hypothesis 1):** Does the Agent SDK's bundled CLI authenticate with a
handle-shaped placeholder token through `ANTHROPIC_BASE_URL`? Which methods, paths and headers does it send, which other
hosts does it contact, and what is the init tool list under `tools=[]`?

**Verdict: PASS, zero credit.** With `CLAUDE_CODE_OAUTH_TOKEN=<handle>` and `ANTHROPIC_BASE_URL=http://127.0.0.1:8080`,
the CLI sends the handle as `Authorization: Bearer <handle>` to the base URL, on `POST /v1/messages?beta=true` only.
With `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` it makes exactly one request per query and contacts no other host;
without it, it adds one auxiliary `/v1/messages` call per query and tries `CONNECT api.anthropic.com:443` twice.
`tools=[]` gives an empty init tool list and sends no tools.

Date: 2026-11-22. Command: `bash spikes/agent-proxy-handshake/run.sh` (and `NONESSENTIAL_OFF=1 bash …`).

## Setup

| Item | Value |
| --- | --- |
| SDK | `claude-agent-sdk` 0.2.187 (bundled CLI; `claude` is not on PATH, the SDK starts its own binary) on `python:3.14-slim-trixie` (3.14.10), arm64 |
| Container | `--network none`, read-only root, tmpfs home and /tmp, cap-drop ALL, no-new-privileges, uid 1000 |
| Upstream | `stub.py` on 127.0.0.1:8080: logs method, path, query keys, header names, auth scheme and whether the credential equals the handle; answers `/v1/messages` with a minimal valid SSE message stream, anything else 404 |
| Other hosts | `HTTP_PROXY`/`HTTPS_PROXY` = a logging forward proxy on 127.0.0.1:8081 that records every target and refuses it; with `--network none` no other route exists |
| Credential | a fresh `secrets.token_urlsafe(32)` per run (43 chars), never a real token |
| Options | `tools=[]` (mode `tools-empty`) or unset (mode `tools-default`); `allowed_tools=[]`, `setting_sources=[]`, `max_turns=1`, model `claude-sonnet-5-5` |

## Observed (`results/20261122T211421/`, `results/20261122T211436-nonessential-off/`)

| | Default | `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` |
| --- | --- | --- |
| Requests to the base URL per query | 2 × `POST /v1/messages?beta=true` (an auxiliary 4,054-byte structured-output request, then the main one) | 1 × `POST /v1/messages?beta=true` |
| Other hosts | `CONNECT api.anthropic.com:443` twice per query (refused; the query still succeeded) | none |
| Credential | `Authorization: Bearer <handle>` in every request; no `x-api-key` | same |
| `tools=[]` | init `SystemMessage` `tools: []`; the request carries no tool (the stub logged `tools` absent or empty alike, so which of the two is not distinguished; the proxy refuses only a present, non-empty `tools`) | same |
| Tools when unset | 24 in init, 12 in the request (the rest deferred) | 23 / 12 |
| Result | `ResultMessage` `success`, `total_cost_usd` 0.0001 (computed from the stub's fake usage) | 0.00005 |

Header names sent (union): `accept, accept-encoding, anthropic-beta, anthropic-dangerous-direct-browser-access,
anthropic-version, authorization, connection, content-length, content-type, host, user-agent, x-app,
x-claude-code-session-id, x-stainless-arch, x-stainless-lang, x-stainless-os, x-stainless-package-version,
x-stainless-retry-count, x-stainless-runtime, x-stainless-runtime-version, x-stainless-timeout`.
`anthropic-beta` includes `oauth-2025-04-20` and `claude-code-20250219`.

`ResultMessage.usage` keys: `cache_creation` (TTL breakdown), `cache_creation_input_tokens`, `cache_read_input_tokens`,
`fallback_credit`, `inference_geo`, `input_tokens`, `iterations`, `output_tokens`, `output_tokens_details`,
`server_tool_use`, `service_tier`, `speed`.

Message order (timestamped in round 2, `results/20261122T213025-maxout/`): `SystemMessage(init)` at 0.228 s,
`AssistantMessage` 0.250 s, `RateLimitEvent` 0.253 s, `ResultMessage` 0.262 s. The init message always precedes the
assistant message, but the request has already been sent by then (one turn). (Round 1 listed `RateLimitEvent` first;
the timestamps show it follows the assistant message.)

## Round 2 (SRV-AGENT review R2-B2, R2-B4): output cap and upstream errors

All with `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, mode `tools-empty`, zero credit.

**Query string:** logged in full: exactly `beta=true`.

**Output cap** (`MAX_OUT=32000 TAG=maxout`, `results/20261122T213025-maxout/`): with `CLAUDE_CODE_MAX_OUTPUT_TOKENS=32000`
the request body has `max_tokens: 32000`. The proxy can therefore bind the cap to the run_token and refuse a larger
`max_tokens`.

**Upstream errors** (`STUB_ERROR_STATUS=429|529|500`, the stub answers every `/v1/messages` with that status and an
Anthropic-shaped error body; 429 adds `retry-after: 1`):

| Status | Upstream requests | Request times (s from first) | `api_retry` SystemMessages | `AssistantMessage.error` | `ResultMessage` | Raised | Wall |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 429 (`results/…213028-429/`) | 11 | 0, 1.2, 2.3, 4.8, 9.2, 18.1, 34.7, 68.1, 103.4, 142.8, 176.1 | 10 | `rate_limit` | `subtype` success, `is_error` true, cost 0 | `ResultError` ("Server is temporarily limiting requests") | 176.4 s |
| 529 (`results/…213327-529/`) | 11 | 0, 0.5, 1.5, 3.9, 7.9, 16.1, 34.2, 73.4, 107.3, 140.2, 178.3 | 10 | `server_error` | same | `ResultError` ("API Error: 529 …") | 178.8 s |
| 500 (`results/…213629-500/`) | 11 | 0, 0.5, 1.7, 3.8, 8.3, 17.6, 36.8, 71.1, 108.3, 141.2, 173.6 | 10 | `server_error` | same | `ResultError` ("API Error: 500 …") | 174.1 s |

Each `api_retry` SystemMessage carries `attempt`, `error`, `error_status`, `max_retries`, `retry_delay_ms`. The CLI's
own retry budget is 10 retries with exponential backoff capped near 35–40 s, about 3 minutes in all; the 429
`retry-after: 1` did not shorten it.

## Hands to SRV-AGENT (round 2)

- Classify by `AssistantMessage.error`: `rate_limit` → `rate_limited`; `server_error` → `provider_unavailable` (529 and
  500 are not distinguished by the SDK). The result's cost is 0 and no upstream 2xx happened, so the reservation settles
  at $0; the proxy's per-run_token status record confirms "no 2xx" independently of the SDK.
- One stage attempt can spend ~3 minutes in the CLI's retries before failing; job timeouts must cover that.
- Bind `max_tokens` to the run_token: the CLI sends `CLAUDE_CODE_MAX_OUTPUT_TOKENS` verbatim as `max_tokens`.

## Hands to SRV-AGENT

- The proxy allow-list is exactly `POST /v1/messages` with query `beta=true`; everything else is refused.
- Forward a fixed allow-list of request headers (the list above minus `authorization`, `host`, `connection`,
  `content-length` handled by the proxy); replace `authorization` with the user's token.
- Set `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`: one model request per stage, and no direct egress attempts
  (the worker has none anyway; without the variable they fail harmlessly).
- The init tool list check cannot precede the first request within one `query()` call. Fail closed by checking the init
  list before reading any assistant output, and by discarding the result if it is not empty; `tools=[]` sends an empty
  tool list in the request itself, which the proxy can also check (refuse a request body with a non-empty `tools`).
- Not tested here: the real upstream's response to a forwarded subscription token (FR-AGENT-11, live).
