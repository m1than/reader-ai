"""The Provider port and its Agent SDK adapter (SRV-AGENT FR-AGENT-6, -7, -9, -10, -13).

A call: resolve the task's model (refused before any spend), reserve, get a run_token from agent-proxy, run
the bundled CLI with no tools in a fresh HOME, check the init message, then in `finally` revoke the run_token,
remove HOME and settle from the SDK's cost and agent-proxy's upstream record. Nothing here logs a prompt, an
output or a token.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Protocol

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from reader.agent import judges, ledger
from reader.agent.errors import PROXY_CODES, AgentError, agent_error
from reader.agent.ledger import Caps, Scope, UpstreamRecord
from reader.agent.pricing import (
    Usage,
    cost,
    log_crosscheck,
    map_model_usage,
    map_usage,
    micro,
    resolve_model,
)
from reader.config import ReaderConfig
from reader.config.effective import effective_settings, file_settings
from reader.obs.agent import AgentSpan, agent_call
from reader.obs.cli_env import cli_env_additions

PROXY_URL: Final = "http://agent-proxy:8080"
PATH_ENV: Final = "/usr/local/bin:/usr/bin:/bin"
SYSTEM_FILE: Final = "system.md"  # in the per-run HOME, mode 0600 (code review B1: argv limit 131,071 bytes)
# AssistantMessage.error as measured (REF-AGENT-4 round 2): 429 → rate_limit; 529 and 500 → server_error.
ERROR_CODES: Final[Mapping[str, str]] = {
    "rate_limit": "rate_limited",
    "server_error": "provider_unavailable",
    "overloaded": "provider_unavailable",
}

log = logging.getLogger("reader.agent")

# FR-AGENT-15 (ADR-0030): the only (task, stage) that may carry tools, and which.
TOOL_STAGES: Final[Mapping[tuple[str, str], tuple[str, ...]]] = {("freshness_web", "search"): ("WebSearch",)}


@dataclass(frozen=True)
class WebSearchAllowance:
    """The run_token's search allowance, enforced by agent-proxy (FR-AGENT-16)."""

    searches_max: int
    searches_per_request_max: int
    requests_max: int

    def as_json(self) -> dict[str, int]:
        return {
            "searches_max": self.searches_max,
            "searches_per_request_max": self.searches_per_request_max,
            "requests_max": self.requests_max,
        }


@dataclass(frozen=True)
class WebSearchCall:
    """The only way a StageCall asks for tool use (FR-AGENT-15): the allowance, the caller's PreToolUse hook
    callback (SRV-FRESH FR-FRESH-6) and the CLI's max_turns. The tools come from TOOL_STAGES."""

    allowance: WebSearchAllowance
    hook: Any
    max_turns: int


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
    course_id: uuid.UUID | None = None  # FR-AGENT-14 (SRV-OBS FR-OBS-12 d): on the `agent.call` span only
    step: int | None = None  # FR-AGENT-14: the compile step, on the `agent.call` span only
    web_search: WebSearchCall | None = None  # FR-AGENT-15: only for a (task, stage) in TOOL_STAGES
    line: str | None = None  # FR-AGENT-19: required for judge.find, else None or the task's fixed line

    @property
    def declared_tools(self) -> tuple[str, ...]:
        """The tools this call declares: TOOL_STAGES' entry when it asks for web search, else none."""
        if self.web_search is None:
            return ()
        return TOOL_STAGES.get((self.task, self.stage), ())


@dataclass(frozen=True)
class PlannedCall:
    """One call a compile loop will make: `reserve_for`'s arguments before model resolution (FR-AGENT-23)."""

    task: str
    prompt_chars: int
    max_output_tokens: int
    floor_usd: Decimal
    custom_system: bool = False
    searches_max: int = 0


@dataclass(frozen=True)
class StageResult:
    text: str
    usage: Usage
    sdk_usd: Decimal
    model: str
    stop_reason: str | None = None  # FR-AGENT-13 c
    tool_results: tuple[Any, ...] = ()  # each UserMessage.tool_use_result of a tool call, in order


class Provider(Protocol):
    async def run(self, call: StageCall) -> StageResult: ...


