# @trace FR-AGENT-15, FR-AGENT-16, FR-AGENT-17, INV-AGENT-6, VEC-AGENT-3, VEC-AGENT-4
"""agent-proxy's task-scoped WebSearch allowance (ADR-0030): the two pinned shapes, the `max_uses` rewrite, the
search allowance and request cap, run-token issue rules, tool-call settlement, the Haiku tiers and the search fee.
In process against the recording upstream stub (no network)."""

from __future__ import annotations

import copy
import json
import logging
import random
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from .conftest import LANE_ADDR, FIXTURES, Upstream, forward, load_yaml, make_proxy, message_body, need

pytestmark = pytest.mark.anyio
D = Decimal
SHAPES = load_yaml(FIXTURES / "websearch-shapes.yaml")
SEQ = load_yaml(FIXTURES / "websearch-sequences.yaml")
ALLOW = {"searches_max": 1, "searches_per_request_max": 1, "requests_max": 3}


def _plain(value: Any) -> Any:
    return json.loads(json.dumps(value))


MAIN = _plain(SHAPES["main_tools"])
NESTED = _plain(SHAPES["nested_tools"])


def tools_for(case: dict[str, Any]) -> list[Any]:
    name = case["tools"]
    main, nested = copy.deepcopy(MAIN), copy.deepcopy(NESTED)
    if name == "main":
        return main
    if name == "main_websearch_only":
        return [main[1]]
    if name == "main_with_description_suffix":
        main[1]["description"] += case.get("suffix", "x" * case.get("suffix_repeat", 0))
        return main
    if name == "main_websearch_extra_key":
        main[1][case["key"]] = case["value"]
        return main
    if name == "main_websearch_schema_extra_property":
        main[1]["input_schema"]["properties"]["extra"] = {"type": "string"}
        return main
    if name == "main_plus_tool":
        return [*main, _plain(case["tool"])]
    if name == "main_two_placeholders":
        return [main[0], copy.deepcopy(main[0]), main[1]]
    if name == "main_placeholder_schema_extra_property":
        main[0]["input_schema"]["properties"]["extra"] = {"type": "string"}
        return main
    if name == "main_placeholder_key":
        main[0][case["key"]] = "x" * case["value_repeat"] if "value_repeat" in case else case["value"]
        return main
    if name == "main_placeholder_without":
        main[0].pop(case["key"])
        return main
    if name == "nested":
        return nested
    if name == "nested_without_max_uses":
        nested[0].pop("max_uses")
        return nested
    if name == "nested_max_uses":
        nested[0]["max_uses"] = case["value"]
        return nested
    if name == "nested_type":
        nested[0]["type"] = case["value"]
        return nested
    if name == "nested_extra_key":
        nested[0][case["key"]] = _plain(case["value"])
        return nested
    if name == "nested_without_key":
        nested[0].pop(case["key"])
        return nested
    if name == "nested_twice":
        return [nested[0], copy.deepcopy(nested[0])]
    if name == "custom":
        return _plain(case["value"])
    raise AssertionError(name)


async def issue_with(c: httpx.AsyncClient, **extra: Any) -> httpx.Response:
    body = {"user": "ivan", "max_output_tokens": 16000, "model": "claude-sonnet-5-5", **extra}
    return await c.post("/run-tokens", json=body)


async def web_token(c: httpx.AsyncClient, allowance: dict[str, int] | None = ALLOW) -> str:
    extra: dict[str, Any] = {"task": "freshness_web"}
    if allowance is not None:
        extra["web_search"] = allowance
    r = await issue_with(c, **extra)
    assert r.status_code == 200, r.text
    token: str = r.json()["run_token"]
    return token


def body_with(tools: list[Any], **extra: Any) -> bytes:
    return message_body(tools=tools, **extra)


def test_pins_match_the_recorded_definitions() -> None:
    """VEC-AGENT-3: the fixture's digests are those of the recorded definitions, and the proxy pins the same."""
    proxy = need("reader.agent.proxy")
    pins = SHAPES["pins"]
    assert proxy.canonical_sha256(MAIN[1]["input_schema"]) == pins["websearch_input_schema_sha256"]
    assert proxy.canonical_sha256(MAIN[0]["input_schema"]) == pins["placeholder_input_schema_sha256"]
    assert pins["web_search_version"] == NESTED[0]["type"] == proxy.WEB_SEARCH_VERSION
    assert pins["websearch_input_schema_sha256"] == proxy.WEBSEARCH_INPUT_SCHEMA_SHA256
    assert pins["placeholder_input_schema_sha256"] == proxy.PLACEHOLDER_INPUT_SCHEMA_SHA256
    assert pins["websearch_description_chars_max"] == proxy.WEBSEARCH_DESCRIPTION_MAX
    assert pins["placeholder_description_chars_max"] == proxy.PLACEHOLDER_DESCRIPTION_MAX


