# @trace FR-KERNEL-7, FR-KERNEL-8, FR-KERNEL-10, FR-KERNEL-11, FR-KERNEL-12, INV-KERNEL-3, INV-KERNEL-4
# @trace INV-KERNEL-6
"""SRV-KERNEL relay against an in-memory kernel WebSocket and fake browsers, with a fake clock: forwarding
and filtering, one execute in flight with a queue of 8, the 60 s timeout and 5 s escalation, kernel restarts
freeing the slot, size caps, the connection limit, the session re-check and what the logs carry."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest

from .conftest import (
    FakeClock,
    FakeUpstream,
    closed_with,
    connect,
    execute,
    kernel_msg,
    load,
    make_service,
    settle,
    tick_sleep,
)

pytestmark = pytest.mark.anyio
MiB = 1024 * 1024


async def _running(clock: FakeClock | None = None, recheck: Any = None) -> Any:
    clock = clock or FakeClock()
    up = FakeUpstream()
    svc = make_service(up, clock, tick_sleep)
    _, session = await svc.create("ivan", "L02")
    run = await connect(svc, up, session, recheck)
    return up, svc, clock, run


def _reply(run: Any, msg_id: str) -> None:
    run.upstream.push(kernel_msg("iopub", "status", msg_id, {"execution_state": "busy"}))
    run.upstream.push(kernel_msg("iopub", "stream", msg_id, {"name": "stdout", "text": "1\n"}))
    run.upstream.push(kernel_msg("shell", "execute_reply", msg_id, {"status": "ok", "execution_count": 1}))
    run.upstream.push(kernel_msg("iopub", "status", msg_id, {"execution_state": "idle"}))


async def test_forward_rewrite_and_filter() -> None:
    _, _, _, run = await _running()
    run.browser.send(execute("m1"))
    await settle()
    sent = run.upstream.sent[0]
    assert set(sent) == {"channel", "header", "parent_header", "metadata", "content"}
    assert sent["header"]["session"] == run.session.id
    assert sent["header"]["username"] == "ivan"
    assert sent["content"]["allow_stdin"] is False
    _reply(run, "m1")
    run.upstream.push(kernel_msg("iopub", "comm_open", "m1", {"comm_id": "c"}))
    run.upstream.push(kernel_msg("stdin", "input_request", "m1", {"prompt": ""}))
    run.upstream.push(b"\x00binary display_data buffers")
    run.upstream.push("not json")
    await settle()
    assert run.browser.types() == ["status", "stream", "execute_reply", "status"]
    assert all(set(m) == {"channel", "header", "parent_header", "metadata", "content"}
               for m in run.browser.received)


async def test_rejected_and_binary_browser_frames() -> None:
    _, _, _, run = await _running()
    run.browser.send({"channel": "control", "header": {"msg_id": "c1", "msg_type": "shutdown_request"},
                      "parent_header": {}, "metadata": {}, "content": {}})
    run.browser.send("garbage")
    await settle()
    assert run.browser.notices() == [("rejected", "c1"), ("rejected", None)]
    assert run.upstream.sent == []
    run.browser.send(b"\x01\x02")
    await settle()
    assert run.browser.close_code == 1003


async def test_browser_frame_over_one_mib_closes_1009() -> None:
    _, _, _, run = await _running()
    run.browser.send(execute("big", code="x" * (MiB + 10)))
    await settle()
    assert run.browser.close_code == 1009
    assert run.upstream.sent == []


async def test_one_in_flight_queue_of_eight_and_busy() -> None:
    _, _, _, run = await _running()
    for i in range(10):
        run.browser.send(execute(f"m{i}"))
    await settle()
    assert [m["header"]["msg_id"] for m in run.upstream.sent] == ["m0"]
    assert run.browser.notices() == [("busy", "m9")]
    _reply(run, "m0")
    await settle()
    assert [m["header"]["msg_id"] for m in run.upstream.sent] == ["m0", "m1"]
    run.browser.send(execute("m1"))     # a duplicate of the in-flight id
    await settle()
    assert ("rejected", "m1") in run.browser.notices()


async def test_pending_non_execute_cap() -> None:
    _, _, _, run = await _running()
    for i in range(33):
        header = {"msg_id": f"k{i}", "msg_type": "kernel_info_request"}
        frame = {"channel": "shell", "header": header, "parent_header": {}, "metadata": {}, "content": {}}
        run.browser.send(frame)
    await settle()
    assert len(run.upstream.sent) == 32
    assert run.browser.notices() == [("busy", "k32")]


async def test_timeout_counts_execution_not_queueing_and_escalates() -> None:
    up, _, clock, run = await _running()
    run.browser.send(execute("a"))
    run.browser.send(execute("b"))
    await settle()
    clock.advance(59.0)
    await settle()
    assert run.browser.notices() == []
    _reply(run, "a")
    await settle()
    clock.advance(59.0)        # b was forwarded just now: 59 s of its own
    await settle()
    assert ("timeout", "b") not in run.browser.notices()
    clock.advance(1.5)
    await settle()
    assert ("timeout", "b") in run.browser.notices()
    assert up.requests_to("/interrupt")
    clock.advance(5.1)
    await settle(40)
    assert len(up.requests_to("/control/exit")) == 2     # first use + escalation
    assert run.browser.close_code == 1012


async def test_kernel_restarted_frees_slot_and_drops_queue() -> None:
    _, _, _, run = await _running()
    run.browser.send(execute("a"))
    run.browser.send(execute("b"))
    run.browser.send(execute("c"))
    await settle()
    run.upstream.push(kernel_msg("iopub", "status", None, {"execution_state": "restarting"}))
    await settle()
    assert ("kernel_restarted", None) in run.browser.notices()
    run.browser.send(execute("d"))
    await settle()
    assert [m["header"]["msg_id"] for m in run.upstream.sent] == ["a", "d"]
    run.upstream.push(kernel_msg("iopub", "status", None, {"execution_state": "dead"}))
    await settle()
    assert ("kernel_dead", None) in run.browser.notices()
    run.browser.send(execute("e"))
    await settle()
    assert run.upstream.sent[-1]["header"]["msg_id"] == "e"


async def test_output_budget_per_execution() -> None:
    _, _, _, run = await _running()
    run.browser.send(execute("a"))
    await settle()
    chunk = "y" * (3 * MiB)
    for _ in range(7):
        run.upstream.push(kernel_msg("iopub", "stream", "a", {"name": "stdout", "text": chunk}))
    run.upstream.push(kernel_msg("shell", "execute_reply", "a", {"status": "ok"}))
    await settle(60)
    streams = [m for m in run.browser.received if m["header"].get("msg_type") == "stream"]
    assert sum(len(json.dumps(m)) for m in streams) <= 16 * MiB
    assert run.browser.notices().count(("output_truncated", "a")) == 1
    assert "execute_reply" in run.browser.types()


async def test_oversized_upstream_message_drops_session() -> None:
    _, svc, _, run = await _running()
    run.browser.send(execute("a"))
    await settle()
    run.upstream.push(closed_with(1009))
    await settle(40)
    assert ("output_truncated", "a") in run.browser.notices()
    assert ("kernel_dead", None) in run.browser.notices()
    assert svc.list("ivan") == []


async def test_slow_browser_buffer_limit_closes_only_that_connection() -> None:
    up, svc, _, run = await _running()
    other = await connect(svc, up, run.session)

    async def stuck(_: str) -> None:
        await asyncio.sleep(3600)

    run.browser.send_text = stuck  # type: ignore[method-assign]
    run.browser.send(execute("a"))
    await settle()
    for _ in range(6):
        run.upstream.push(kernel_msg("iopub", "stream", "a", {"name": "stdout", "text": "z" * (2 * MiB)}))
    for i in range(6):
        run.upstream.push(kernel_msg("iopub", "display_data", None, {"data": {"text/plain": "q" * (2 * MiB)},
                                                                      "i": i}))
    await settle(80)
    assert run.browser.close_code == 1013
    assert other.browser.close_code is None
    assert svc.list("ivan")


async def test_session_recheck_closes_4401() -> None:
    valid = {"ok": True}

    async def recheck() -> bool:
        return valid["ok"]

    _, _, clock, run = await _running(recheck=recheck)
    clock.advance(61)
    await settle()
    assert run.browser.close_code is None
    valid["ok"] = False
    clock.advance(61)
    await settle()
    assert run.browser.close_code == 4401


async def test_upstream_connect_uses_the_handles_service_only() -> None:
    up, _, _, _ = await _running()
    assert up.ws_hosts == ["kernel-user-ivan"]
    upstream = load("reader.kernels.upstream")
    assert upstream.HTTP_TIMEOUT_S == 10.0


async def test_logs_carry_no_code_or_output(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    _, _, _, run = await _running()
    run.browser.send(execute("a", code="print('CANARY-CODE-7')"))
    run.browser.send({"channel": "control", "header": {"msg_id": "x", "msg_type": "debug_request"},
                      "parent_header": {}, "metadata": {}, "content": {"secret": "CANARY-CODE-7"}})
    await settle()
    run.upstream.push(kernel_msg("iopub", "stream", "a", {"name": "stdout", "text": "CANARY-OUT-9"}))
    run.upstream.push(kernel_msg("iopub", "comm_msg", "a", {"data": "CANARY-OUT-9"}))
    run.upstream.push(closed_with(1009))
    await settle(40)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "CANARY-CODE-7" not in text
    assert "CANARY-OUT-9" not in text
