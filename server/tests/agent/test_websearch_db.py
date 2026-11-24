# @trace FR-AGENT-15, FR-AGENT-17
"""The adapter's tool call (ADR-0030): only `("freshness_web", "search")` may carry tools; the CLI gets WebSearch,
the PreToolUse hook, max_turns and the search cap; the init list must be ["WebSearch"]; the run_token carries the
allowance; the settle row takes the SDK cost and the summed model_usage with web_search_requests. [db]"""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from .conftest import FakeCli, FakeProxyClient, Pg, Script, need, rows, world

pytestmark = pytest.mark.anyio
D = Decimal
HAIKU_USAGE = {  # the owner's live call (spikes/web-search § 5b): model_usage of one search run
    "claude-haiku-5-5": {
        "inputTokens": 14456, "outputTokens": 1147, "cacheReadInputTokens": 1281,
        "cacheCreationInputTokens": 2781, "cacheCreationEphemeral5mInputTokens": 0,
        "cacheCreationEphemeral1hInputTokens": 2781, "webSearchRequests": 1, "thinkingTokens": 442,
        "costUSD": 0.01258811,
    }
}
MAIN_ONLY = {  # ResultMessage.usage covers the main loop only
    "input_tokens": 4, "output_tokens": 312, "cache_creation_input_tokens": 2781, "cache_read_input_tokens": 1281,
    "cache_creation": {"ephemeral_1h_input_tokens": 2781, "ephemeral_5m_input_tokens": 0},
    "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
}
RESULT = {"query": "q", "results": [{"tool_use_id": "s", "content": [{"title": "T", "url": "https://e.org/a"}]},
                                    "summary"], "durationSeconds": 1.0, "searchCount": 1}


async def _hook(input_data: Any, tool_use_id: Any, context: Any) -> dict[str, Any]:
    return {}


def _call(w: Any, **kw: Any) -> Any:
    provider = need("reader.agent.provider")
    base: dict[str, Any] = {
        "owner_id": w.users["ivan"], "handle": "ivan", "run_id": uuid.uuid4(), "lesson_id": None,
        "stage": "search", "task": "freshness_web", "attempt": 0, "system": "S", "prompt": "P",
        "max_output_tokens": 1000, "floor_usd": D("0.02"), "run_cap_usd": D("1.00"), "thinking_off": True,
        "abort_on_retry": True,
        "web_search": provider.WebSearchCall(
            allowance=provider.WebSearchAllowance(searches_max=1, searches_per_request_max=1, requests_max=3),
            hook=_hook,
            max_turns=2,
        ),
    }
    base.update(kw)
    return provider.StageCall(**base)


def _provider(w: Any, scripts: list[Script], tmp: Path) -> tuple[Any, FakeCli, FakeProxyClient]:
    provider = need("reader.agent.provider")
    cli = FakeCli(scripts)
    proxy = FakeProxyClient(cli)
    return provider.AgentProvider(w.engine, w.config, proxy, cli=cli, home_root=tmp), cli, proxy


