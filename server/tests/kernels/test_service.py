# @trace FR-KERNEL-2, FR-KERNEL-3, FR-KERNEL-4, FR-KERNEL-5, FR-KERNEL-6, FR-KERNEL-9, FR-KERNEL-13
# @trace FR-KERNEL-14, INV-KERNEL-1, INV-KERNEL-5, VEC-KERNEL-2
"""SRV-KERNEL sessions and resets against FakeUpstream with a fake clock: create and reuse with the nonce
check, the 4-session limit, delete and interrupt, the reset sequences of VEC-KERNEL-2, first use after api
start, unplanned restarts, upstream failures, and cross-user isolation."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from .conftest import FakeClock, FakeUpstream, advancing_sleep, connect, fixture, load, make_service, settle

pytestmark = pytest.mark.anyio


async def _error(coro: Any) -> Any:
    service = load("reader.kernels.service")
    try:
        await coro
    except service.ServiceError as e:
        return e
    raise AssertionError("no ServiceError")


async def test_first_use_resets_then_creates() -> None:
    up = FakeUpstream()
    svc = make_service(up)
    status, session = await svc.create("ivan", "L02")
    assert status == 201
    assert [c.path for c in up.calls][:2] == ["/control/state", "/control/exit"]
    assert up.requests_to("/api/kernels")[0].body == {"name": "python3"}
    assert len(session.id) == 32
    assert session.kernel_id in up.kernels["kernel-user-ivan"]
    assert session.nonce == up.nonce["kernel-user-ivan"]


async def test_reuse_checks_nonce_every_time() -> None:
    up = FakeUpstream()
    svc = make_service(up)
    _, first = await svc.create("ivan", "L02")
    before = len(up.requests_to("/control/state"))
    status, again = await svc.create("ivan", "L02")
    assert status == 200
    assert again is first
    assert len(up.requests_to("/control/state")) == before + 1
    up.nonce["kernel-user-ivan"] = "f" * 32   # the container restarted on its own
    status, fresh = await svc.create("ivan", "L02")
    assert status == 201
    assert fresh.id != first.id
    assert svc.list("ivan") == [{"session_id": fresh.id, "lesson_id": "L02"}]


async def test_create_validates_lesson_and_kernel_id() -> None:
    up = FakeUpstream()
    svc = make_service(up)
    for bad in ("", "a" * 129, "a/b", None, 5):
        assert (await _error(svc.create("ivan", bad))).status == 400
    up.create_reply = (201, {"id": "../../api/kernels"})
    e = await _error(svc.create("ivan", "L02"))
    assert (e.status, e.code) == (502, "kernel_upstream")
    assert svc.list("ivan") == []


async def test_fifth_session_is_refused_after_the_nonce_check_without_a_gateway_request() -> None:
    up = FakeUpstream()
    svc = make_service(up)
    for i in range(4):
        await svc.create("ivan", f"L{i}")
    n = len(up.calls)
    e = await _error(svc.create("ivan", "L9"))
    assert (e.status, e.code) == (409, "too_many_kernels")
    assert [c.path for c in up.calls[n:]] == ["/control/state"]
    up.nonce["kernel-user-ivan"] = "c" * 32      # the container restarted: its sessions are gone
    status, _ = await svc.create("ivan", "L9")
    assert status == 201


async def test_delete_and_interrupt() -> None:
    up = FakeUpstream()
    svc = make_service(up)
    _, s = await svc.create("ivan", "L02")
    await svc.interrupt("ivan", s.id)
    assert up.requests_to("/interrupt")[0].path == f"/api/kernels/{s.kernel_id}/interrupt"
    await svc.delete("ivan", s.id)
    assert svc.list("ivan") == []
    assert (await _error(svc.delete("ivan", s.id))).status == 404
    _, s2 = await svc.create("ivan", "L03")
    up.kernels["kernel-user-ivan"].clear()        # the gateway lost the kernel: DELETE answers 404
    await svc.delete("ivan", s2.id)
    assert svc.list("ivan") == []


async def test_unknown_and_foreign_sessions_look_the_same() -> None:
    up = FakeUpstream()
    svc = make_service(up)
    _, anna = await svc.create("anna", "L02")
    a = await _error(svc.interrupt("ivan", anna.id))
    b = await _error(svc.interrupt("ivan", "0" * 32))
    assert (a.status, a.code) == (b.status, b.code) == (404, "not_found")
    assert svc.list("ivan") == []


@pytest.mark.parametrize("case_id", ["changes", "same-nonce-up", "deadline", "new-nonce-not-up-yet",
                                     "state-unreachable", "exit-refused", "recovery-after-failure"])
async def test_reset_sequences(case_id: str) -> None:
    case = next(c for c in fixture("reset.yaml")["resets"] if c["id"] == case_id)
    host = "kernel-user-ivan"
    clock = FakeClock()
    up = FakeUpstream()
    svc = make_service(up, clock, advancing_sleep(clock))
    st_ = svc.user("ivan")
    st_.started = True
    if case.get("precondition", {}).get("service_usable") is False:
        st_.usable = False
    before = case["before"]
    script: list[Any] = ["refused" if before == "connection_refused" else before]
    for item in case.get("polls", []):
        if isinstance(item, dict):
            script.append(item)
        elif item == "connection_refused":
            script.append("refused")
        else:  # a text step "... until 30 s": repeat the last concrete answer past the deadline
            last = script[-1] if len(script) > 1 else before
            if "connection_refused" in item:
                last = "refused"
            elif "gateway: up" in item:
                last = {"nonce": before["nonce"], "gateway": "up"}
            script.extend([last] * 200)
    up.states[host] = script
    up.exit_status = case.get("exit", 202)
    expect = case["expect"]
    t0 = clock()
    if expect["result"] == "ok":
        await svc.reset("ivan")
        assert st_.usable is True
        if "recorded_nonce" in expect:
            assert st_.nonce == expect["recorded_nonce"]
    else:
        e = await _error(svc.reset("ivan"))
        assert (e.status, e.code) == (503, "kernel_unavailable")
        assert st_.usable is False
        if expect.get("fails_at") == "30 s after step 2":
            assert 30.0 <= clock() - t0 <= 31.0
        if expect.get("exit_called") is False:
            assert up.requests_to("/control/exit") == []


async def test_reset_drops_sessions_at_step_two_and_blocks_creation_until_confirmed() -> None:
    clock = FakeClock()
    up = FakeUpstream()
    svc = make_service(up, clock, advancing_sleep(clock))
    _, s = await svc.create("ivan", "L02")
    run = await connect(svc, up, s)
    host = "kernel-user-ivan"
    up.states[host] = [{"nonce": up.nonce[host], "gateway": "up"}, "refused", "refused",
                       {"nonce": "a" * 32, "gateway": "up"}]
    reset = asyncio.ensure_future(svc.reset("ivan"))
    await settle()
    assert ("kernel_reset", None) in run.browser.notices()
    assert run.browser.close_code == 1012
    await reset
    assert svc.list("ivan") == []
    assert svc.user("ivan").nonce == "a" * 32


async def test_reset_is_coalesced() -> None:
    clock = FakeClock()
    up = FakeUpstream()
    svc = make_service(up, clock, advancing_sleep(clock))
    svc.user("ivan").started = True
    await asyncio.gather(svc.reset("ivan"), svc.reset("ivan"))
    assert len(up.requests_to("/control/exit")) == 1


async def test_unusable_service_refuses_until_explicit_reset() -> None:
    up = FakeUpstream()
    svc = make_service(up)
    up.exit_status = 500
    assert (await _error(svc.create("ivan", "L02"))).code == "kernel_unavailable"
    up.exit_status = 202
    assert (await _error(svc.create("ivan", "L02"))).code == "kernel_unavailable"
    assert up.requests_to("/api/kernels") == []
    await svc.reset("ivan")
    status, _ = await svc.create("ivan", "L02")
    assert status == 201


async def test_unresolvable_service_is_unavailable_and_failures_are_upstream_errors() -> None:
    up = FakeUpstream()
    svc = make_service(up)
    up.unresolvable.add("kernel-user-ivan")
    e = await _error(svc.create("ivan", "L02"))
    assert (e.status, e.code) == (503, "kernel_unavailable")
    up2 = FakeUpstream()
    svc2 = make_service(up2)
    await svc2.create("ivan", "L01")
    up2.fail.add("/api/kernels")
    e = await _error(svc2.create("ivan", "L02"))
    assert (e.status, e.code) == (502, "kernel_upstream")
    assert [s["lesson_id"] for s in svc2.list("ivan")] == ["L01"]


@pytest.mark.parametrize("case_id", ["container-restarted", "exiting-same-nonce", "kernel-died",
                                     "state-unreadable"])
async def test_unplanned_restarts(case_id: str) -> None:
    case = next(c for c in fixture("reset.yaml")["unplanned"] if c["id"] == case_id)
    up = FakeUpstream()
    svc = make_service(up)
    host = "kernel-user-ivan"
    _, s1 = await svc.create("ivan", "L01")
    _, s2 = await svc.create("ivan", "L02")
    r1 = await connect(svc, up, s1)
    r2 = await connect(svc, up, s2)
    recorded = svc.user("ivan").nonce
    state = case["state"]
    if state == "connection_refused":
        up.states[host] = ["refused"]
    elif state["nonce"] == case["recorded_nonce"]:
        up.states[host] = [{"nonce": recorded, "gateway": state["gateway"]}]
    else:
        up.states[host] = [{"nonce": "9" * 32, "gateway": state["gateway"]}]
    r1.upstream.push(None)        # the upstream WebSocket of session 1 ends
    await settle(40)
    expect = case["expect"]
    code = expect["notice"]
    assert (code, None) in r1.browser.notices()
    assert r1.browser.close_code == expect["close_code"]
    if expect["dropped"] == "all_sessions_of_user":
        assert svc.list("ivan") == []
        assert r2.browser.close_code == expect["close_code"]
    else:
        assert [x["session_id"] for x in svc.list("ivan")] == [s2.id]
        assert r2.browser.close_code is None
        await settle(10)
        assert any(c.method == "DELETE" and c.path.endswith(s1.kernel_id) for c in up.calls)
    if case_id == "container-restarted":
        assert svc.user("ivan").nonce == "9" * 32


async def test_interrupt_404_runs_unplanned_restart_handling() -> None:
    up = FakeUpstream()
    svc = make_service(up)
    _, s = await svc.create("ivan", "L02")
    up.kernels["kernel-user-ivan"].clear()
    e = await _error(svc.interrupt("ivan", s.id))
    assert e.status == 404
    assert svc.list("ivan") == []


async def test_delete_404_runs_the_state_read_and_removes_the_session() -> None:
    up = FakeUpstream()
    svc = make_service(up)
    _, s1 = await svc.create("ivan", "L01")
    _, s2 = await svc.create("ivan", "L02")
    up.kernels["kernel-user-ivan"].discard(s1.kernel_id)
    up.nonce["kernel-user-ivan"] = "b" * 32          # and the container restarted
    before = len(up.requests_to("/control/state"))
    await svc.delete("ivan", s1.id)
    assert len(up.requests_to("/control/state")) == before + 1
    assert svc.list("ivan") == []      # FR-KERNEL-14: a new nonce drops all of the user's sessions
    assert s2.hub is None


async def test_first_use_reset_coalesces_with_an_explicit_reset() -> None:
    clock = FakeClock()
    up = FakeUpstream()
    svc = make_service(up, clock, advancing_sleep(clock))
    await asyncio.gather(svc.create("ivan", "L01"), svc.reset("ivan"))
    assert len(up.requests_to("/control/exit")) == 1


async def test_reset_deadline_is_a_whole_even_when_a_state_read_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = load("reader.kernels.service")
    monkeypatch.setattr(service, "RESET_DEADLINE_S", 0.3)
    up = FakeUpstream()
    calls = {"n": 0}
    real = up.http

    async def hanging(method: str, host: str, port: int, path: str, body: Any = None) -> Any:
        if path == "/control/state":
            calls["n"] += 1
            if calls["n"] > 1:            # every read after the exit hangs
                await asyncio.sleep(3600)
        return await real(method, host, port, path, body)

    svc = service.KernelService(up.endpoint, http=hanging, ws=up.ws)
    svc.user("ivan").started = True
    started = asyncio.get_running_loop().time()
    e = await _error(svc.reset("ivan"))
    assert e.code == "kernel_unavailable"
    assert asyncio.get_running_loop().time() - started < 2.0


async def test_no_session_outlives_its_nonce() -> None:
    up = FakeUpstream()
    svc = make_service(up)
    _, s = await svc.create("ivan", "L02")
    up.nonce["kernel-user-ivan"] = "e" * 32
    up.gateway["kernel-user-ivan"] = "exiting"
    up.states["kernel-user-ivan"] = [{"nonce": "e" * 32, "gateway": "exiting"}] * 3 + [
        {"nonce": "d" * 32, "gateway": "up"}
    ]
    status, s2 = await svc.create("ivan", "L02")
    assert status == 201
    assert s2.id != s.id
    assert s2.nonce == "d" * 32
    assert all(x["session_id"] != s.id for x in svc.list("ivan"))


async def test_per_user_lock_does_not_block_other_users() -> None:
    clock = FakeClock()
    up = FakeUpstream()
    svc = make_service(up, clock, advancing_sleep(clock))
    await svc.create("anna", "L01")
    await svc.user("ivan").lock.acquire()
    try:
        status, _ = await asyncio.wait_for(svc.create("anna", "L02"), 2)
        assert status == 201
    finally:
        svc.user("ivan").lock.release()


handles = st.sampled_from(["ivan", "anna", "pair-1", "verify"])
ops = st.lists(
    st.tuples(st.sampled_from(["create", "delete", "interrupt", "list", "reset"]), handles,
              st.sampled_from(["L1", "L2"]), st.integers(0, 3)),
    max_size=12,
)


@settings(max_examples=40, deadline=None)
@given(actor=handles, sequence=ops)
def test_requests_reach_only_the_actors_service(
    actor: str, sequence: list[tuple[str, str, str, int]]
) -> None:
    """INV-KERNEL-1: every upstream request made for the actor goes to kernel-user-<actor>."""

    async def run() -> None:
        service = load("reader.kernels.service")
        clock = FakeClock()
        up = FakeUpstream()
        svc = make_service(up, clock, advancing_sleep(clock))
        others = {h for _, h, _, _ in sequence} - {actor}
        for h in sorted(others):
            await svc.create(h, "L0")
        foreign = [s.id for h in others for s in svc.user(h).sessions.values()]
        up.calls.clear()
        for op, _, lesson, idx in sequence:
            mine = list(svc.user(actor).sessions)
            target = (mine + foreign)[idx % len(mine + foreign)] if mine + foreign else "0" * 32
            try:
                if op == "create":
                    await svc.create(actor, lesson)
                elif op == "delete":
                    await svc.delete(actor, target)
                elif op == "interrupt":
                    await svc.interrupt(actor, target)
                elif op == "reset":
                    await svc.reset(actor)
                else:
                    listed = svc.list(actor)
                    assert all(x["session_id"] not in foreign for x in listed)
            except service.ServiceError:
                pass
        assert {c.host for c in up.calls} <= {f"kernel-user-{actor}"}

    asyncio.run(run())
