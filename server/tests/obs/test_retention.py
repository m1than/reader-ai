"""SRV-OBS [unit]: the retention job — next day first, one delete call with the cutoff, the disabled path.

@trace FR-OBS-11
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from .conftest import require

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_kind_declaration() -> None:
    jobs = require("reader.obs.jobs")
    (kind,) = jobs.JOB_KINDS
    assert kind.name == "obs.retention"
    assert kind.key == ("day",)
    assert kind.max_attempts == 3
    assert kind.timeout_s == 60
    assert httpx.TransportError in kind.transient
    assert kind.args.model_validate({"day": "2026-11-24"}).day == "2026-11-24"
    for bad in ("2026-13-01", "yesterday", "2026-11-24T00:00"):
        with pytest.raises(ValueError):
            kind.args.model_validate({"day": bad})


@pytest.mark.parametrize(
    ("day", "nxt"), [("2026-11-24", "2026-11-25"), ("2026-12-31", "2027-01-01"), ("2028-02-28", "2028-02-29")]
)
def test_next_day_is_the_argument_plus_one(day: str, nxt: str) -> None:
    assert require("reader.obs.jobs").next_day(day) == nxt


def test_schedule_and_cutoff() -> None:
    jobs = require("reader.obs.jobs")
    assert jobs.seconds_until("2026-11-25", NOW) == 15 * 3600  # to 03:00 UTC of that day
    assert jobs.seconds_until("2026-11-24", NOW) == 0  # already past
    assert jobs.cutoff_ms(NOW, 30) == int(NOW.timestamp() * 1000) - 30 * 86_400_000


class FakeJobs:
    def __init__(self, log: list[str], fail: bool | int = False) -> None:
        """`fail`: True fails every enqueue; an int n fails the first n."""
        self.log, self.fail = log, fail
        self.calls: list[tuple[str, dict[str, Any], int]] = []

    async def enqueue(self, kind: str, args: dict[str, Any], schedule_in: int = 0) -> Any:
        self.log.append("enqueue")
        self.calls.append((kind, dict(args), schedule_in))
        if self.fail is True or (type(self.fail) is int and len(self.calls) <= self.fail):
            raise RuntimeError("queue down")


def _client(
    log: list[str],
    status: int = 200,
    bodies: list[dict[str, Any]] | None = None,
    exc: Exception | None = None,
    content: bytes | None = None,
) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        log.append("delete")
        if exc is not None:
            raise exc
        if bodies is not None:
            bodies.append(
                {
                    "url": str(request.url),
                    "body": json.loads(request.content),
                    "ctype": request.headers.get("content-type"),
                }
            )
        if content is not None:
            return httpx.Response(status, content=content)
        return httpx.Response(status, json={"traces_deleted": 3})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_next_day_first_then_one_delete() -> None:
    jobs = require("reader.obs.jobs")
    log: list[str] = []
    bodies: list[dict[str, Any]] = []
    fake = FakeJobs(log)
    async with _client(log, bodies=bodies) as client:
        deleted = await jobs.retain(
            "2026-11-24", jobs=fake, enabled=True, retention_days=30, client=client, now=NOW
        )
    assert log == ["enqueue", "delete"]
    assert fake.calls == [("obs.retention", {"day": "2026-11-25"}, 15 * 3600)]
    assert deleted == 3
    (call,) = bodies
    assert call["url"] == "http://mlflow:5000/api/2.0/mlflow/traces/delete-traces"
    assert call["ctype"] == "application/json"
    assert call["body"] == {"experiment_id": "0", "max_timestamp_millis": jobs.cutoff_ms(NOW, 30)}


async def test_disabled_enqueues_the_next_day_and_deletes_nothing() -> None:
    jobs = require("reader.obs.jobs")
    log: list[str] = []
    async with _client(log) as client:
        assert (
            await jobs.retain(
                "2026-11-24", jobs=FakeJobs(log), enabled=False, retention_days=30, client=client, now=NOW
            )
            == 0
        )
    assert log == ["enqueue"]


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(require("reader.obs.jobs"), "ENQUEUE_PAUSE_S", 0.0)


async def test_a_failed_next_day_enqueue_is_retried_then_still_deletes() -> None:
    jobs = require("reader.obs.jobs")
    assert jobs.ENQUEUE_TRIES == 3
    log: list[str] = []
    fake = FakeJobs(log, fail=1)
    async with _client(log) as client:
        await jobs.retain("2026-11-24", jobs=fake, enabled=True, retention_days=30, client=client, now=NOW)
    assert log == ["enqueue", "enqueue", "delete"]
    log.clear()
    async with _client(log) as client:
        await jobs.retain(
            "2026-11-24",
            jobs=FakeJobs(log, fail=True),
            enabled=True,
            retention_days=30,
            client=client,
            now=NOW,
        )
    assert log == ["enqueue", "enqueue", "enqueue", "delete"]  # three tries, then today's delete anyway


async def test_a_late_run_enqueues_the_argument_day_plus_one_at_once() -> None:
    """Day 2026-11-20 run on 2026-11-24 (round 1 survivor): the next key is 2026-11-21, due now."""
    jobs = require("reader.obs.jobs")
    log: list[str] = []
    fake = FakeJobs(log)
    async with _client(log) as client:
        await jobs.retain("2026-11-20", jobs=fake, enabled=True, retention_days=30, client=client, now=NOW)
    assert fake.calls == [("obs.retention", {"day": "2026-11-21"}, 0)]


@pytest.mark.parametrize(
    ("status", "content"),
    [
        (400, b'{"error_code": "INVALID_PARAMETER_VALUE"}'),
        (404, b""),
        (200, b"<html>not json</html>"),
        (200, b"[]"),
        (200, b'{"traces_deleted": "3"}'),
        (200, b'{"traces_deleted": true}'),
    ],
)
async def test_a_4xx_or_a_bad_200_is_retention_failed(status: int, content: bytes) -> None:
    jobs = require("reader.obs.jobs")
    log: list[str] = []
    async with _client(log, status=status, content=content) as client:
        with pytest.raises(jobs.RetentionFailed):
            await jobs.retain(
                "2026-11-24", jobs=FakeJobs(log), enabled=True, retention_days=30, client=client, now=NOW
            )


async def test_an_empty_200_object_means_zero_deleted() -> None:
    jobs = require("reader.obs.jobs")
    log: list[str] = []
    async with _client(log, content=b"{}") as client:
        count = await jobs.retain(
            "2026-11-24", jobs=FakeJobs(log), enabled=True, retention_days=30, client=client, now=NOW
        )
    assert count == 0


async def test_unreachable_is_transient_and_other_failures_are_coded() -> None:
    jobs = require("reader.obs.jobs")
    log: list[str] = []
    async with _client(log, exc=httpx.ConnectError("no route")) as client:
        with pytest.raises(httpx.TransportError):
            await jobs.retain(
                "2026-11-24", jobs=FakeJobs(log), enabled=True, retention_days=30, client=client, now=NOW
            )
    async with _client(log, status=500) as client:
        with pytest.raises(jobs.RetentionFailed) as raised:
            await jobs.retain(
                "2026-11-24", jobs=FakeJobs(log), enabled=True, retention_days=30, client=client, now=NOW
            )
    # FR-EVENTS-6 records a permanent failure by its class name: the key ends dead with `RetentionFailed`.
    assert type(raised.value).__name__ == "RetentionFailed"
    assert not issubclass(jobs.RetentionFailed, httpx.TransportError)


async def test_on_worker_start_enqueues_today() -> None:
    jobs = require("reader.obs.jobs")
    log: list[str] = []
    fake = FakeJobs(log)
    await jobs.on_worker_start(fake, now=NOW)
    assert fake.calls == [("obs.retention", {"day": "2026-11-24"}, 0)]


@pytest.mark.parametrize(("enabled", "days"), [(True, 7), (False, 365)])
async def test_handle_takes_the_current_settings(
    enabled: bool, days: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 2: the job reads `enabled` and `retention_days` from setup's current settings."""
    import reader.jobs
    from reader.obs import setup

    jobs = require("reader.obs.jobs")
    seen: list[dict[str, Any]] = []
    queue = FakeJobs([])

    async def recording(day: str, **kwargs: Any) -> int:
        seen.append({"day": day, **kwargs})
        return 0

    monkeypatch.setattr(setup, "current_settings", lambda: setup.Settings(enabled, days))
    monkeypatch.setattr(reader.jobs, "running", lambda: queue)
    monkeypatch.setattr(jobs, "retain", recording)
    await jobs.handle(jobs.RetentionArgs(day="2026-11-24"), None, None)
    ((call),) = seen
    assert (call["day"], call["enabled"], call["retention_days"], call["jobs"]) == (
        "2026-11-24",
        enabled,
        days,
        queue,
    )
