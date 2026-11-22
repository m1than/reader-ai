"""Lab verification against `kernel-verify` (SRV-PIPELINE FR-PIPELINE-9; ADR-0022: one run at a time).

Worker-side, no browser relay: SRV-KERNEL's upstream client. Each run resets the kernel container
(OPS-SANDBOX: exit, a new nonce, `up`), starts one kernel and executes every lab's source, then each assert,
under `lab_exec_timeout_s`. The list runs `lab_runs` times. A lab passes when every assert passes on every
run and its normalised text outputs are identical across runs.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from reader.kernels import upstream
from reader.kernels.upstream import Endpoint, UpstreamError
from reader.pipeline.errors import VerifyUnavailable
from reader.pipeline.findings import Problem, labs_of, labs_sha256, source_sha256, source_text

LAB_EXEC_TIMEOUT_S: Final = 60.0  # bounds.lab_exec_timeout_s
LAB_RUNS: Final = 2  # bounds.lab_runs
VERIFY_JOB_TIMEOUT_S: Final = 3600.0  # bounds.verify_job_timeout_s
RESET_DEADLINE_S: Final = 30.0  # SRV-KERNEL FR-KERNEL-5
POLL_S: Final = 0.25
INTERRUPT_GRACE_S: Final = 5.0
VERIFY_HOST: Final = "kernel-verify"
VERIFY_ENDPOINT: Final = Endpoint(VERIFY_HOST)

log = logging.getLogger("reader.pipeline")


@dataclass(frozen=True)
class LabReport:
    labs_sha256: str
    results: Mapping[str, Mapping[str, Any]]
    problems: list[Problem] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        return {
            "labs_sha256": self.labs_sha256,
            "results": {k: dict(v) for k, v in self.results.items()},
            "problems": [p.as_json() for p in self.problems],
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> LabReport:
        return cls(
            data["labs_sha256"], dict(data["results"]), [Problem.from_json(p) for p in data["problems"]]
        )


def _normalise(text: str) -> str:
    lines = text.replace("\r\n", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).rstrip()


def assert_code(expression: str) -> str:
    """An expression becomes `assert (expr)`; a statement (e.g. `assert x`) runs as written."""
    try:
        ast.parse(expression, mode="eval")
    except SyntaxError:
        return expression
    return f"assert ({expression})"


@dataclass
class _Exec:
    ok: bool
    text: str


class VerifyClient:
    def __init__(
        self,
        endpoint: Endpoint = VERIFY_ENDPOINT,
        *,
        http: Callable[..., Awaitable[Any]] = upstream.http,
        ws: Callable[..., Awaitable[Any]] = upstream.ws_connect,
        exec_timeout_s: float = LAB_EXEC_TIMEOUT_S,
        runs: int = LAB_RUNS,
        budget_s: float = VERIFY_JOB_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.endpoint, self.http, self.ws = endpoint, http, ws
        self.exec_timeout_s, self.runs, self.budget_s, self.clock = exec_timeout_s, runs, budget_s, clock

    async def run(self, notebook: Mapping[str, Any]) -> LabReport:
        labs = labs_of(notebook)
        sha = labs_sha256(notebook)
        if not labs:
            return LabReport(sha, {}, [])
        steps = sum(1 + len(cell["metadata"]["reader"].get("asserts", [])) for _, cell in labs)
        if steps * self.exec_timeout_s * self.runs > self.budget_s:
            text = "The labs need more time than one verification may take; they were not run."
            return LabReport(
                sha, {}, [Problem("labs_unverifiable", None, None, text, "lab_verification", True)]
            )
        passed = {cell["id"]: True for _, cell in labs}
        outputs: dict[str, list[str]] = {cell["id"]: [] for _, cell in labs}
        for _ in range(self.runs):
            await self._one_run(labs, passed, outputs)
        results: dict[str, dict[str, Any]] = {}
        problems: list[Problem] = []
        for _, cell in labs:
            cid = cell["id"]
            identical = len(set(outputs[cid])) <= 1
            ok = passed[cid] and identical
            results[cid] = {
                "runs": self.runs,
                "passed": ok,
                "outputs_identical": identical,
                "input_sha256": source_sha256(cell),
            }
            if not ok:
                text = (
                    "A lab assert failed or the lab timed out." if identical else "The lab's outputs differ."
                )
                problems.append(Problem("lab_failed", cid, None, f"{cid}: {text}", "lab_verification", True))
        log.info("lab_verification labs=%s failed=%s", len(labs), len(problems))
        return LabReport(sha, results, problems)

    async def _one_run(
        self, labs: list[tuple[int, dict[str, Any]]], passed: dict[str, bool], outputs: dict[str, list[str]]
    ) -> None:
        try:
            await self._reset()
            kernel = await self._start_kernel()
            connection = await self.ws(self.endpoint, kernel)
        except UpstreamError:
            raise VerifyUnavailable() from None
        session = uuid.uuid4().hex
        try:
            for _, cell in labs:
                cid = cell["id"]
                result = await self._execute(connection, kernel, session, source_text(cell))
                outputs[cid].append(_normalise(result.text))
                ok = result.ok
                for expression in cell["metadata"]["reader"].get("asserts", []):
                    if not ok:
                        break
                    ok = (await self._execute(connection, kernel, session, assert_code(expression))).ok
                passed[cid] = passed[cid] and ok
        except UpstreamError:
            raise VerifyUnavailable() from None
        finally:
            with contextlib.suppress(Exception):  # the kernel is reset before the next run anyway
                await connection.close()

    async def _state(self) -> dict[str, Any]:
        reply = await self.http("GET", self.endpoint.host, self.endpoint.control_port, "/control/state")
        data = reply.json() if reply.status == 200 else None
        if not isinstance(data, dict) or not isinstance(data.get("nonce"), str):
            raise UpstreamError("bad state")
        return data

    async def _reset(self) -> None:
        """OPS-SANDBOX's reset: POST /control/exit, then wait for a new nonce that answers `up`."""
        before = (await self._state())["nonce"]
        reply = await self.http("POST", self.endpoint.host, self.endpoint.control_port, "/control/exit")
        if reply.status != 202:
            raise UpstreamError("exit refused")
        deadline = self.clock() + RESET_DEADLINE_S
        while self.clock() < deadline:
            try:
                state = await self._state()
                if state["nonce"] != before and state.get("gateway") == "up":
                    return
            except UpstreamError:
                pass
            await asyncio.sleep(POLL_S)
        raise UpstreamError("reset timeout")

    async def _start_kernel(self) -> str:
        reply = await self.http(
            "POST", self.endpoint.host, self.endpoint.gateway_port, "/api/kernels", {"name": "python3"}
        )
        data = reply.json() if reply.status == 201 else None
        kernel = data.get("id") if isinstance(data, dict) else None
        if not isinstance(kernel, str):
            raise UpstreamError("no kernel")
        return kernel

    async def _execute(self, connection: Any, kernel: str, session: str, code: str) -> _Exec:
        msg_id = uuid.uuid4().hex
        await connection.send(
            json.dumps(
                {
                    "channel": "shell",
                    "header": {
                        "msg_id": msg_id,
                        "msg_type": "execute_request",
                        "session": session,
                        "username": "reader",
                        "version": "5.3",
                    },
                    "parent_header": {},
                    "metadata": {},
                    "content": {
                        "code": code,
                        "silent": False,
                        "store_history": False,
                        "user_expressions": {},
                        "allow_stdin": False,
                        "stop_on_error": True,
                    },
                }
            )
        )
        texts: list[str] = []
        reply_status: str | None = None
        idle = False
        try:
            async with asyncio.timeout(self.exec_timeout_s):
                while reply_status is None or not idle:
                    reply_status, idle = self._take(
                        await connection.recv(), msg_id, texts, reply_status, idle
                    )
        except TimeoutError:
            await self._interrupt(connection, kernel, msg_id)
            return _Exec(False, "".join(texts))
        except Exception as error:  # the WebSocket closed or failed
            raise UpstreamError(type(error).__name__) from None
        return _Exec(reply_status == "ok", "".join(texts))

    @staticmethod
    def _take(
        frame: Any, msg_id: str, texts: list[str], status: str | None, idle: bool
    ) -> tuple[str | None, bool]:
        try:
            message = json.loads(frame)
        except TypeError, ValueError:
            return status, idle
        if not isinstance(message, dict) or message.get("parent_header", {}).get("msg_id") != msg_id:
            return status, idle
        kind = message.get("header", {}).get("msg_type")
        content = message.get("content", {}) or {}
        if kind == "stream" and isinstance(content.get("text"), str):
            texts.append(content["text"])
        elif kind == "execute_result":
            plain = content.get("data", {}).get("text/plain")
            if isinstance(plain, str):
                texts.append(plain + "\n")
        elif kind == "execute_reply":
            status = str(content.get("status"))
        elif kind == "status" and content.get("execution_state") == "idle":
            idle = True
        return status, idle

    async def _interrupt(self, connection: Any, kernel: str, msg_id: str) -> None:
        """A timed-out cell: interrupt the kernel and drain its reply, so later labs of this run can run."""
        try:
            await self.http(
                "POST", self.endpoint.host, self.endpoint.gateway_port, f"/api/kernels/{kernel}/interrupt", {}
            )
            async with asyncio.timeout(INTERRUPT_GRACE_S):
                status, idle = None, False
                while status is None or not idle:
                    status, idle = self._take(await connection.recv(), msg_id, [], status, idle)
        except TimeoutError, UpstreamError:
            pass
        except Exception:  # noqa: S110 (a closed connection surfaces on the next execute)
            pass
