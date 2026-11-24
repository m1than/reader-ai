# @trace FR-AGENT-6, FR-AGENT-7, FR-AGENT-9, FR-AGENT-10, INV-AGENT-5
"""The Provider port's Agent SDK adapter with a fake CLI and a fake agent-proxy client, against the real ledger.
[db]"""

from __future__ import annotations

import logging
import os
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from .conftest import FakeCli, FakeProxyClient, Pg, Script, need, rows, world

pytestmark = pytest.mark.anyio
D = Decimal
ENV_KEYS = {
    "PATH",
    "HOME",
    "LANG",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "DISABLE_TELEMETRY",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
}


def _call(w: Any, run: uuid.UUID, task: str = "fact_check", stage: str = "fact_check", **kw: Any) -> Any:
    provider = need("reader.agent.provider")
    base: dict[str, Any] = {
        "owner_id": w.users["ivan"],
        "handle": "ivan",
        "run_id": run,
        "lesson_id": None,
        "stage": stage,
        "task": task,
        "attempt": 0,
        "system": "SYSTEM",
        "prompt": "PROMPT",
        "max_output_tokens": 16000,
        "floor_usd": D("0.25"),
        "run_cap_usd": D("8.00"),
    }
    base.update(kw)
    return provider.StageCall(**base)


def _provider(w: Any, scripts: list[Script], tmp: Path) -> tuple[Any, FakeCli, FakeProxyClient]:
    provider = need("reader.agent.provider")
    cli = FakeCli(scripts)
    proxy = FakeProxyClient(cli)
    home = tmp / "homes"
    home.mkdir()
    return provider.AgentProvider(w.engine, w.config, proxy, cli=cli, home_root=home), cli, proxy


async def _ledger(w: Any) -> list[tuple[str, Any, Any]]:
    return [
        (r[0], r[1], r[2])
        for r in await rows(w.engine, "SELECT kind, usd, settled_usd FROM agent_spend ORDER BY id")
    ]


