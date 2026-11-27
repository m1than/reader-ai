"""SRV-TUTOR [db]: the `tutor.answer` handler with a fake search and Provider, two stalled calls through the real
SRV-AGENT adapter, the routes, isolation, stale rows, spend bounds and logs.

@trace FR-TUTOR-1 FR-TUTOR-2 FR-TUTOR-3 FR-TUTOR-4 FR-TUTOR-5 FR-TUTOR-7 FR-TUTOR-8 FR-TUTOR-9 FR-TUTOR-10
@trace FR-TUTOR-12 INV-TUTOR-1 INV-TUTOR-3 INV-TUTOR-4 INV-TUTOR-5 INV-TUTOR-6 INV-TUTOR-7
"""

from __future__ import annotations

import contextlib
import json
import logging
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from .conftest import (
    MAT,
    SPANS,
    FakeJobs,
    FakeProvider,
    FakeSearch,
    Pg,
    Reply,
    answer_json,
    client,
    deps,
    execute,
    insert_question,
    need,
    question_row,
    rows,
    run_handler,
    scalar,
    world,
)

pytestmark = pytest.mark.anyio
D = Decimal
NO_SPEND = {"usd": 0.0, "output_tokens": 0, "thinking_tokens": None}  # FR-TUTOR-2: a row no model call reached


# ---- FR-TUTOR-3, -7, -8, -9: the handler


async def test_answer_is_validated_and_stored_with_citations_from_the_database(pg: Pg, tmp_path: Path) -> None:
    answer = need("reader.tutor.answer")
    params = need("reader.tutor.params")
    w = await world(pg, tmp_path)
    qid = await insert_question(w, cell_id="f01-explain")
    provider, search = FakeProvider(), FakeSearch()
    await run_handler(w, qid, deps(w, provider, search))
    state, stored, grounded, code, model = await question_row(w, qid)
    assert (state, grounded, code, model) == ("answered", True, None, "claude-sonnet-5-5")
    system, sha = answer.system_prompt()
    assert stored["prompt_sha256"] == sha
    assert stored["model"] == "claude-sonnet-5-5"
    assert stored["retrieval"] == "ok"
    assert stored["insufficient"] is False
    assert stored["blocks"] == [
        {
            "text": "Miners search for a nonce.",
            "supported": True,
            "quote": "Miners vary the nonce until the hash",
            "citations": [{"span": f"{MAT}:0", "page": 61, "material_title": "Book of ivan"}],
        }
    ]
    # FR-TUTOR-5: one search over the course's material; FR-TUTOR-7: one call with the tutor's options.
    assert search.calls == [(w.users["ivan"], "Why do miners vary the nonce?", params.RETRIEVE_K, MAT)]
    (call,) = provider.calls
    assert (call.owner_id, call.handle, call.run_id, call.lesson_id) == (w.users["ivan"], "ivan", qid, w.lessons["ivan"])
    assert (call.stage, call.task, call.attempt) == ("tutor", "tutor", 1)
    assert call.system == system
    assert call.thinking_off is True
    assert call.abort_on_retry is True
    assert call.deadline_s == params.CALL_DEADLINE_S
    assert call.max_output_tokens == params.MAX_OUTPUT_TOKENS
    assert call.run_cap_usd == params.QUESTION_CAP_USD
    assert call.daily_budget_usd == params.DAILY_CAP_USD
    assert call.floor_usd == answer.floor_usd(
        "claude-sonnet-5-5", len(system.encode()), len(call.prompt.encode())
    )
    sent = json.loads(call.prompt)
    assert sent["cell"]["id"] == "f01-explain"
    assert sent["cell"]["kind"] == "explain"
    assert sent["cell"]["text"].startswith("## Catching up")
    assert [s["id"] for s in sent["sources"]] == ["s1", "s2", "s3"]
    assert [s["page"] for s in sent["sources"]] == [61, 62, 63]
    assert all(MAT not in s["text"] for s in sent["sources"])  # the model never sees a span id