@pytest.mark.parametrize("case", SHAPES["cases"], ids=lambda c: c["id"])
async def test_shape_cases(case: dict[str, Any], tmp_path: Path) -> None:
    """FR-AGENT-15 (VEC-AGENT-3): forwarded only in a pinned shape; a nested request's max_uses is rewritten."""
    p = make_proxy(tmp_path)
    extra: dict[str, Any] = {}
    if "tool_choice" in case:
        extra["tool_choice"] = _plain(case["tool_choice"])
    if "mcp_servers" in case:
        extra["mcp_servers"] = _plain(case["mcp_servers"])
    tools = tools_for(case)
    async with p.client() as c:
        token = await web_token(c)
        r = await forward(c, token, body=body_with(tools, **extra))
    if case["expect"] == "tools_mismatch":
        assert (r.status_code, r.json()) == (403, {"error": "tools_mismatch"})
        assert p.upstream.requests == []
        return
    assert r.status_code == 200, r.text
    sent = json.loads(p.upstream.bodies[0])
    if case["expect"] == "main":
        assert sent["tools"] == tools  # forwarded unchanged
    else:
        assert sent["tools"] == [{**{k: v for k, v in tools[0].items()}, "max_uses": case["max_uses_forwarded"]}]


@pytest.mark.parametrize("tools", [MAIN, NESTED], ids=["main", "nested"])
async def test_tools_are_refused_without_an_allowance(tools: list[Any], tmp_path: Path) -> None:
    """FR-AGENT-15: every other task, and a freshness_web token without the allowance, stays tool-less."""
    p = make_proxy(tmp_path)
    async with p.client() as c:
        plain = (await issue_with(c)).json()["run_token"]
        other = (await issue_with(c, task="draft")).json()["run_token"]
        bare = await web_token(c, None)
        for token in (plain, other, bare):
            r = await forward(c, token, body=body_with(copy.deepcopy(tools)))
            assert (r.status_code, r.json()) == (403, {"error": "tools_mismatch"})
        ok = await forward(c, bare, body=message_body())  # tool-less body on the same token is fine
        assert ok.status_code == 200
    assert len(p.upstream.requests) == 1


async def test_nested_request_on_another_model_is_refused(tmp_path: Path) -> None:
    """FR-AGENT-15: a nested request moved to another model by a CLI flag fails closed."""
    p = make_proxy(tmp_path)
    async with p.client() as c:
        token = await web_token(c)
        r = await forward(c, token, body=body_with(copy.deepcopy(NESTED), model="claude-haiku-5-5"))
    assert (r.status_code, r.json()) == (403, {"error": "model_mismatch"})
    assert p.upstream.requests == []


@pytest.mark.parametrize("case", SEQ["issue"], ids=lambda c: c["id"])
async def test_issue_cases(case: dict[str, Any], tmp_path: Path) -> None:
    """FR-AGENT-15 (VEC-AGENT-4 `issue`)."""
    p = make_proxy(tmp_path)
    async with p.client() as c:
        r = await issue_with(c, **_plain(case["body"]))
    assert r.status_code == case["status"]
    if "error" in case:
        assert r.json() == {"error": case["error"]}


class StatusUpstream(Upstream):
    """Answers each request with the next status of `script` (200 when it runs out)."""

    def __init__(self, script: list[int]) -> None:
        super().__init__()
        self.script = list(script)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.status = self.script.pop(0) if self.script else 200
        return super().handler(request)


@pytest.mark.parametrize("seq", SEQ["sequences"], ids=lambda s: s["id"])
async def test_allowance_sequences(seq: dict[str, Any], tmp_path: Path) -> None:
    """FR-AGENT-16 (VEC-AGENT-4): the allowance, the per-request rewrite and the request cap, retries included."""
    statuses = [s.get("status", 200) for s in seq["steps"]]
    upstream = StatusUpstream(statuses)
    p = make_proxy(tmp_path, upstream)
    async with p.client() as c:
        token = await web_token(c, _plain(seq["allowance"]) if seq["allowance"] else None)
        forwarded = 0
        for step, want in zip(seq["steps"], seq["expect"], strict=True):
            tools = copy.deepcopy(MAIN if step["kind"] == "main" else NESTED)
            r = await forward(c, token, body=body_with(tools))
            if "refused" in want:
                assert (r.status_code, r.json()) == (403, {"error": want["refused"]})
                continue
            assert r.status_code == step.get("status", 200)
            sent = json.loads(upstream.bodies[forwarded])
            forwarded += 1
            if "max_uses" in want:
                assert sent["tools"][0]["max_uses"] == want["max_uses"]
        record = (await c.post("/run-tokens/revoke", json={"run_token": token})).json()
    assert record["requests"] == seq["record"]["requests"]
    assert record["searches_forwarded"] == seq["record"]["searches_forwarded"]
    assert record["refused"] == seq["record"]["refused"]


