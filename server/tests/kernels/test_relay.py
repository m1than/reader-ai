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
from hypothesis import given, settings
from hypothesis import strategies as st

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
    for i in range(300):   # canaries in channel and msg_type, each distinct
        run.upstream.push(json.dumps({"channel": f"CANARY-CH-{i}\nforged", "header": {"msg_type": "x"}}))
        run.upstream.push(json.dumps({"channel": "iopub", "header": {"msg_type": f"CANARY-MT-{i}"}}))
    await settle(80)
    assert len(run.hub.dropped) <= 10        # closed-set labels: the counter stays bounded
    run.upstream.push(closed_with(1009))
    await settle(40)
    text = "\n".join(r.getMessage() for r in caplog.records)
    for canary in ("CANARY-CODE-7", "CANARY-OUT-9", "CANARY-CH", "CANARY-MT", "forged"):
        assert canary not in text


POISON: list[Any] = [
    {"channel": "iopub", "header": {"msg_type": {}}},
    {"channel": "shell", "header": {"msg_type": []}},
    {"channel": "iopub", "header": {"msg_type": ["status"]}},
    {"channel": {"x": 1}, "header": {"msg_type": "stream"}},
    {"channel": "iopub", "header": {"msg_type": "stream"}, "content": {"a": "[" * 1}},
]


def _deep(depth: int) -> str:
    head = '{"channel":"iopub","header":{"msg_type":"stream"},"content":{"a":'
    return head + "[" * depth + "]" * depth + "}}"


async def test_poison_and_deep_kernel_frames_are_dropped_and_the_relay_survives() -> None:
    _, svc, _, run = await _running()
    run.browser.send(execute("a"))
    await settle()
    for frame in POISON:
        run.upstream.push(json.dumps(frame))
    for depth in (500, 900, 1500, 5000, 50000, 200000):
        run.upstream.push(_deep(depth))
    run.upstream.push(kernel_msg("iopub", "stream", "a", {"name": "stdout", "text": "after"}))
    run.upstream.push(kernel_msg("shell", "execute_reply", "a", {"status": "ok"}))
    await settle(60)
    assert not run.hub.tasks[0].done()
    assert "execute_reply" in run.browser.types()
    iopub = [m for m in run.browser.received if m.get("channel") == "iopub"]
    assert any(m["content"].get("text") == "after" for m in iopub)
    assert svc.list("ivan")


async def test_poison_browser_frames_are_rejected_and_the_connection_survives() -> None:
    _, _, _, run = await _running()
    poison = [
        {"channel": "shell", "header": {"msg_id": "p1", "msg_type": []}, "parent_header": {}, "content": {}},
        {"channel": "shell", "header": {"msg_id": "p2", "msg_type": {}}, "parent_header": {}, "content": {}},
        {"channel": [], "header": {"msg_id": "p3", "msg_type": "execute_request"}, "parent_header": {}},
    ]
    for frame in poison:
        run.browser.send(frame)
    run.browser.send('{"a":' + "[" * 100000 + "]" * 100000 + "}")
    run.browser.send(execute("ok"))
    await settle(40)
    assert not run.task.done()
    assert run.browser.close_code is None
    assert [n for n in run.browser.notices() if n[0] == "rejected"][:3] == [
        ("rejected", "p1"), ("rejected", "p2"), ("rejected", "p3")]
    assert [m["header"]["msg_id"] for m in run.upstream.sent] == ["ok"]


async def test_timeout_survives_the_last_browser_disconnecting() -> None:
    up, svc, clock, run = await _running()
    run.browser.send(execute("a", code="while True: pass"))
    run.browser.send(execute("queued"))
    await settle()
    run.browser.disconnect()
    await settle(30)
    assert not run.hub.closing
    assert run.hub.queue == type(run.hub.queue)()
    clock.advance(60.5)
    await settle(30)
    assert len(up.requests_to("/interrupt")) == 1
    clock.advance(5.5)
    await settle(60)
    assert len(up.requests_to("/control/exit")) == 2      # first use + escalation
    assert svc.list("ivan") == []


