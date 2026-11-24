# Spike: web-search-otel — RESULT

**Question (SRV-FRESH FR-FRESH-13 with SRV-OBS FR-OBS-6):** with observability on, SRV-AGENT gives every CLI SRV-OBS's
trace export (`cli_env_additions()`: traces only, the four content flags `0`). Does the freshness search run's
WebSearch tool span, or any other exported span or event, then carry the search query, the result titles or URLs or
the nested model's summary, with `OTEL_LOG_TOOL_DETAILS=0`?

**Verdict: no, zero credit.** With the content flags `0` (production) or unset (CLI defaults), and even with
`OTEL_LOG_TOOL_DETAILS=1`, no query, title, URL, summary, system prompt or credential canary appears in any OTLP
request, decoded or raw. Only `OTEL_LOG_TOOL_CONTENT=1` (the positive control) exports them: one `tool.output`
event on `claude_code.tool.execution` whose `output` attribute is the tool result text ("Web search results for
query: ..., Links: [...]"). So the search run keeps the trace export; the choice is pinned by a test of its
environment (`server/tests/agent/test_obs_env.py::test_a_search_run_gets_the_telemetry_env_with_content_off`).

Date: 2026-11-24.
- **Command:** `bash spikes/web-search-otel/run.sh` (modes `content-off content-unset tool-details tool-content`).
- **Results:** `results/20261124T122933/` (`report.json`, `requests.jsonl`, the raw OTLP bodies in `otlp/`).

## Setup

As spikes/cli-otel-content (same image base, SDK 0.2.187 with its bundled CLI 2.1.330, `--network none`, read-only
root, tmpfs home, cap-drop ALL, uid 1000; stub upstream, refusing forward proxy, capturing OTLP receiver), with the
search run's options as SRV-FRESH builds them: `tools=["WebSearch"]`, `allowed_tools=[]`, a `PreToolUse` hook
matching `WebSearch` that allows exactly `{query: <q>}` once, `max_turns=2`, model `claude-haiku-5-5`, a file system
prompt, the prompt `Query: <q>`, FR-AGENT-6's environment plus `MAX_THINKING_TOKENS=0` and
`CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION=1`. The stub answers the measured WebSearch flow (spikes/web-search § 3a):
main loop `tool_use` WebSearch with the query canary, the nested request with `server_tool_use` and a
`web_search_tool_result` whose title and URL hold canaries plus a summary canary, then a final text.

## Results

| Mode | Upstream | Gate | Query | Title | URL | Summary | System | Handle |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `content-off` (four flags `0`) | main, search, final | allowed 1 | 0 | 0 | 0 | 0 | 0 | 0 |
| `content-unset` | main, search, final | allowed 1 | 0 | 0 | 0 | 0 | 0 | 0 |
| `tool-details` (`OTEL_LOG_TOOL_DETAILS=1`) | main, search, final | allowed 1 | 0 | 0 | 0 | 0 | 0 | 0 |
| `tool-content` (`OTEL_LOG_TOOL_CONTENT=1`, control) | main, search, final | allowed 1 | **1** | **1** | **1** | **1** | 0 | 0 |

- **Spans per run:** `claude_code.interaction`, three `claude_code.llm_request`, `claude_code.tool`,
  `claude_code.tool.blocked_on_user` (the hook decision: `decision`, `source`) and `claude_code.tool.execution`.
  `claude_code.tool` carries `tool_name`, `tool_name_safe`, `gen_ai.tool.name`, `tool_use_id` and timings, no input.
- `user_prompt` on `claude_code.interaction` is `<REDACTED>` (the prompt line holds the query): the query canary was
  absent in every mode but the control.
- Only `/v1/traces` was posted (logs and metrics export `none`); no request carried `Authorization`; no other host.

## Limits

- One CLI version; a `claude-agent-sdk` pin change must rerun this spike with SRV-OBS's CLI canary harness.
- This spike measures what the CLI emits; it does not exercise Rotel or MLflow. The content flags are what keeps
  text out of the CLI's spans (SRV-OBS FR-OBS-6 governs what happens to them downstream).

## Spend

$0. No real token; the network was `none`.