async def test_success_reserves_runs_and_settles(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    p, cli, proxy = _provider(w, [Script(text="answer", cost=0.05)], tmp_path)
    run = uuid.uuid4()
    result = await p.run(_call(w, run))
    assert result.text == "answer"
    assert result.model == "claude-sonnet-5-5"  # the task's alias, resolved
    assert result.sdk_usd == D("0.05")
    req = cli.requests[0]
    assert set(req.env) == ENV_KEYS
    assert req.env["ANTHROPIC_BASE_URL"] == "http://agent-proxy:8080"
    assert req.env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert req.env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "16000"
    assert req.env["CLAUDE_CODE_OAUTH_TOKEN"] == proxy.revoked[0]  # the run_token, revoked after the call
    assert not os.path.exists(req.env["HOME"])  # the per-run HOME is removed  # noqa: ASYNC240
    assert proxy.issued == [("ivan", 16000, "claude-sonnet-5-5")]  # the token is bound to the model
    assert cli.systems == [("SYSTEM", 0o600)]  # the system prompt travels as a 0600 file, never in argv
    assert req.system_file.name == "system.md"
    ledger = await _ledger(w)
    assert [k for k, _, _ in ledger] == ["reserve", "settle"]
    assert ledger[1][2] == D("0.05")
    model = await rows(w.engine, "SELECT DISTINCT model FROM agent_spend")
    assert model == [("claude-sonnet-5-5",)]


async def test_budget_overrides_are_enforced_live_per_call(pg: Pg, tmp_path: Path) -> None:
    """SRV-CONFIG FR-CONFIG-26 (paired): the reserve reads the owner's overrides at each call; the file's values
    (monthly 200, fact_check 2.0) allow this call, as its last run shows."""
    from sqlalchemy import text

    errors = need("reader.agent.errors")
    w = await world(pg, tmp_path)
    p, _, proxy = _provider(w, [Script(text="answer", cost=0.05)], tmp_path)
    run = uuid.uuid4()
    insert = text(
        "INSERT INTO settings_overrides (key, value, updated_by, version) VALUES (:k, CAST(:v AS jsonb), :u, 1)"
    )
    for key, code in [
        ("budgets.monthly_usd", "monthly_budget_exhausted"),
        ("tasks.fact_check.budget_usd_per_lesson", "task_budget_exhausted"),
    ]:
        async with w.engine.begin() as conn:
            await conn.execute(insert, {"k": key, "v": "0.1", "u": w.users["ivan"]})
        with pytest.raises(errors.BudgetRefused) as refused:
            await p.run(_call(w, run))
        assert refused.value.code == code, key
        assert await _ledger(w) == [] and proxy.issued == []
        async with w.engine.begin() as conn:
            await conn.execute(text("DELETE FROM settings_overrides"))
    result = await p.run(_call(w, run))
    assert result.text == "answer"
    assert [kind for kind, _, _ in await _ledger(w)] == ["reserve", "settle"]


def test_sdk_options_have_no_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = need("reader.agent.provider")
    monkeypatch.setenv("READER_DB_HOST", "postgres")  # a worker variable the CLI must not see
    req = provider.CliRequest(
        system_file=Path("/tmp/x/system.md"),
        prompt="P",
        model="claude-sonnet-5-5",
        env={"HOME": "/tmp/x"},
        max_budget_usd=0.5,
        home=Path("/tmp/x"),
    )
    options = provider.sdk_options(req)
    assert options.tools == [] and options.allowed_tools == [] and options.setting_sources == []
    assert options.max_turns == 1 and options.model == "claude-sonnet-5-5"
    assert options.max_budget_usd == 0.5
    assert options.system_prompt == {"type": "file", "path": "/tmp/x/system.md"}
    # S1: the SDK merges os.environ; every worker variable is overridden with an empty value
    assert options.env["HOME"] == "/tmp/x"
    assert options.env["READER_DB_HOST"] == ""
    assert {k for k, v in options.env.items() if v} == {"HOME"}
    assert set(options.env) == {*os.environ, "HOME"}
    assert options.stderr is not None  # stderr is read and discarded
    assert options.mcp_servers == {} or options.mcp_servers == []
    default = provider.sdk_options(dataclasses_replace(req, system_file=None))
    assert default.system_prompt == {"type": "preset", "preset": "claude_code"}


def dataclasses_replace(obj: Any, **changes: Any) -> Any:
    import dataclasses

    return dataclasses.replace(obj, **changes)


async def test_missing_init_message_is_rejected(pg: Pg, tmp_path: Path) -> None:
    errors = need("reader.agent.errors")
    w = await world(pg, tmp_path)
    p, _, _ = _provider(w, [Script(send_init=False)], tmp_path)
    with pytest.raises(errors.AgentError) as refused:
        await p.run(_call(w, uuid.uuid4()))
    assert refused.value.code == "tools_mismatch"


async def test_unparseable_revoke_answer_settles_at_the_reserve(pg: Pg, tmp_path: Path) -> None:
    import httpx

    provider = need("reader.agent.provider")
    client = provider.HttpProxyClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"<html>proxy error</html>")
        )
    )
    assert await client.revoke("rt-x") is None  # handled before settlement: the outcome is unknown


async def test_unknown_model_refused_before_any_row(pg: Pg, tmp_path: Path) -> None:
    errors = need("reader.agent.errors")
    w = await world(pg, tmp_path)
    p, cli, _ = _provider(w, [Script()], tmp_path)
    with pytest.raises(errors.AgentError) as refused:
        await p.run(_call(w, uuid.uuid4(), task="nope"))
    assert refused.value.code == "unknown_model"
    assert await _ledger(w) == [] and cli.requests == []


async def test_budget_refusal_spends_nothing(pg: Pg, tmp_path: Path) -> None:
    errors = need("reader.agent.errors")
    w = await world(pg, tmp_path)
    p, _, proxy = _provider(w, [Script()], tmp_path)
    with pytest.raises(errors.BudgetRefused) as refused:
        await p.run(_call(w, uuid.uuid4(), run_cap_usd=D("0.10")))
    assert refused.value.code == "budget_exhausted"
    assert await _ledger(w) == [] and proxy.issued == []