async def test_allowance_invariant_over_random_sequences(tmp_path: Path) -> None:
    """INV-AGENT-6: for random request sequences, retries included, the bounds hold for what reaches upstream."""
    rng = random.Random(20261123)  # noqa: S311 (a seeded test sequence, not cryptography)
    for trial in range(25):
        allowance = {
            "searches_max": rng.randint(1, 4),
            "searches_per_request_max": rng.randint(1, 3),
            "requests_max": rng.randint(1, 6),
        }
        upstream = Upstream(status=rng.choice([200, 200, 429, 529]))
        p = make_proxy(tmp_path / str(trial), upstream)
        async with p.client() as c:
            token = await web_token(c, allowance)
            plain = (await issue_with(c)).json()["run_token"]
            for _ in range(rng.randint(1, 10)):
                kind = rng.choice(["main", "nested", "nested", "plain-tools", "mcp"])
                if kind == "plain-tools":
                    await forward(c, plain, body=body_with(copy.deepcopy(NESTED)))
                elif kind == "mcp":
                    await forward(c, token, body=body_with(copy.deepcopy(MAIN), mcp_servers=[{"name": "x"}]))
                else:
                    tools = copy.deepcopy(MAIN if kind == "main" else NESTED)
                    if kind == "nested" and rng.random() < 0.3:
                        tools[0]["max_uses"] = rng.randint(1, 9)
                    await forward(c, token, body=body_with(tools))
        before = len(upstream.bodies)
        async with p.client(LANE_ADDR) as lane:  # INV-AGENT-6 amended: worker-judge never gets an allowance
            refused = await issue_with(lane, task=rng.choice(["freshness_web", "judge.find"]), web_search=allowance)
            assert (refused.status_code, refused.json()) == (400, {"error": "tools_not_allowed"})
            plain_lane = (await issue_with(lane)).json()["run_token"]
            r = await forward(lane, plain_lane, body=body_with(copy.deepcopy(rng.choice([MAIN, NESTED]))))
            assert r.status_code == 403
        assert len(upstream.bodies) == before  # nothing with tools forwarded for the lane
        sent = [json.loads(b) for b in upstream.bodies]
        assert len(sent) <= allowance["requests_max"]
        uses = [t["max_uses"] for b in sent for t in b.get("tools", []) if "type" in t]
        assert all(u <= allowance["searches_per_request_max"] for u in uses)
        assert sum(uses) <= allowance["searches_max"]
        assert all(not b.get("mcp_servers") for b in sent)


