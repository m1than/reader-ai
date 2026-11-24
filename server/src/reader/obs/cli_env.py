"""The Agent SDK CLI's telemetry environment (SRV-OBS FR-OBS-6, SRV-AGENT FR-AGENT-14).

Traces only, OTLP/HTTP to Rotel; the four content flags set to 0 so no inherited value can turn them on (the
CLI's defaults are already off, measured in spikes/cli-otel-content on CLI 2.1.330). OTEL_LOG_RAW_API_BODIES,
ENABLE_BETA_TRACING_DETAILED and BETA_TRACING_ENDPOINT are never set here; SRV-AGENT passes them blank.
"""

from __future__ import annotations

from typing import Final

_ADDITIONS: Final = (
    ("CLAUDE_CODE_ENABLE_TELEMETRY", "1"),
    ("CLAUDE_CODE_ENHANCED_TELEMETRY_BETA", "1"),
    ("OTEL_TRACES_EXPORTER", "otlp"),
    ("OTEL_METRICS_EXPORTER", "none"),
    ("OTEL_LOGS_EXPORTER", "none"),
    ("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf"),
    ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://rotel:4318"),
    ("OTEL_LOG_USER_PROMPTS", "0"),
    ("OTEL_LOG_ASSISTANT_RESPONSES", "0"),
    ("OTEL_LOG_TOOL_CONTENT", "0"),
    ("OTEL_LOG_TOOL_DETAILS", "0"),
)


def cli_env_additions() -> dict[str, str]:
    return dict(_ADDITIONS)
