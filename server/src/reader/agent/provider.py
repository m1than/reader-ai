"""The Provider port and its Agent SDK adapter (SRV-AGENT FR-AGENT-6, -7, -9, -10).

A call: resolve the task's model (refused before any spend), reserve, get a run_token from agent-proxy, run
the bundled CLI with no tools in a fresh HOME, check the init message, then in `finally` revoke the run_token,
remove HOME and settle from the SDK's cost and agent-proxy's upstream record. Nothing here logs a prompt, an
output or a token.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Protocol

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from reader.agent import ledger
from reader.agent.errors import AgentError, agent_error
from reader.agent.ledger import Caps, Scope, UpstreamRecord
from reader.agent.pricing import Usage, cost, log_crosscheck, map_usage, resolve_model
from reader.config import ReaderConfig

PROXY_URL: Final = "http://agent-proxy:8080"
PATH_ENV: Final = "/usr/local/bin:/usr/bin:/bin"
# AssistantMessage.error as measured (REF-AGENT-4 round 2): 429 → rate_limit; 529 and 500 → server_error.
ERROR_CODES: Final[Mapping[str, str]] = {
    "rate_limit": "rate_limited",
    "server_error": "provider_unavailable",
    "overloaded": "provider_unavailable",
}

log = logging.getLogger("reader.agent")


@dataclass(frozen=True)
class StageCall:
    owner_id: uuid.UUID
    handle: str
    run_id: uuid.UUID
    lesson_id: uuid.UUID | None
    stage: str
    task: str
    attempt: int
    system: str | None  # None: the CLI's default system prompt (FR-AGENT-13 b)
    prompt: str
    max_output_tokens: int
    floor_usd: Decimal
    run_cap_usd: Decimal
    daily_budget_usd: Decimal | None = None  # FR-AGENT-12
    thinking_off: bool = False  # FR-AGENT-13 a
    abort_on_retry: bool = False  # FR-AGENT-13 d
    deadline_s: float | None = None  # FR-AGENT-13 e


@dataclass(frozen=True)
class StageResult:
    text: str
    usage: Usage
    sdk_usd: Decimal
    model: str
    stop_reason: str | None = None  # FR-AGENT-13 c


class Provider(Protocol):
    async def run(self, call: StageCall) -> StageResult: ...


class ProxyClient(Protocol):
    async def issue(self, handle: str, max_output_tokens: int) -> str: ...

    async def revoke(self, run_token: str) -> UpstreamRecord | None: ...


@dataclass(frozen=True)
class CliRequest:
    system: str | None
    prompt: str
    model: str
    env: dict[str, str]
    max_budget_usd: float
    home: Path


CliRun = Callable[[CliRequest], AsyncIterator[Any]]


class HttpProxyClient:
    """The worker's client for agent-proxy on agentnet (FR-AGENT-3)."""

    def __init__(self, base_url: str = PROXY_URL, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.client = httpx.AsyncClient(base_url=base_url, transport=transport, timeout=10.0, trust_env=False)

    async def issue(self, handle: str, max_output_tokens: int) -> str:
        try:
            r = await self.client.post(
                "/run-tokens", json={"user": handle, "max_output_tokens": max_output_tokens}
            )
        except httpx.HTTPError:
            raise agent_error("provider_unavailable") from None
        if r.status_code == 409:
            raise agent_error("provider_missing")
        token = r.json().get("run_token") if r.status_code == 200 else None
        if not isinstance(token, str):
            raise agent_error("provider_unavailable")
        return token

    async def revoke(self, run_token: str) -> UpstreamRecord | None:
        try:
            r = await self.client.post("/run-tokens/revoke", json={"run_token": run_token})
        except httpx.HTTPError:
            return None
        return UpstreamRecord.from_json(r.json()) if r.status_code == 200 else None


def sdk_options(request: CliRequest) -> Any:
    from claude_agent_sdk import ClaudeAgentOptions

    return ClaudeAgentOptions(
        tools=[],
        allowed_tools=[],
        setting_sources=[],
        max_turns=1,
        model=request.model,
        max_budget_usd=request.max_budget_usd,
        system_prompt=request.system,
        env=request.env,
        cwd=str(request.home),
    )


async def sdk_cli(request: CliRequest) -> AsyncIterator[Any]:
    """The real CLI through the Agent SDK (the `agent` extra)."""
    from claude_agent_sdk import query

    async for message in query(prompt=request.prompt, options=sdk_options(request)):
        yield message


def classify(
    *, assistant_error: str | None, is_error: bool, sdk_usd: Decimal | None, record: UpstreamRecord | None
) -> str | None:
    """The error code of a finished call, or None when it succeeded (FR-AGENT-6, VEC-AGENT-1)."""
    if record is not None and record.refused:
        return "sdk_error"  # agent-proxy refused the request: a bug on our side, never retried
    if assistant_error is not None:
        return ERROR_CODES.get(assistant_error, "sdk_error")
    if is_error or sdk_usd is None:
        return "sdk_error"
    return None


@dataclass
class _Seen:
    init: dict[str, Any] | None = None
    assistant_error: str | None = None
    result: Any = None
    texts: list[str] = field(default_factory=list)
    retried: bool = False


def _kind(message: Any) -> str:
    return type(message).__name__


class AgentProvider:
    def __init__(
        self,
        engine: AsyncEngine,
        config: ReaderConfig,
        proxy: ProxyClient,
        *,
        cli: CliRun = sdk_cli,
        home_root: Path | None = None,
        proxy_url: str = PROXY_URL,
    ) -> None:
        self.engine, self.config, self.proxy, self.cli = engine, config, proxy, cli
        self.home_root = home_root
        self.proxy_url = proxy_url

    def _caps(self, call: StageCall) -> Caps:
        task = self.config.tasks.get(call.task)
        budgets = self.config.budgets
        return Caps(
            run_cap_usd=call.run_cap_usd,
            task_budget_usd=task.budget_usd_per_lesson if task else None,
            monthly_usd=budgets.monthly_usd if budgets else None,
            warn_at=budgets.warn_at if budgets else None,
            daily_task_usd=call.daily_budget_usd,
        )

    async def run(self, call: StageCall) -> StageResult:
        task = self.config.tasks.get(call.task)
        if task is None:
            raise agent_error("unknown_model")
        model = resolve_model(task.model)
        reserve = ledger.reserve_amount(
            model,
            len(call.system or "") + len(call.prompt),
            call.max_output_tokens,
            call.floor_usd,
            custom_system=call.system is not None,
        )
        scope = Scope(call.owner_id, call.run_id, call.lesson_id, call.stage, call.task, call.attempt)
        reservation = await ledger.reserve(self.engine, scope, model, reserve, self._caps(call))
        started = time.monotonic()
        home = Path(tempfile.mkdtemp(prefix="reader-cli-", dir=self.home_root))  # mode 0700
        run_token: str | None = None
        seen = _Seen()
        record: UpstreamRecord | None = UpstreamRecord((), ())  # nothing sent until a run_token exists
        sdk_usd: Decimal | None = None
        usage: Usage | None = None
        failure: AgentError | None = None
        try:
            run_token = await self.proxy.issue(call.handle, call.max_output_tokens)
            try:
                async with asyncio.timeout(call.deadline_s):
                    await self._drive(call, model, reserve, run_token, home, seen)
            except TimeoutError:
                raise agent_error("deadline_exceeded") from None
            if seen.retried and call.abort_on_retry:
                raise agent_error("upstream_retry")
            result = seen.result
            if result is not None and result.total_cost_usd is not None:
                sdk_usd = Decimal(str(result.total_cost_usd))
            if result is not None and result.usage:
                try:
                    usage = map_usage(result.usage)
                except AgentError as error:
                    failure = error
        except AgentError as error:
            failure = error
        finally:
            if run_token is not None:
                record = await self.proxy.revoke(run_token)
            shutil.rmtree(home, ignore_errors=True)
            settled, warn = ledger.settle_amount(reserve, record, sdk_usd)
            if warn:
                log.warning("%s stage=%s run=%s", warn, call.stage, call.run_id)
            usd = cost(model, usage) if usage is not None else None
            if usd is not None and sdk_usd is not None:
                log_crosscheck(model, sdk_usd, usd)
            await ledger.settle(self.engine, reservation, settled, sdk_usd, usd, usage, record)
        log.info(
            "agent_call stage=%s run=%s model=%s settled=%s seconds=%.1f",
            call.stage,
            call.run_id,
            model,
            settled,
            time.monotonic() - started,
        )
        if failure is not None:
            raise failure
        if seen.init is None or not _init_ok(seen.init, model):
            raise agent_error("tools_mismatch")
        code = classify(
            assistant_error=seen.assistant_error,
            is_error=bool(seen.result is None or seen.result.is_error),
            sdk_usd=sdk_usd,
            record=record,
        )
        if code is not None:
            raise agent_error(code)
        assert sdk_usd is not None  # noqa: S101 (classify returned None)
        if usage is None:
            raise agent_error("usage_unmapped")
        text = seen.result.result if isinstance(seen.result.result, str) else "".join(seen.texts)
        stop = getattr(seen.result, "stop_reason", None)
        return StageResult(text, usage, sdk_usd, model, stop if isinstance(stop, str) else None)

    async def _drive(
        self, call: StageCall, model: str, reserve: Decimal, run_token: str, home: Path, seen: _Seen
    ) -> None:
        env = {
            "PATH": PATH_ENV,
            "HOME": str(home),
            "LANG": "C.UTF-8",
            "CLAUDE_CODE_OAUTH_TOKEN": run_token,
            "ANTHROPIC_BASE_URL": self.proxy_url,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_TELEMETRY": "1",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(call.max_output_tokens),
        }
        if call.thinking_off:
            env["MAX_THINKING_TOKENS"] = "0"
        request = CliRequest(call.system, call.prompt, model, env, float(reserve), home)
        stream = self.cli(request)
        try:
            async for message in stream:
                kind = _kind(message)
                subtype = getattr(message, "subtype", None)
                if kind == "SystemMessage" and subtype == "init" and seen.init is None:
                    seen.init = dict(getattr(message, "data", {}) or {})
                elif kind == "SystemMessage" and subtype == "api_retry":
                    if not seen.retried:
                        log.info("api_retry stage=%s run=%s", call.stage, call.run_id)
                    seen.retried = True
                    if call.abort_on_retry:
                        break  # FR-AGENT-13 d: closed below, before the CLI's own backoff
                elif kind == "AssistantMessage":
                    # FR-AGENT-7: no assistant output is used unless the init message came first and passed.
                    error = getattr(message, "error", None)
                    if error is not None:
                        seen.assistant_error = str(error)
                    for block in getattr(message, "content", []) or []:
                        if isinstance(getattr(block, "text", None), str):
                            seen.texts.append(block.text)
                elif kind == "ResultMessage":
                    seen.result = message
        except Exception:
            if seen.result is None and seen.assistant_error is None:
                raise agent_error("sdk_error") from None
        finally:
            with contextlib.suppress(Exception):
                await stream.aclose()  # type: ignore[attr-defined]


def _init_ok(init: Mapping[str, Any], model: str) -> bool:
    return init.get("tools") == [] and not init.get("mcp_servers") and init.get("model") == model