class ProxyClient(Protocol):
    async def issue(self, handle: str, max_output_tokens: int, model: str, **extra: Any) -> str: ...

    async def revoke(self, run_token: str) -> UpstreamRecord | None: ...


class Killer:
    """Holds the way to kill the running CLI process; set by the CLI runner, used on abort and deadline."""

    def __init__(self) -> None:
        self._kill: Callable[[], None] | None = None

    def set(self, kill: Callable[[], None]) -> None:
        self._kill = kill

    def kill(self) -> None:
        if self._kill is not None:
            with contextlib.suppress(Exception):
                self._kill()


@dataclass(frozen=True)
class CliRequest:
    system_file: Path | None  # the system prompt, in a 0600 file in HOME; None: the CLI's default
    prompt: str
    model: str
    env: dict[str, str]
    max_budget_usd: float
    home: Path
    killer: Killer = field(default_factory=Killer)
    tools: tuple[str, ...] = ()  # FR-AGENT-15
    max_turns: int = 1
    pre_tool_use: Any = None


CliRun = Callable[[CliRequest], AsyncIterator[Any]]


class HttpProxyClient:
    """The worker's client for agent-proxy on agentnet (FR-AGENT-3)."""

    def __init__(
        self,
        base_url: str = PROXY_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self.client = httpx.AsyncClient(
            base_url=base_url, transport=transport, timeout=timeout_s, trust_env=False
        )

    async def issue(self, handle: str, max_output_tokens: int, model: str, **extra: Any) -> str:
        """`extra`: `task` and `web_search` for a tool call (FR-AGENT-15). FR-AGENT-6 maps a refusal."""
        body = {"user": handle, "max_output_tokens": max_output_tokens, "model": model, **extra}
        try:
            r = await self.client.post("/run-tokens", json=body)
        except httpx.HTTPError:
            raise agent_error("provider_unavailable") from None
        try:
            data: Any = r.json()
        except ValueError:
            data = None
        if r.status_code == 409:
            raise agent_error("provider_missing")
        if r.status_code == 429 and isinstance(data, dict) and data.get("error") == "client_quota":
            raise agent_error(PROXY_CODES["client_quota"])
        if r.status_code in (400, 403):  # client_unknown, bad_request, tools_not_allowed: never retried
            raise agent_error("sdk_error")
        token = data.get("run_token") if r.status_code == 200 and isinstance(data, dict) else None
        if not isinstance(token, str):
            raise agent_error("provider_unavailable")
        return token

    async def revoke_all(self) -> int | None:
        """Revoke every token of this client (FR-AGENT-3 revised); None on any failure."""
        try:
            r = await self.client.post("/run-tokens/revoke-all", json={})
            revoked = r.json().get("revoked") if r.status_code == 200 else None
        except httpx.HTTPError, ValueError, AttributeError:
            return None
        return revoked if type(revoked) is int else None

    async def revoke(self, run_token: str) -> UpstreamRecord | None:
        """The upstream record, or None when unknown (404, a failed call, or an answer that is not JSON)."""
        try:
            r = await self.client.post("/run-tokens/revoke", json={"run_token": run_token})
            return UpstreamRecord.from_json(r.json()) if r.status_code == 200 else None
        except httpx.HTTPError, ValueError:
            return None

    async def aclose(self) -> None:
        await self.client.aclose()


def cli_env(env: Mapping[str, str]) -> dict[str, str]:
    """The SDK merges the worker's own environment into the CLI's: blank every variable not passed (S1)."""
    return {**{name: "" for name in os.environ}, **env}


def _discard(_: str) -> None:
    """The CLI's stderr is read and dropped (it may echo a prompt)."""


def sdk_options(request: CliRequest) -> Any:
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

    system: Any = (
        {"type": "file", "path": str(request.system_file)}
        if request.system_file is not None
        else {"type": "preset", "preset": "claude_code"}  # the SDK turns None into an empty prompt
    )
    hooks: Any = None
    if request.tools and request.pre_tool_use is not None:  # FR-AGENT-15: the caller's gate on each call
        hooks = {"PreToolUse": [HookMatcher(matcher="|".join(request.tools), hooks=[request.pre_tool_use])]}
    return ClaudeAgentOptions(
        tools=list(request.tools),
        allowed_tools=[],
        setting_sources=[],
        max_turns=request.max_turns,
        hooks=hooks,
        model=request.model,
        max_budget_usd=request.max_budget_usd,
        system_prompt=system,
        env=cli_env(request.env),
        cwd=str(request.home),
        stderr=_discard,
    )


async def sdk_cli(request: CliRequest) -> AsyncIterator[Any]:
    """The real CLI through the Agent SDK (the `agent` extra); the killer can kill its process."""
    from claude_agent_sdk import query
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    options = sdk_options(request)
    transport = SubprocessCLITransport(prompt=request.prompt, options=options)

    def kill() -> None:
        process = getattr(transport, "_process", None)
        if process is not None and process.returncode is None:
            os.kill(process.pid, signal.SIGKILL)

    request.killer.set(kill)
    async for message in query(prompt=request.prompt, options=options, transport=transport):
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
    tool_results: list[Any] = field(default_factory=list)  # UserMessage.tool_use_result, in order


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
        line_caps: Callable[[ReaderConfig], Mapping[str, Decimal | None]] = judges.line_caps,
    ) -> None:
        self.engine, self.config, self.proxy, self.cli = engine, config, proxy, cli
        self.home_root = home_root
        self.proxy_url = proxy_url
        self.line_caps = line_caps

    async def _settings(self) -> Any:
        # SRV-CONFIG FR-CONFIG-26 (paired): the budgets as reader.yaml plus the owner's overrides, per call.
        engine: Any = self.engine  # None only where the ledger is in memory (the freshness [image] check)
        if engine is None:  # no database, so no overrides: reader.yaml alone
            return file_settings(self.config)
        async with engine.connect() as conn:
            return await effective_settings(conn, self.config)

    def _line_cap(self, task: str, line: str | None) -> tuple[Decimal | None, str]:
        """FR-AGENT-19: the configured cap a call runs under and its refusal code; `cap_missing` if unset."""
        key = judges.cap_key(task, line)
        if key is None:
            return None, "budget_exhausted"
        cap = self.line_caps(self.config).get(key)
        if cap is None:
            raise agent_error("cap_missing")
        return cap, "index_budget_exhausted" if key.startswith("index") else "eval_budget_exhausted"

    async def _caps(self, call: StageCall) -> Caps:
        settings = await self._settings()
        line_cap, run_code = self._line_cap(call.task, judges.line_for(call.task, call.line))
        return Caps(
            run_cap_usd=call.run_cap_usd if line_cap is None else min(call.run_cap_usd, line_cap),
            task_budget_usd=settings.task_budgets.get(call.task),
            monthly_usd=settings.monthly_usd,
            warn_at=settings.warn_at,
            daily_task_usd=call.daily_budget_usd,
            run_code=run_code,
        )

    async def run(self, call: StageCall) -> StageResult:
        if call.web_search is not None and (
            (call.task, call.stage) not in TOOL_STAGES or call.web_search.hook is None
        ):
            raise agent_error("tools_mismatch")  # FR-AGENT-15: before any reservation; no tool without a gate
        line = judges.line_for(call.task, call.line)  # FR-AGENT-19: before any reservation and the span
        task = self.config.tasks.get(call.task)
        if task is None:
            raise agent_error("unknown_model")
        model = resolve_model(task.model)
        self._line_cap(call.task, line)  # `cap_missing` before any spend
        # FR-AGENT-14 (SRV-OBS FR-OBS-6): the call runs inside `agent.call`, current while the CLI starts.
        with agent_call(call.task, call.stage, model, call.lesson_id, call.course_id, call.step) as span:
            return await self._run(call, model, span, line)

    async def loop_fits(
        self, owner_id: uuid.UUID, run_id: uuid.UUID, run_cap_usd: Decimal, planned: Sequence[PlannedCall]
    ) -> bool:
        """FR-AGENT-23: would every call of one loop pass FR-AGENT-9's caps, summed, now? Writes nothing."""
        return await self.loop_fits_limit(owner_id, run_id, run_cap_usd, planned) is None

    async def loop_fits_limit(
        self, owner_id: uuid.UUID, run_id: uuid.UUID, run_cap_usd: Decimal, planned: Sequence[PlannedCall]
    ) -> str | None:
        """FR-AGENT-23: None when the loop fits, else the first refusing limit: `run`, `task`, `month`."""
        settings = await self._settings()
        by_task: dict[str, Decimal] = {}
        for p in planned:
            task = self.config.tasks.get(p.task)
            if task is None:
                raise agent_error("unknown_model")
            usd = ledger.reserve_for(
                resolve_model(task.model), p.prompt_chars, p.max_output_tokens, p.floor_usd,
                custom_system=p.custom_system, searches_max=p.searches_max,
            )
            by_task[p.task] = by_task.get(p.task, Decimal(0)) + usd
        total = sum(by_task.values(), Decimal(0))
        if await ledger.spent(self.engine, run_id=run_id) + total > run_cap_usd:
            return "run"
        for name, usd in by_task.items():  # first-planned order
            budget = settings.task_budgets.get(name)
            if budget is None:
                continue
            if await ledger.spent(self.engine, run_id=run_id, task=name) + usd > budget:
                return "task"
        monthly: Decimal | None = settings.monthly_usd
        if monthly is None:
            return None
        month: Decimal = await ledger.spent(self.engine, owner_id=owner_id, month=True)
        return None if month + total <= monthly else "month"

    async def _run(self, call: StageCall, model: str, span: AgentSpan, line: str | None) -> StageResult:
        tool_call = call.web_search is not None
        reserve = ledger.reserve_for(
            model,
            len(call.system or "") + len(call.prompt),
            call.max_output_tokens,
            call.floor_usd,
            custom_system=call.system is not None,
            searches_max=call.web_search.allowance.searches_max if call.web_search is not None else 0,
        )
        scope = Scope(call.owner_id, call.run_id, call.lesson_id, call.stage, call.task, call.attempt, line)
        reservation = await ledger.reserve(self.engine, scope, model, reserve, await self._caps(call))
        started = time.monotonic()
        home: Path | None = None
        run_token: str | None = None
        seen = _Seen()
        record: UpstreamRecord | None = UpstreamRecord((), ())  # nothing sent until a run_token exists
        sdk_usd: Decimal | None = None
        usage: Usage | None = None
        failure: AgentError | None = None
        try:
            home = Path(tempfile.mkdtemp(prefix="reader-cli-", dir=self.home_root))  # mode 0700
            extra: dict[str, Any] = (
                {"task": call.task, "web_search": call.web_search.allowance.as_json()}
                if call.web_search is not None
                else {}
            )
            run_token = await self.proxy.issue(call.handle, call.max_output_tokens, model, **extra)
            await self.drive_bounded(call, model, reserve, run_token, home, seen)
            if seen.retried and call.abort_on_retry:
                raise agent_error("upstream_retry")
            result = seen.result
            if result is not None and result.total_cost_usd is not None:
                sdk_usd = Decimal(str(result.total_cost_usd))
            # FR-AGENT-17: a tool call's ResultMessage.usage covers the main loop only; model_usage has all.
            raw_usage = getattr(result, "model_usage", None) if tool_call else getattr(result, "usage", None)
            if result is not None and raw_usage:
                try:
                    usage = map_model_usage(raw_usage) if tool_call else map_usage(raw_usage)
                except AgentError as error:
                    failure = error
        except AgentError as error:
            failure = error
        finally:
            if run_token is not None:
                record = await self.proxy.revoke(run_token)
            if home is not None:
                shutil.rmtree(home, ignore_errors=True)
            error_result = seen.result is None or bool(getattr(seen.result, "is_error", False))
            settled, warn = ledger.settle_amount(
                reserve,
                record,
                sdk_usd,
                error_result=error_result,
                tool_call=tool_call,
                requests_max=call.web_search.allowance.requests_max if call.web_search is not None else None,
            )
            if warn:
                log.warning("%s stage=%s run=%s", warn, call.stage, call.run_id)
            usd = cost(model, usage, summed=tool_call) if usage is not None else None
            if usd is not None and sdk_usd is not None:
                log_crosscheck(model, sdk_usd, usd, usage if tool_call else None)
            # FR-AGENT-14: quantized once with the ledger's rounding; the ledger and the span get one value.
            settled = micro(settled)
            await ledger.settle(self.engine, reservation, settled, sdk_usd, usd, usage, record)
            stop = getattr(seen.result, "stop_reason", None)
            span.settled(usage, settled, stop if isinstance(stop, str) else None, summed=tool_call)
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
        if seen.init is None or not _init_ok(seen.init, model, call.declared_tools):
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
        return StageResult(
            text, usage, sdk_usd, model, stop if isinstance(stop, str) else None, tuple(seen.tool_results)
        )

    async def drive_bounded(
        self, call: StageCall, model: str, reserve: Decimal, run_token: str, home: Path, seen: _Seen
    ) -> None:
        """The CLI call under the call's deadline (FR-AGENT-13 e): `deadline_exceeded` with the CLI killed."""
        killer = Killer()
        # S6: the kill is scheduled before the timeout, so at the deadline it runs first, before the
        # cancellation reaches the SDK's own close (which would wait up to 5 s for the CLI to exit).
        timer = (
            asyncio.get_running_loop().call_later(call.deadline_s, killer.kill)
            if call.deadline_s is not None
            else None
        )
        try:
            async with asyncio.timeout(call.deadline_s):
                await self._drive(call, model, reserve, run_token, home, seen, killer)
        except TimeoutError:
            raise agent_error("deadline_exceeded") from None
        finally:
            if timer is not None:
                timer.cancel()

    async def _drive(
        self,
        call: StageCall,
        model: str,
        reserve: Decimal,
        run_token: str,
        home: Path,
        seen: _Seen,
        killer: Killer | None = None,
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
        # FR-AGENT-14 (SRV-OBS FR-OBS-6): traces only, content off. A provider built without a configuration
        # (the [image] CLI check drives `drive_bounded` directly) adds nothing.
        config: ReaderConfig | None = self.config  # None only in that check
        if config is not None and config.observability.enabled:
            env.update(cli_env_additions())
        if call.web_search is not None:  # FR-AGENT-15: the CLI's own per-turn search cap
            env["CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION"] = str(call.web_search.allowance.searches_max)
        system_file: Path | None = None
        if call.system is not None:
            system_file = home / SYSTEM_FILE
            fd = os.open(system_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                out.write(call.system)
        request = CliRequest(
            system_file,
            call.prompt,
            model,
            env,
            float(reserve),
            home,
            killer or Killer(),
            call.declared_tools,
            call.web_search.max_turns if call.web_search is not None else 1,
            call.web_search.hook if call.web_search is not None else None,
        )
        stream = self.cli(request)
        finished = False
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
                        break  # FR-AGENT-13 d: killed below, before the CLI's own backoff
                elif kind == "AssistantMessage":
                    # FR-AGENT-7: no assistant output is used unless the init message came first and passed.
                    error = getattr(message, "error", None)
                    if error is not None:
                        seen.assistant_error = str(error)
                    for block in getattr(message, "content", []) or []:
                        if isinstance(getattr(block, "text", None), str):
                            seen.texts.append(block.text)
                elif kind == "UserMessage" and call.web_search is not None:
                    result_data = getattr(message, "tool_use_result", None)
                    if isinstance(result_data, dict):
                        seen.tool_results.append(result_data)
                elif kind == "ResultMessage":
                    seen.result = message
            else:
                finished = True
        except Exception:
            finished = True
            if seen.result is None and seen.assistant_error is None:
                raise agent_error("sdk_error") from None
        finally:
            if not finished:  # abort or deadline (a cancellation lands here too): no 5 s grace (S6)
                request.killer.kill()
            with contextlib.suppress(Exception):
                await stream.aclose()  # type: ignore[attr-defined]


def _init_ok(init: Mapping[str, Any], model: str, tools: tuple[str, ...] = ()) -> bool:
    """FR-AGENT-7: the init tool list equals the call's declared tools ([] except FR-AGENT-15's)."""
    return init.get("tools") == list(tools) and not init.get("mcp_servers") and init.get("model") == model
