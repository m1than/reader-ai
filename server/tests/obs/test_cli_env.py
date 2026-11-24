"""SRV-OBS [unit]: the CLI's telemetry environment and the pin that ties it to the measured canary.

@trace FR-OBS-6 VEC-OBS-2
"""

from __future__ import annotations

import re

from .conftest import FIXTURES, REPO, load_yaml, require

VEC = load_yaml(FIXTURES / "cli-span-keys.yaml")


def test_cli_env_additions_are_exact() -> None:
    env = require("reader.obs.cli_env")
    assert env.cli_env_additions() == {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_METRICS_EXPORTER": "none",
        "OTEL_LOGS_EXPORTER": "none",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://rotel:4318",
        "OTEL_LOG_USER_PROMPTS": "0",
        "OTEL_LOG_ASSISTANT_RESPONSES": "0",
        "OTEL_LOG_TOOL_CONTENT": "0",
        "OTEL_LOG_TOOL_DETAILS": "0",
    }
    for never in (
        "OTEL_LOG_RAW_API_BODIES",
        "ENABLE_BETA_TRACING_DETAILED",
        "BETA_TRACING_ENDPOINT",
        "OTEL_EXPORTER_OTLP_HEADERS",
    ):
        assert never not in env.cli_env_additions()


def test_locked_sdk_equals_the_measured_canary_version() -> None:
    """A claude-agent-sdk pin change fails here until the canary is rerun and VEC-OBS-2 regenerated."""
    lock = (REPO / "server" / "uv.lock").read_text(encoding="utf-8")
    match = re.search(r'\[\[package\]\]\nname = "claude-agent-sdk"\nversion = "([^"]+)"', lock)
    assert match is not None
    assert VEC["cli"]["package"] == "claude-agent-sdk"
    assert match.group(1) == VEC["cli"]["sdk"]


def test_vec_obs_2_shape() -> None:
    assert set(VEC["spans"]) == {
        "claude_code.interaction",
        "claude_code.llm_request",
        # the WebSearch run's tool spans (SRV-FRESH's search call): keys only, no input or output key
        "claude_code.tool",
        "claude_code.tool.blocked_on_user",
        "claude_code.tool.execution",
    }
    for name in ("claude_code.tool", "claude_code.tool.blocked_on_user", "claude_code.tool.execution"):
        assert not {"input", "output", "tool_input", "tool.output"} & set(VEC["spans"][name]["keys"]), name
    assert VEC["spans"]["claude_code.interaction"]["redacted"] == {"user_prompt": "<REDACTED>"}
    assert "authorization" not in VEC["otlp_request_headers"]
    for span in VEC["spans"].values():
        assert not any("cost" in key for key in span["keys"])