@pytest.mark.parametrize(
    "script",
    [
        Script(init_tools=["Bash"]),
        Script(init_tools=None),
        Script(init_mcp=[{"name": "x"}]),
        Script(init_model="claude-opus-5-5"),
    ],
    ids=["tools", "tools-missing", "mcp", "model"],
)
async def test_init_check(pg: Pg, tmp_path: Path, script: Script) -> None:
    errors = need("reader.agent.errors")
    w = await world(pg, tmp_path)
    p, _, _ = _provider(w, [script], tmp_path)
    with pytest.raises(errors.AgentError) as refused:
        await p.run(_call(w, uuid.uuid4()))
    assert refused.value.code == "tools_mismatch"
    assert not isinstance(refused.value, errors.TransientAgentError)
    ledger = await _ledger(w)
    assert ledger[-1][0] == "settle" and ledger[-1][2] == D(
        "0.05"
    )  # the request went out (one 2xx): its cost


@pytest.mark.parametrize(
    ("script", "code", "settled"),
    [
        (
            Script(assistant_error="rate_limit", is_error=True, cost=0, statuses=(429,) * 11),
            "rate_limited",
            "0",
        ),
        (
            Script(assistant_error="server_error", is_error=True, cost=0, statuses=(529,) * 11),
            "provider_unavailable",
            "0",
        ),
        (Script(cost=None), "sdk_error", "0.25"),  # 2xx, no SDK cost: the reserve
        (Script(revoke_unknown=True, cost=0.05), None, "0.25"),  # unknown record: the reserve
        (Script(statuses=(), refused=("output_cap",), is_error=True, cost=0), "sdk_error", "0"),
    ],
    ids=["429", "529", "no-cost", "unknown-record", "refused"],
)
async def test_errors_and_settlements(
    pg: Pg, tmp_path: Path, script: Script, code: str | None, settled: str
) -> None:
    errors = need("reader.agent.errors")
    w = await world(pg, tmp_path)
    p, _, _ = _provider(w, [script], tmp_path)
    if code is None:
        await p.run(_call(w, uuid.uuid4()))
    else:
        with pytest.raises(errors.AgentError) as failure:
            await p.run(_call(w, uuid.uuid4()))
        assert failure.value.code == code
        assert isinstance(failure.value, errors.TransientAgentError) is (
            code in ("rate_limited", "provider_unavailable")
        )
    ledger = await _ledger(w)
    assert ledger[0] == ("reserve", D("0.25"), None)  # the floor: tiny prompt
    assert ledger[1][0] == "settle" and ledger[1][2] == D(settled)


async def test_provider_missing(pg: Pg, tmp_path: Path) -> None:
    errors = need("reader.agent.errors")
    w = await world(pg, tmp_path)
    p, _, proxy = _provider(w, [Script()], tmp_path)
    proxy.missing.add("ivan")
    with pytest.raises(errors.AgentError) as failure:
        await p.run(_call(w, uuid.uuid4()))
    assert failure.value.code == "provider_missing"
    ledger = await _ledger(w)
    assert ledger[-1][0] == "settle" and ledger[-1][2] == D("0")  # nothing was sent


async def test_adapter_logs_no_prompt_output_or_token(
    pg: Pg, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    w = await world(pg, tmp_path)
    p, _, proxy = _provider(
        w, [Script(text="CANARY-OUT-91"), Script(assistant_error="rate_limit", is_error=True)], tmp_path
    )
    await p.run(_call(w, uuid.uuid4(), system="CANARY-SYS-11", prompt="CANARY-PROMPT-22"))
    with pytest.raises(Exception):  # noqa: B017, PT011
        await p.run(_call(w, uuid.uuid4(), system="CANARY-SYS-11", prompt="CANARY-PROMPT-22"))
    text = "\n".join(r.getMessage() for r in caplog.records)
    for secret in ("CANARY-OUT-91", "CANARY-SYS-11", "CANARY-PROMPT-22", *proxy.revoked):
        assert secret not in text
