"""SRV-AGENT FR-AGENT-14 with SRV-OBS FR-OBS-6: the agent.call span and the CLI's telemetry environment. [db]

@trace FR-AGENT-14 FR-OBS-6
"""

from __future__ import annotations

import dataclasses
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from obs.conftest import finished, obs_setup

from .conftest import CONFIG, FakeCli, FakeProxyClient, Pg, Script, need, world, write_config

pytestmark = pytest.mark.anyio


class SpanCli(FakeCli):
    """Records the traceparent current when the CLI starts (the SDK injects exactly that as TRACEPARENT)."""

    parents: list[str | None]

    async def __call__(self, request: Any) -> Any:  # type: ignore[override]
        self.parents.append(need("reader.obs.propagation").current_traceparent())
        async for message in super().__call__(request):
            yield message


async def _enabled_world(pg: Pg, tmp: Path, enabled: bool) -> Any:
    from reader.config import load_config

    w = await world(pg, tmp)
    (tmp / "cfg").mkdir(exist_ok=True)
    text = CONFIG.replace(
        "observability: { enabled: false }", f"observability: {{ enabled: {str(enabled).lower()} }}"
    )
    return dataclasses.replace(w, config=load_config(write_config(tmp / "cfg", text)))


def _call(w: Any) -> Any:
    provider = need("reader.agent.provider")
    return provider.StageCall(
        owner_id=w.users["ivan"],
        handle="ivan",
        run_id=uuid.uuid4(),
        lesson_id=None,
        stage="fact_check",
        task="fact_check",
        attempt=0,
        system="SYSTEM",
        prompt="PROMPT",
        max_output_tokens=16000,
        floor_usd=Decimal("0.25"),
        run_cap_usd=Decimal("8.00"),
    )


def _provider(w: Any, cli: FakeCli, tmp: Path) -> Any:
    home = tmp / "homes"
    home.mkdir(exist_ok=True)
    return need("reader.agent.provider").AgentProvider(
        w.engine, w.config, FakeProxyClient(cli), cli=cli, home_root=home
    )


async def test_enabled_adds_exactly_the_telemetry_env_and_a_current_span(pg: Pg, tmp_path: Path) -> None:
    cli_env = need("reader.obs.cli_env")
    w = await _enabled_world(pg, tmp_path, True)
    cli = SpanCli([Script(text="answer", cost=0.05)])
    cli.parents = []
    with obs_setup("worker") as (handle, memory):
        await _provider(w, cli, tmp_path).run(_call(w))
        spans = finished(handle, memory)
    env = cli.requests[0].env
    additions = cli_env.cli_env_additions()
    assert {k: env[k] for k in additions} == additions
    assert set(env) == {
        "PATH",
        "HOME",
        "LANG",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "DISABLE_TELEMETRY",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
        *additions,
    }
    for blank in ("OTEL_LOG_RAW_API_BODIES", "ENABLE_BETA_TRACING_DETAILED", "BETA_TRACING_ENDPOINT"):
        assert blank not in env
    (call,) = [s for s in spans if s.name == "agent.call"]
    assert cli.parents == [f"00-{call.context.trace_id:032x}-{call.context.span_id:016x}-01"]
    attrs = dict(call.attributes)
    assert attrs["reader.task"] == "fact_check"
    assert attrs["reader.stage"] == "fact_check"
    assert attrs["gen_ai.request.model"] == "claude-sonnet-5-5"
    assert attrs["reader.cost_usd"] == pytest.approx(0.05)
    assert attrs["reader.stop_reason"] == "end_turn"
    # sdk_usage(): input 2, output 100, cache read 0, cache creation 7000 (all 1h) — exact (round 1 survivors).
    assert attrs["gen_ai.usage.input_tokens"] == 2
    assert attrs["gen_ai.usage.output_tokens"] == 100
    assert attrs["gen_ai.usage.cache_read_input_tokens"] == 0
    assert attrs["gen_ai.usage.cache_creation_input_tokens"] == 7000
    assert call.kind.name == "CLIENT"


async def _settled_usd(w: Any) -> Decimal:
    from sqlalchemy import text

    async with w.engine.connect() as conn:
        (value,) = (await conn.execute(text("SELECT settled_usd FROM agent_spend WHERE kind = 'settle'"))).one()
    return Decimal(value)


async def test_failed_but_billed_call_records_the_settled_amount(pg: Pg, tmp_path: Path) -> None:
    """Two 2xx answers: the ledger settles max(sdk, reserve), not the SDK's 0.05; the span records the ledger's."""
    w = await _enabled_world(pg, tmp_path, True)
    cli = SpanCli([Script(assistant_error="rate_limit", is_error=True, cost=0.05, statuses=(200, 200))])
    cli.parents = []
    with obs_setup("worker") as (handle, memory):
        with pytest.raises(need("reader.agent.errors").AgentError):
            await _provider(w, cli, tmp_path).run(_call(w))
        (call,) = [s for s in finished(handle, memory) if s.name == "agent.call"]
    settled = await _settled_usd(w)
    assert settled != Decimal("0.05")
    attrs = dict(call.attributes)
    assert attrs["reader.cost_usd"] == float(settled)
    assert attrs["reader.error_code"] == "rate_limited"
    assert attrs["gen_ai.usage.output_tokens"] == 100  # span.settled ran on the failure path
    assert call.status.status_code.name == "ERROR"


async def test_a_non_pipeline_stage_is_not_recorded(pg: Pg, tmp_path: Path, caplog: Any) -> None:
    import logging

    w = await _enabled_world(pg, tmp_path, True)
    cli = SpanCli([Script(text="answer", cost=0.05)])
    cli.parents = []
    call = dataclasses.replace(_call(w), stage="tutor")
    with caplog.at_level(logging.WARNING, logger="reader.obs"), obs_setup("worker") as (handle, memory):
        await _provider(w, cli, tmp_path).run(call)
        (span,) = [s for s in finished(handle, memory) if s.name == "agent.call"]
    assert "reader.stage" not in dict(span.attributes)
    assert not any("key=reader.stage" in r.getMessage() for r in caplog.records)  # never set, so never dropped


async def test_disabled_keeps_fr_agent_6_exact(pg: Pg, tmp_path: Path) -> None:
    w = await _enabled_world(pg, tmp_path, False)
    cli = SpanCli([Script(text="answer", cost=0.05)])
    cli.parents = []
    await _provider(w, cli, tmp_path).run(_call(w))
    assert set(cli.requests[0].env) == {
        "PATH",
        "HOME",
        "LANG",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "DISABLE_TELEMETRY",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    }


async def test_failed_call_records_the_error_code(pg: Pg, tmp_path: Path) -> None:
    w = await _enabled_world(pg, tmp_path, True)
    cli = SpanCli([Script(assistant_error="rate_limit", is_error=True, cost=0.0)])
    cli.parents = []
    with obs_setup("worker") as (handle, memory):
        with pytest.raises(need("reader.agent.errors").AgentError):
            await _provider(w, cli, tmp_path).run(_call(w))
        (call,) = [s for s in finished(handle, memory) if s.name == "agent.call"]
    assert call.attributes["reader.error_code"] == "rate_limited"
    assert call.status.status_code.name == "ERROR"
    assert call.status.description is None