async def test_idle_hub_ends_after_the_reply_once_alone() -> None:
    _, _, _, run = await _running()
    run.browser.send(execute("a"))
    await settle()
    run.browser.disconnect()
    await settle(20)
    assert not run.hub.closing
    _reply_only(run, "a")
    await settle(20)
    assert run.hub.closing
    assert run.session.hub is None


def _reply_only(run: Any, msg_id: str) -> None:
    run.upstream.push(kernel_msg("shell", "execute_reply", msg_id, {"status": "ok"}))


async def test_budgets_are_released_per_execute() -> None:
    _, _, _, run = await _running()
    for i in range(50):
        run.browser.send(execute(f"m{i}"))
        await settle(2)
        _reply_only(run, f"m{i}")
        await settle(2)
    assert run.hub.output == {}
    assert run.hub.truncated == set()


async def test_start_refuses_on_a_closing_hub() -> None:
    relay = load("reader.kernels.relay")
    up = FakeUpstream()
    svc = make_service(up, FakeClock(), tick_sleep)
    _, s = await svc.create("ivan", "L03")
    hub = relay.Hub(svc, s)
    s.hub = hub
    svc.drop_all("ivan", "kernel_reset", 1012)
    assert await hub.start() is False
    assert up.ws_hosts == []


any_json = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=12),
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=8), inner, max_size=3),
    max_leaves=12,
)
kernel_frames = st.one_of(
    any_json.map(json.dumps),
    st.builds(lambda ch, mt, c: json.dumps({"channel": ch, "header": {"msg_type": mt}, "content": c}),
              st.one_of(st.sampled_from(["shell", "iopub", "stdin", "control"]), any_json),
              st.one_of(st.sampled_from(["stream", "status", "execute_reply", "comm_open"]), any_json),
              any_json),
    st.binary(max_size=20),
)
browser_frames = st.one_of(
    any_json.map(json.dumps),
    st.builds(lambda ch, mt, mid, c: json.dumps({"channel": ch, "header": {"msg_id": mid, "msg_type": mt},
                                                 "parent_header": {}, "metadata": {}, "content": c}),
              st.one_of(st.just("shell"), any_json),
              st.one_of(st.sampled_from(["execute_request", "kernel_info_request", "comm_open"]), any_json),
              st.one_of(st.text(min_size=1, max_size=10), any_json),
              any_json),
)
KEYS = {"channel", "header", "parent_header", "metadata", "content"}
ALLOWED_OUT = {
    ("shell", t) for t in ("execute_reply", "kernel_info_reply", "complete_reply", "inspect_reply",
                           "is_complete_reply")
} | {("iopub", t) for t in ("status", "execute_input", "stream", "display_data", "update_display_data",
                            "execute_result", "error", "clear_output")}


@settings(max_examples=60, deadline=None)
@given(from_browser=st.lists(browser_frames, max_size=12), from_kernel=st.lists(kernel_frames, max_size=12))
def test_frame_sequences_through_the_relay(from_browser: list[str], from_kernel: list[Any]) -> None:
    """FR-KERNEL-8, INV-KERNEL-2/3: arbitrary frames in both directions never end the relay, and only rebuilt,
    allow-listed messages cross it."""

    async def run_case() -> None:
        _, svc, _, run = await _running()
        for frame in from_browser:
            run.browser.send(frame)
        for frame in from_kernel:
            run.upstream.push(frame)
        await settle(30)
        assert not run.task.done()
        assert not run.hub.tasks[0].done()
        assert run.browser.close_code is None
        for msg in run.upstream.sent:
            assert set(msg) == KEYS
            assert msg["channel"] == "shell"
            assert msg["header"]["username"] == "ivan"
            assert msg["header"]["session"] == run.session.id
        for msg in run.browser.received:
            if msg["channel"] == "reader":
                continue
            assert set(msg) == KEYS
            assert (msg["channel"], msg["header"]["msg_type"]) in ALLOWED_OUT
        run.browser.disconnect()
        await settle(10)
        assert svc.list("ivan")

    asyncio.run(run_case())