async def test_degraded_search_is_recorded_and_unsupported_blocks_kept(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    qid = await insert_question(w)
    blocks = [
        {"text": "Supported.", "spans": ["s2"], "quote": "adjusted every 2016 blocks so that"},
        {"text": "Invented.", "spans": ["s9", "s1"], "quote": "miners choose the reward freely here"},
    ]
    provider = FakeProvider([Reply(text=answer_json(blocks))])
    await run_handler(w, qid, deps(w, provider, FakeSearch(degraded=True)))
    state, stored, grounded, _, _ = await question_row(w, qid)
    assert (state, grounded, stored["retrieval"]) == ("answered", False, "degraded")
    assert [b["supported"] for b in stored["blocks"]] == [True, False]
    assert [c["span"] for c in stored["blocks"][1]["citations"]] == [f"{MAT}:0"]


async def test_no_source_fails_without_a_model_call(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    qid = await insert_question(w)
    provider = FakeProvider()
    await run_handler(w, qid, deps(w, provider, FakeSearch(seqs=[])))
    assert (await question_row(w, qid))[0::3] == ("failed", "sources_unavailable")
    assert provider.calls == []
    # A course without a material: no search at all.
    await execute(w.engine, "UPDATE courses SET material_sha256 = NULL WHERE owner_id = :o", o=w.users["ivan"])
    search = FakeSearch()
    qid2 = await insert_question(w, question="Another one?")
    await run_handler(w, qid2, deps(w, provider, search))
    assert (await question_row(w, qid2))[0::3] == ("failed", "sources_unavailable")
    assert search.calls == []
    assert provider.calls == []


async def test_expired_and_finished_rows_are_never_answered(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    provider = FakeProvider()
    old = await insert_question(w, age_s=601)
    await run_handler(w, old, deps(w, provider))
    assert (await question_row(w, old))[0::3] == ("expired", "expired")
    done = await insert_question(w, question="Second?")
    await execute(w.engine, "UPDATE tutor_questions SET state = 'failed', error_code = 'lost' WHERE id = :i", i=done)
    await run_handler(w, done, deps(w, provider))
    assert (await question_row(w, done))[0::3] == ("failed", "lost")
    assert provider.calls == []


@pytest.mark.parametrize(
    ("reply", "state", "code"),
    [
        (Reply(code="budget_exhausted"), "refused", "budget_exhausted"),
        (Reply(code="daily_budget_exhausted"), "refused", "daily_budget_exhausted"),
        (Reply(code="monthly_budget_exhausted"), "refused", "monthly_budget_exhausted"),
        (Reply(code="task_budget_exhausted"), "refused", "budget_exhausted"),
        (Reply(code="provider_missing"), "failed", "provider_missing"),
        (Reply(code="tools_mismatch"), "failed", "tutor_failed"),
        (Reply(stop_reason="max_tokens"), "failed", "output_truncated"),
    ],
)
async def test_permanent_outcomes_end_the_row_on_the_first_attempt(
    pg: Pg, tmp_path: Path, reply: Reply, state: str, code: str
) -> None:
    w = await world(pg, tmp_path)
    qid = await insert_question(w)
    await run_handler(w, qid, deps(w, FakeProvider([reply])), attempts=0)
    assert (await question_row(w, qid))[0::3] == (state, code)


@pytest.mark.parametrize(
    ("reply", "code"),
    [
        (Reply(code="upstream_retry"), "provider_unavailable"),
        (Reply(code="deadline_exceeded"), "provider_unavailable"),
        (Reply(code="rate_limited"), "rate_limited"),
        (Reply(code="provider_unavailable"), "provider_unavailable"),
        (Reply(text="Sure, here it is."), "output_invalid"),
    ],
)
async def test_transient_retries_once_then_fails(pg: Pg, tmp_path: Path, reply: Reply, code: str) -> None:
    jobs = need("reader.tutor.jobs")
    w = await world(pg, tmp_path)
    qid = await insert_question(w)
    d = deps(w, FakeProvider([reply, reply]))
    kind = jobs.make_kind(d)
    with pytest.raises(kind.transient) as failure:
        await run_handler(w, qid, d, attempts=0)
    assert failure.value.code == code
    assert (await question_row(w, qid))[0] == "queued"  # rolled back: SRV-EVENTS retries the key
    await run_handler(w, qid, d, attempts=1)  # the last attempt (attempts_max = 2)
    assert (await question_row(w, qid))[0::3] == ("failed", code)


def test_job_kind_declaration() -> None:
    jobs = need("reader.tutor.jobs")
    params = need("reader.tutor.params")
    kind = jobs.JOB_KINDS[0]
    assert len(jobs.JOB_KINDS) == 1
    assert kind.name == "tutor.answer"
    assert kind.key == ("question_id",)
    assert kind.max_attempts == params.ATTEMPTS_MAX
    assert kind.timeout_s == params.JOB_TIMEOUT_S
    assert kind.priority == params.PRIORITY
    assert kind.args.model_config.get("extra") == "forbid"
    with pytest.raises(Exception):  # noqa: B017, PT011 (pydantic ValidationError)
        kind.args.model_validate({"question_id": str(uuid.uuid4()), "question": "text"})


# ---- FR-TUTOR-7 / INV-TUTOR-5: stalled calls through the real SRV-AGENT adapter


def _real(w: Any, scripts: list[Any], tmp: Path, deadline_s: float = 0.3) -> tuple[Any, Any]:
    from agent.conftest import FakeCli, FakeProxyClient
    from reader.agent.provider import AgentProvider

    jobs = need("reader.tutor.jobs")
    cli = FakeCli(scripts)
    provider = AgentProvider(w.engine, w.config, FakeProxyClient(cli), cli=cli, home_root=tmp)
    d = jobs.Deps(
        provider=lambda _engine: contextlib.nullcontext(provider),
        search=FakeSearch(),
        blobs_root=w.blobs,
        config=lambda: w.config,
        deadline_s=deadline_s,
    )
    return d, cli


async def _settles(w: Any, qid: uuid.UUID) -> list[tuple[Decimal, Decimal]]:
    found = await rows(
        w.engine,
        "SELECT r.usd, s.settled_usd FROM agent_spend r JOIN agent_spend s ON s.reservation_id = r.id"
        " WHERE r.run_id = :q AND r.kind = 'reserve' ORDER BY r.id",
        q=qid,
    )
    return [(r[0], r[1]) for r in found]


async def test_stall_before_any_2xx_settles_zero(pg: Pg, tmp_path: Path) -> None:
    from agent.conftest import Script

    w = await world(pg, tmp_path)
    qid = await insert_question(w)
    d, cli = _real(w, [Script(sleep_s=5, statuses=()), Script(sleep_s=5, statuses=())], tmp_path)
    with pytest.raises(Exception) as failure:  # noqa: PT011 (the kind's transient type)
        await run_handler(w, qid, d, attempts=0)
    assert failure.value.code == "provider_unavailable"  # type: ignore[attr-defined]
    await run_handler(w, qid, d, attempts=1)
    assert (await question_row(w, qid))[0::3] == ("failed", "provider_unavailable")
    assert cli.closed == [True, True]
    assert [s for _, s in await _settles(w, qid)] == [D(0), D(0)]


async def test_stall_after_a_2xx_settles_the_reserve(pg: Pg, tmp_path: Path) -> None:
    from agent.conftest import Script

    ledger = need("reader.agent.ledger")
    params = need("reader.tutor.params")
    w = await world(pg, tmp_path)
    qid = await insert_question(w)
    d, _ = _real(w, [Script(sleep_s=5, statuses=(200,)), Script(sleep_s=5, statuses=(200,))], tmp_path)
    with pytest.raises(Exception):  # noqa: B017, PT011
        await run_handler(w, qid, d, attempts=0)
    await run_handler(w, qid, d, attempts=1)
    settles = await _settles(w, qid)
    assert len(settles) == 2
    assert all(reserve == settled for reserve, settled in settles)
    spent = await ledger.spent(w.engine, run_id=qid)
    assert spent == sum(s for _, s in settles)
    assert spent <= params.QUESTION_CAP_USD  # INV-TUTOR-5


async def test_api_retry_aborts_the_call(pg: Pg, tmp_path: Path) -> None:
    from agent.conftest import Script

    w = await world(pg, tmp_path)
    qid = await insert_question(w)
    d, cli = _real(w, [Script(api_retries=1, statuses=(529,)), Script(api_retries=1, statuses=(529,))], tmp_path, 5)
    with pytest.raises(Exception):  # noqa: B017, PT011
        await run_handler(w, qid, d, attempts=0)
    await run_handler(w, qid, d, attempts=1)
    assert (await question_row(w, qid))[0::3] == ("failed", "provider_unavailable")
    assert cli.closed == [True, True]


async def test_settlement_above_the_floor_is_logged(
    pg: Pg, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from agent.conftest import Script

    w = await world(pg, tmp_path)
    qid = await insert_question(w)
    d, _ = _real(w, [Script(text=answer_json(), cost=0.5)], tmp_path, 5)
    with caplog.at_level(logging.INFO):
        await run_handler(w, qid, d)
    assert (await question_row(w, qid))[0] == "answered"
    assert any("reserve_exceeded" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


# ---- FR-TUTOR-1, -2, INV-TUTOR-1: routes


async def test_ask_queues_a_question_and_enqueues_its_job(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    jobs = FakeJobs()
    lesson = w.lessons["ivan"]
    async with client(w, jobs=jobs) as c:
        r = await c.post(f"/api/lessons/{lesson}/ask", json={"question": "  What is a nonce?\n", "cell_id": "f01-lab"})
        assert r.status_code == 202
        qid = uuid.UUID(r.json()["question_id"])
        assert set(r.json()) == {"question_id"}
        assert jobs.enqueued == [("tutor.answer", {"question_id": str(qid)})]
        found = await rows(
            w.engine, "SELECT state, question, cell_id, revision_sha256 FROM tutor_questions WHERE id = :i", i=qid
        )
        assert found == [("queued", "What is a nonce?", "f01-lab", w.revisions["ivan"])]
        one = await c.get(f"/api/lessons/{lesson}/questions/{qid}")
        assert one.status_code == 200
        assert one.json() == {"state": "queued"}
        listed = await c.get(f"/api/lessons/{lesson}/questions")
        (item,) = listed.json()["questions"]
        assert item["question_id"] == str(qid)
        assert (item["question"], item["cell_id"], item["state"]) == ("What is a nonce?", "f01-lab", "queued")
        assert (item["grounded"], item["error_code"]) == (None, None)
        assert set(item) == {"question_id", "question", "cell_id", "state", "grounded", "error_code", "created_at"}


@pytest.mark.parametrize(
    "question",
    ["", "   ", "x" * 1001, "😀" * 800, "a\x00b", "bell\x07", "esc\x1b[0m", None, 5],
)
async def test_invalid_questions(pg: Pg, tmp_path: Path, question: Any) -> None:
    w = await world(pg, tmp_path)
    jobs = FakeJobs()
    async with client(w, jobs=jobs) as c:
        r = await c.post(f"/api/lessons/{w.lessons['ivan']}/ask", json={"question": question})
    assert (r.status_code, r.json()) == (400, {"error": "question_invalid"})
    assert jobs.enqueued == []
    assert await scalar(w.engine, "SELECT count(*) FROM tutor_questions") == 0


async def test_question_limits_accept_newlines_tabs_and_1000_characters(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    async with client(w, jobs=FakeJobs()) as c:
        r = await c.post(f"/api/lessons/{w.lessons['ivan']}/ask", json={"question": "line\n\tnext " + "я" * 989})
    assert r.status_code == 202


async def test_revision_and_cell_checks(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    lesson = w.lessons["ivan"]
    async with client(w, jobs=FakeJobs()) as c:
        bad_rev = await c.post(f"/api/lessons/{lesson}/ask", json={"question": "q", "revision": "e" * 64})
        foreign_rev = await c.post(f"/api/lessons/{lesson}/ask", json={"question": "q", "revision": "nope"})
        bad_cell = await c.post(f"/api/lessons/{lesson}/ask", json={"question": "q", "cell_id": "no-such-cell"})
        ok = await c.post(
            f"/api/lessons/{lesson}/ask",
            json={"question": "q", "revision": w.revisions["ivan"], "cell_id": "f01-explain"},
        )
    assert (bad_rev.status_code, bad_rev.json()) == (400, {"error": "unknown_revision"})
    assert (foreign_rev.status_code, foreign_rev.json()) == (400, {"error": "unknown_revision"})
    assert (bad_cell.status_code, bad_cell.json()) == (400, {"error": "cell_unknown"})
    assert ok.status_code == 202


async def test_foreign_unknown_and_malformed_ids_answer_like_missing(pg: Pg, tmp_path: Path) -> None:
    from reader.lessons import store as lessons

    w = await world(pg, tmp_path)
    anna_q = await insert_question(w, "anna")
    ivan_q = await insert_question(w, "ivan")
    async with w.engine.begin() as conn:
        course = await lessons.create_course(conn, w.users["ivan"], "Second", None)
        other = await lessons.ensure_lesson(conn, w.users["ivan"], course, 1, "Other")
    responses = []
    async with client(w, jobs=FakeJobs()) as c:
        missing = uuid.uuid4()
        for path in (
            f"/api/lessons/{missing}/questions/{ivan_q}",
            f"/api/lessons/{w.lessons['anna']}/questions/{anna_q}",
            f"/api/lessons/{w.lessons['ivan']}/questions/{anna_q}",
            f"/api/lessons/{other}/questions/{ivan_q}",
            f"/api/lessons/{w.lessons['ivan']}/questions/{uuid.uuid4()}",
            f"/api/lessons/{w.lessons['ivan']}/questions/not-a-uuid",
            f"/api/lessons/not-a-uuid/questions/{ivan_q}",
            f"/api/lessons/{w.lessons['anna']}/questions",
            f"/api/lessons/{missing}/questions",
        ):
            responses.append(await c.get(path))
        responses.append(await c.post(f"/api/lessons/{w.lessons['anna']}/ask", json={"question": "q"}))
        responses.append(await c.post(f"/api/lessons/{missing}/ask", json={"question": "q"}))
    shapes = {(r.status_code, r.text, tuple(sorted(r.headers.keys()))) for r in responses}
    assert len(shapes) == 1
    assert (responses[0].status_code, responses[0].json()) == (404, {"error": "not_found"})


async def test_session_required(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    async with client(w, handle=None, jobs=FakeJobs()) as c:
        r = await c.post(f"/api/lessons/{w.lessons['ivan']}/ask", json={"question": "q"})
    assert r.status_code == 401


async def test_busy_while_a_question_is_live(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    jobs = FakeJobs()
    lesson = w.lessons["ivan"]
    async with client(w, jobs=jobs) as c:
        first = await c.post(f"/api/lessons/{lesson}/ask", json={"question": "one"})
        assert first.status_code == 202
        second = await c.post(f"/api/lessons/{lesson}/ask", json={"question": "two"})
        assert (second.status_code, second.json()) == (409, {"error": "tutor_busy"})
        # anna is not blocked by ivan's question
    async with client(w, "anna", jobs=jobs) as c:
        assert (await c.post(f"/api/lessons/{w.lessons['anna']}/ask", json={"question": "q"})).status_code == 202


async def test_concurrent_insert_hits_the_unique_index(pg: Pg, tmp_path: Path) -> None:
    store = need("reader.tutor.store")
    errors = need("reader.tutor.errors")
    w = await world(pg, tmp_path)
    await insert_question(w)
    with pytest.raises(errors.TutorError) as busy:
        await insert_question(w, question="raced")
    assert busy.value.code == "tutor_busy"
    assert store is not None


async def test_daily_pre_check(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    await execute(
        w.engine,
        "INSERT INTO agent_spend (owner_id, run_id, stage, task, attempt, kind, model, usd)"
        " VALUES (:o, :r, 'tutor', 'tutor', 1, 'reserve', 'claude-sonnet-5-5', 2.90)",
        o=w.users["ivan"], r=uuid.uuid4(),
    )
    jobs = FakeJobs()
    async with client(w, jobs=jobs) as c:
        r = await c.post(f"/api/lessons/{w.lessons['ivan']}/ask", json={"question": "q"})
        assert (r.status_code, r.json()) == (429, {"error": "daily_budget_exhausted"})
    async with client(w, "anna", jobs=jobs) as c:
        assert (await c.post(f"/api/lessons/{w.lessons['anna']}/ask", json={"question": "q"})).status_code == 202
    assert await scalar(w.engine, "SELECT count(*) FROM tutor_questions WHERE owner_id = :o", o=w.users["ivan"]) == 0


@pytest.mark.parametrize("jobs", [FakeJobs(fail=True), None], ids=["enqueue-error", "no-jobs"])
async def test_enqueue_failure_finishes_the_row(pg: Pg, tmp_path: Path, jobs: Any) -> None:
    w = await world(pg, tmp_path)
    lesson = w.lessons["ivan"]
    async with client(w, jobs=jobs) as c:
        r = await c.post(f"/api/lessons/{lesson}/ask", json={"question": "q"})
        assert r.status_code == 202
        qid = r.json()["question_id"]
        assert (await c.get(f"/api/lessons/{lesson}/questions/{qid}")).json() == {
            "state": "failed", "error_code": "tutor_failed", "usage": NO_SPEND
        }
        # INV-TUTOR-6: nothing stays queued, so a new question can be asked
        assert (await c.post(f"/api/lessons/{lesson}/ask", json={"question": "again"})).status_code == 202


# ---- FR-TUTOR-10, INV-TUTOR-6: stale rows


async def test_stale_rows_are_finalised_by_reads_and_the_busy_check(pg: Pg, tmp_path: Path) -> None:
    tutor_jobs = need("reader.tutor.jobs")
    w = await world(pg, tmp_path)
    lesson = w.lessons["ivan"]
    jobs = FakeJobs()
    lost = await insert_question(w, age_s=61)
    async with client(w, jobs=jobs) as c:
        assert (await c.get(f"/api/lessons/{lesson}/questions/{lost}")).json() == {
            "state": "failed", "error_code": "lost", "usage": NO_SPEND
        }
        dead = await insert_question(w, age_s=200)
        jobs.statuses[tutor_jobs.job_key(dead)] = "dead"
        listed = (await c.get(f"/api/lessons/{lesson}/questions")).json()["questions"]
        # newest first (FR-TUTOR-2): the lost row is 61 s old, the dead one 200 s
        assert [(q["state"], q["error_code"]) for q in listed] == [("failed", "lost"), ("failed", "tutor_failed")]
        waiting = await insert_question(w, age_s=601)
        jobs.statuses[tutor_jobs.job_key(waiting)] = "queued"
        r = await c.post(f"/api/lessons/{lesson}/ask", json={"question": "new"})  # the busy check finalises it
        assert r.status_code == 202
    assert (await question_row(w, waiting))[0::3] == ("expired", "expired")


async def test_running_job_keeps_the_row_queued(pg: Pg, tmp_path: Path) -> None:
    tutor_jobs = need("reader.tutor.jobs")
    w = await world(pg, tmp_path)
    jobs = FakeJobs()
    running = await insert_question(w, age_s=601)
    jobs.statuses[tutor_jobs.job_key(running)] = "running"
    async with client(w, jobs=jobs) as c:
        r = await c.get(f"/api/lessons/{w.lessons['ivan']}/questions/{running}")
    assert r.json() == {"state": "queued"}


# ---- INV-TUTOR-7: composite foreign keys


async def test_rows_belong_to_the_lessons_owner_and_revision(pg: Pg, tmp_path: Path) -> None:
    from sqlalchemy.exc import IntegrityError

    store = need("reader.tutor.store")
    w = await world(pg, tmp_path)
    for owner, lesson, revision in (
        (w.users["anna"], w.lessons["ivan"], w.revisions["ivan"]),
        (w.users["ivan"], w.lessons["ivan"], "f" * 64),
    ):
        with pytest.raises(IntegrityError):
            async with w.engine.begin() as conn:
                await store.insert_question(conn, owner, lesson, revision, None, "q")


# ---- FR-TUTOR-12, INV-TUTOR-4: no text in logs or job args


async def test_no_text_leaks_into_logs_or_job_args(pg: Pg, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    canaries = {"q": "CANARY-QUESTION-7f3a", "span": "CANARY-SPAN-91bc", "answer": "CANARY-ANSWER-55de"}
    marked = [(SPANS[0][0], f"{SPANS[0][1]} {canaries['span']}"), *SPANS[1:]]  # the canary in seq 0's text
    w = await world(pg, tmp_path, {"ivan": marked, "anna": marked})
    jobs = FakeJobs()
    blocks = [{"text": canaries["answer"], "spans": ["s1"], "quote": "Miners vary the nonce until the hash"}]
    with caplog.at_level(logging.DEBUG):
        async with client(w, jobs=jobs) as c:
            r = await c.post(
                f"/api/lessons/{w.lessons['ivan']}/ask",
                json={"question": f"Why {canaries['q']}?", "cell_id": "f01-explain"},
            )
            qid = uuid.UUID(r.json()["question_id"])
        await run_handler(w, qid, deps(w, FakeProvider([Reply(text=answer_json(blocks))])))
    assert (await question_row(w, qid))[0] == "answered"
    text = "\n".join(r.getMessage() for r in caplog.records)
    for canary in (*canaries.values(), "Catching up"):
        assert canary not in text
    assert canaries["q"] not in json.dumps(jobs.enqueued)
    assert any("tutor" in r.name for r in caplog.records)  # the module does log (ids, codes, timings)