async def test_tools_only_for_the_freshness_search_stage(
    pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    errors = need("reader.agent.errors")
    w = await world(pg, tmp_path)
    p, cli, proxy = _provider(w, [Script()], tmp_path)
    provider = need("reader.agent.provider")
    spans: list[tuple[Any, ...]] = []

    def no_span(*args: Any) -> Any:
        spans.append(args)
        raise AssertionError("agent.call opened for a refused tool call")

    monkeypatch.setattr(provider, "agent_call", no_span)  # FR-AGENT-15: refused before the span opens
    ungated = provider.WebSearchCall(
        allowance=provider.WebSearchAllowance(searches_max=1, searches_per_request_max=1, requests_max=3),
        hook=None,
        max_turns=2,
    )
    bad_calls: list[dict[str, Any]] = [
        {"task": "draft"}, {"stage": "report"}, {"task": "fact_check", "stage": "tutor"}, {"web_search": ungated},
    ]
    for bad in bad_calls:
        with pytest.raises(errors.AgentError) as refused:
            await p.run(_call(w, **bad))
        assert refused.value.code == "tools_mismatch"
    assert proxy.issued == [] and cli.requests == [] and spans == []
    assert await rows(w.engine, "SELECT id FROM agent_spend") == []  # refused before the reservation


async def test_tool_call_reserve_adds_the_search_fee(pg: Pg, tmp_path: Path) -> None:
    """FR-AGENT-17: reserve = max(floor, single-call estimate + searches_max x web_search_usd)."""
    ledger = need("reader.agent.ledger")
    w = await world(pg, tmp_path)
    script = Script(
        text="one line", cost=0.01258811, usage=MAIN_ONLY, init_tools=["WebSearch"], statuses=(200, 200, 200),
        model_usage=HAIKU_USAGE, searches_forwarded=1, tool_use_results=[RESULT],
    )
    p, _, _ = _provider(w, [script], tmp_path)
    await p.run(_call(w, floor_usd=D(0)))
    [reserve] = await rows(w.engine, "SELECT usd FROM agent_spend WHERE kind = 'reserve'")
    estimate = ledger.worst_case("claude-haiku-5-5", 2, 1000, custom_system=True)
    assert reserve == (estimate + D("0.01"),)
    assert reserve[0] == D("0.010700")  # (1,000 + 1) * 0.20 + 1,000 * 0.50 per 1e6, plus one $0.01 search


async def test_tool_call_options_settlement_and_results(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    script = Script(
        text="one line", cost=0.01258811, usage=MAIN_ONLY, init_tools=["WebSearch"], statuses=(200, 200, 200),
        model_usage=HAIKU_USAGE, searches_forwarded=1, tool_use_results=[RESULT],
    )
    p, cli, proxy = _provider(w, [script], tmp_path)
    result = await p.run(_call(w))
    assert result.model == "claude-haiku-5-5"
    assert result.tool_results == (RESULT,)
    assert result.usage.web_search_requests == 1 and result.usage.input == 14456
    assert proxy.issue_extras == [
        {"task": "freshness_web",
         "web_search": {"searches_max": 1, "searches_per_request_max": 1, "requests_max": 3}}
    ]
    request = cli.requests[0]
    assert request.tools == ("WebSearch",) and request.max_turns == 2 and request.pre_tool_use is _hook
    assert request.env["CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION"] == "1"
    options = need("reader.agent.provider").sdk_options(request)
    assert options.tools == ["WebSearch"] and options.allowed_tools == [] and options.max_turns == 2
    assert list(options.hooks) == ["PreToolUse"]
    [settle] = await rows(
        w.engine,
        "SELECT settled_usd, sdk_usd, usd, input, output, cache_write_1h, cache_read, thinking, web_search_requests"
        " FROM agent_spend WHERE kind = 'settle'",
    )
    assert settle == (D("0.012588"), D("0.012588"), D("0.012588"), 14456, 1147, 2781, 1281, 442, 1)


async def test_tool_call_init_must_list_websearch(pg: Pg, tmp_path: Path) -> None:
    errors = need("reader.agent.errors")
    w = await world(pg, tmp_path)
    p, _, _ = _provider(
        w,
        [Script(init_tools=[], model_usage=HAIKU_USAGE), Script(init_tools=["WebSearch", "Bash"],
                                                                 model_usage=HAIKU_USAGE)],
        tmp_path,
    )
    for _ in range(2):
        with pytest.raises(errors.AgentError) as refused:
            await p.run(_call(w))
        assert refused.value.code == "tools_mismatch"


async def test_tool_less_calls_are_unchanged(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    p, cli, proxy = _provider(w, [Script()], tmp_path)
    await p.run(_call(w, task="fact_check", stage="tutor", web_search=None, abort_on_retry=False,
                      thinking_off=False))
    assert proxy.issue_extras == [{}]
    options = need("reader.agent.provider").sdk_options(cli.requests[0])
    assert options.tools == [] and options.max_turns == 1 and not options.hooks
    assert "CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION" not in cli.requests[0].env