async def test_proxy_log_names_the_route_kind_only(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """FR-AGENT-16: the log line adds `main` | `search` and nothing else (no query, tool or token)."""
    p = make_proxy(tmp_path)
    caplog.set_level(logging.INFO, logger="reader.agent.proxy")
    async with p.client() as c:
        token = await web_token(c)
        await forward(c, token, body=body_with(copy.deepcopy(MAIN)))
        await forward(c, token, body=body_with(copy.deepcopy(NESTED)))
    lines = [r.getMessage() for r in caplog.records if "/v1/messages" in r.getMessage()]
    assert [line.split()[-1] for line in lines] == ["kind=main", "kind=search"]
    assert all("WebSearch" not in line and token not in line and "web_search" not in line for line in lines)


@pytest.mark.parametrize("case", SEQ["settlements"], ids=lambda c: c["id"])
def test_tool_call_settlements(case: dict[str, Any]) -> None:
    """FR-AGENT-17 (VEC-AGENT-4 `settlements`)."""
    ledger = need("reader.agent.ledger")
    rec = case["record"]
    record = (
        None
        if rec is None
        else ledger.UpstreamRecord(
            tuple(rec["statuses"]), tuple(rec["refused"]), rec.get("in_flight", 0), rec.get("failed", 0)
        )
    )
    sdk = None if case["sdk_usd"] is None else D(case["sdk_usd"])
    settled, _ = ledger.settle_amount(
        D(case["reserve"]),
        record,
        sdk,
        error_result=case.get("error_result", False),
        tool_call=case["tool_call"],
        requests_max=case.get("requests_max", 3),
    )
    assert settled == D(case["settled"])


def test_haiku_tiers_and_the_search_fee() -> None:
    """FR-AGENT-17: the tier follows the prompt tokens; the fee is added per search; the reserve uses the higher
    tier past 100,000 estimated prompt tokens."""
    pricing = need("reader.agent.pricing")
    ledger = need("reader.agent.ledger")
    live = pricing.Usage(input=14452, output=835, cache_write_5m=0, cache_write_1h=0, cache_read=0,
                         web_search_requests=1)
    # the owner's live search request (spikes/web-search § 5b): $0.011863 with the fee
    assert pricing.cost("claude-haiku-5-5", live) == D("0.011863")
    big = pricing.Usage(input=200500, output=835, cache_write_5m=0, cache_write_1h=0, cache_read=0)
    assert pricing.cost("claude-haiku-5-5", big) == D("0.102338")  # 200,500 * 0.50 + 835 * 2.50, per 1e6
    sonnet = pricing.Usage(input=1000, output=0, cache_write_5m=0, cache_write_1h=0, cache_read=0,
                           web_search_requests=2)
    assert pricing.cost("claude-sonnet-5-5", sonnet) == D("0.022000")
    small = ledger.worst_case("claude-haiku-5-5", 2000, 1000, custom_system=True)
    large = ledger.worst_case("claude-haiku-5-5", 220000, 1000, custom_system=True)
    assert small == D("0.000900")  # (1,000 + 1,000) * 0.20 + 1,000 * 0.50
    assert large == D("0.113500")  # (1,000 + 110,000) * 1.00 + 1,000 * 2.50: the higher tier
    # The boundary is inclusive: exactly 100,000 prompt tokens is the lower tier, 100,001 the higher.
    at = pricing.Usage(input=100000, output=0, cache_write_5m=0, cache_write_1h=0, cache_read=0)
    over = pricing.Usage(input=100001, output=0, cache_write_5m=0, cache_write_1h=0, cache_read=0)
    assert pricing.cost("claude-haiku-5-5", at) == D("0.010000")  # 100,000 * 0.10
    assert pricing.cost("claude-haiku-5-5", over) == D("0.050001")  # 100,001 * 0.50, half up
    # A tool call's summed model_usage is priced at the lower tier (it cannot say which request crossed).
    assert pricing.cost("claude-haiku-5-5", big, summed=True) == D("0.020468")  # 200,500 * 0.10 + 835 * 0.50


def test_model_usage_sum_and_tier_note(caplog: pytest.LogCaptureFixture) -> None:
    """FR-AGENT-17: a tool call's usage is the sum over model_usage; a summed prompt over 100k logs tier_unknown."""
    pricing = need("reader.agent.pricing")
    entry = {
        "inputTokens": 14456, "outputTokens": 1147, "cacheReadInputTokens": 1281,
        "cacheCreationInputTokens": 2781, "cacheCreationEphemeral5mInputTokens": 0,
        "cacheCreationEphemeral1hInputTokens": 2781, "webSearchRequests": 1, "thinkingTokens": 442,
    }
    usage = pricing.map_model_usage({"claude-haiku-5-5": entry})
    assert usage == pricing.Usage(input=14456, output=1147, cache_write_5m=0, cache_write_1h=2781,
                                  cache_read=1281, thinking=442, web_search_requests=1)
    assert pricing.cost("claude-haiku-5-5", usage) == D("0.012588")  # = the live call's SDK and proxy totals
    caplog.set_level(logging.INFO, logger="reader.agent")
    pricing.log_crosscheck("claude-haiku-5-5", D("0.012588"), D("0.012588"), usage)
    assert not [r for r in caplog.records if "tier_unknown" in r.getMessage()]
    big = pricing.Usage(input=150000, output=10, cache_write_5m=0, cache_write_1h=0, cache_read=0)
    pricing.log_crosscheck("claude-haiku-5-5", D("0.08"), D("0.015005"), big)
    notes = [r for r in caplog.records if "tier_unknown" in r.getMessage()]
    assert len(notes) == 1 and notes[0].levelno == logging.INFO
    with pytest.raises(need("reader.agent.errors").AgentError):
        pricing.map_model_usage({"claude-haiku-5-5": {**entry, "inputTokens": -1}})
    # The TTL breakdown must add up to cacheCreationInputTokens: missing or inconsistent is usage_unmapped.
    for broken in (
        {**entry, "cacheCreationEphemeral1hInputTokens": 2780},
        {k: v for k, v in entry.items() if k != "cacheCreationEphemeral1hInputTokens"},
        {**entry, "cacheCreationEphemeral5mInputTokens": 1},
    ):
        with pytest.raises(need("reader.agent.errors").AgentError) as refused:
            pricing.map_model_usage({"claude-haiku-5-5": broken})
        assert refused.value.code == "usage_unmapped"
