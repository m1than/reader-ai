# Spike: cli-otel-content — RESULT

**Question (SRV-OBS FR-OBS-6, INV-OBS-2):** under SRV-AGENT FR-AGENT-6's exact CLI environment, does the bundled CLI
still export its trace spans? If so, which environment flags keep the prompt, the system prompt, the response and the
credential out of what it exports? What do its spans carry?

**Verdict: PASS, zero credit.**
- The CLI exports its spans under the full SRV-AGENT environment, `DISABLE_TELEMETRY=1` and
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` included.
- `claude_code.interaction` is a child of our span.
- With the content flags unset, or set to `0`, no canary (prompt, system prompt, response, run_token stand-in)
  appears in any OTLP request, decoded or raw.
- The positive control (`OTEL_LOG_USER_PROMPTS=1`, `OTEL_LOG_ASSISTANT_RESPONSES=1`) finds the prompt canary, so the
  scan works.
- No OTLP request carries an `Authorization` header.

Date: 2026-11-24.
- **Command:** `bash spikes/cli-otel-content/run.sh`.
- **Results:** `results/20261124T002020/` (`report.json`, `requests.jsonl`, the raw OTLP bodies in `otlp/`).

## Setup

| Item | Value |
| --- | --- |
| SDK and CLI | `claude-agent-sdk` 0.2.187 (the server's pin) with its bundled CLI 2.1.330; `opentelemetry-sdk` and `-exporter-otlp-proto-http` 1.45.2; `python:3.14-slim-trixie`, arm64 |
| Container | `--network none`, read-only root, tmpfs home and /tmp, cap-drop ALL, no-new-privileges, uid 1000 |
| Upstream | `stub.py` on 127.0.0.1:8080. It answers `/v1/messages` with a minimal SSE stream whose text holds the response canary |
| OTLP receiver | `stub.py` on 127.0.0.1:4318. It stores every request body and logs its path and header names |
| Other hosts | `HTTP(S)_PROXY` points at a logging, refusing forward proxy on 127.0.0.1:8081 |
| CLI environment | FR-AGENT-6 exactly: `PATH, HOME, LANG, CLAUDE_CODE_OAUTH_TOKEN=<random 43-char handle>, ANTHROPIC_BASE_URL, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1, DISABLE_TELEMETRY=1, CLAUDE_CODE_MAX_OUTPUT_TOKENS`, with every other inherited variable passed blank. Added: `CLAUDE_CODE_ENABLE_TELEMETRY=1, CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1, OTEL_TRACES_EXPORTER=otlp, OTEL_METRICS_EXPORTER=none, OTEL_LOGS_EXPORTER=none, OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf, OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318` |
| Options | `tools=[]`, `allowed_tools=[]`, `setting_sources=[]`, `max_turns=1`, model `claude-sonnet-5-5`, `system_prompt={"type": "file", ...}`: the same as FR-AGENT-6 |
| Our spans | `reader.job spike` → `reader.stage draft`, opened by the probe with the OTel SDK; `query()` runs inside the stage span |
| Canaries | Random per run: prompt `bookcanary<hex>`, system prompt `systemcanary<hex>`, response `answercanary<hex>`, and the handle |

## Results

| Mode | CLI spans | Under our stage span | Prompt | System | Response | Handle | Other hosts |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `agent-env` (content flags unset) | 2 | `claude_code.interaction` | 0 | 0 | 0 | 0 | none |
| `agent-env-explicit-off` (the five flags `=0`) | 2 | `claude_code.interaction` | 0 | 0 | 0 | 0 | none |
| `positive-control` (`OTEL_LOG_USER_PROMPTS=1`, `OTEL_LOG_ASSISTANT_RESPONSES=1`) | 2 | `claude_code.interaction` | **1** | 0 | 0 | 0 | none |

The counts are canary hits, decoded and raw alike.

- **Spans:** `claude_code.interaction`, then `claude_code.llm_request` (its child), with scope
  `com.anthropic.claude_code.tracing`. The resource keys are `host.arch, os.type, os.version, service.name,
  service.version`. The full key list is in `spec/fixtures/SRV-OBS/cli-span-keys.yaml` (VEC-OBS-2).
- **Content:**
  - `user_prompt` is the literal `<REDACTED>`, and `user_prompt_length` is the prompt length in characters.
  - Under the positive control, `user_prompt` holds the prompt text.
  - The response canary never appeared, even with `OTEL_LOG_ASSISTANT_RESPONSES=1`: with logs export off, the CLI
    put no response text on a span.
- **No cost attribute.** `llm_request` carries `input_tokens`, `output_tokens`, `cache_read_tokens`,
  `cache_creation_tokens`, model, timings and `stop_reason`. Cost must come from our own span (SRV-OBS FR-OBS-6).
- **`user.id`:** a 64-hex value, the same for the two spans of one run. The handle canary was absent, so it is not
  the credential. HOME is a fresh directory per run, as in FR-AGENT-6.
- **OTLP requests:** three per mode, `POST /v1/traces`, protobuf. Header names: `accept, accept-encoding,
  connection, content-length, content-type, host, user-agent`. No `authorization`.
- **Upstream:** one `POST /v1/messages` per mode, and no other host.

## Flags in the bundled CLI 2.1.330

Read from its strings:
- **Content flags:** `OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_ASSISTANT_RESPONSES` (which falls back to
  `OTEL_LOG_USER_PROMPTS`), `OTEL_LOG_TOOL_CONTENT`, `OTEL_LOG_TOOL_DETAILS` and `OTEL_LOG_RAW_API_BODIES`
  (`inline`, a `file` directory, or disabled). A prompt is emitted as `<REDACTED>` unless `OTEL_LOG_USER_PROMPTS`
  is truthy.
- **Detailed beta tracing:** a separate path, `ENABLE_BETA_TRACING_DETAILED` together with `BETA_TRACING_ENDPOINT`,
  with its own exporter. Both stay unset (blank).
- **Telemetry switches:** `DISABLE_TELEMETRY` and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` select the CLI's own
  product-telemetry mode. They did not stop OTel trace export in this run.

## Limits

- One query, no tools, a stub upstream: tool spans (`claude_code.tool*`) were not produced. The compile and tutor
  calls also run with `tools=[]` (FR-AGENT-7).
- Logs and metrics export were off in every mode. SRV-OBS keeps them off.
- One CLI version. A `claude-agent-sdk` pin change must rerun this harness (SRV-OBS FR-OBS-6, INV-OBS-2 [image]).

## Spend

$0. No real token; the network was `none`.
