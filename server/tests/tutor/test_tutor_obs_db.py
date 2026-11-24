"""SRV-OBS [db]: the `tutor.answer` handler's call sites of the obs helpers — the root keys (lesson, course,
prompt version, final state), the session marker with the lesson, the `tutor.retrieve` and `tutor.citations`
child spans with their counts, and the code assessments enqueued as `obs.feedback` (exact args); the question,
the answer and the book text reach no span and no job arg.

@trace FR-OBS-12 FR-OBS-13 FR-OBS-14 FR-OBS-18 INV-OBS-2 INV-OBS-3
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from obs.conftest import finished, obs_setup, require

from .conftest import (
    FakeProvider,
    FakeSearch,
    Pg,
    Reply,
    answer_json,
    deps,
    execute,
    insert_question,
    need,
    world,
)

pytestmark = pytest.mark.anyio
QUESTION = "questioncanary-1b2c"
ANSWER = "answercanary-9d8e"
BOOK = "bookcanary-7f3a9c"
PROMPT_VERSION = 4
JOB_ID, KEY = 31, "tutor.answer:" + "e" * 64


class Captured:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict[str, Any], int]] = []

    async def enqueue(self, kind: str, args: Any, schedule_in: int = 0) -> Any:
        self.enqueued.append((kind, dict(args), schedule_in))
        return None


async def _run(w: Any, qid: uuid.UUID, provider: Any, monkeypatch: pytest.MonkeyPatch, age_s: int = 0) -> Any:
    import reader.jobs
    from reader.jobs.model import JobContext
    from reader.obs import prompts

    captured = Captured()
    monkeypatch.setattr(reader.jobs, "running", lambda: captured)

    async def version_of(name: str) -> int | None:
        return PROMPT_VERSION if name == "reader.tutor" else None

    monkeypatch.setattr(prompts, "version_of", version_of)
    kind = need("reader.tutor.jobs").make_kind(deps(w, provider, FakeSearch()))
    args = kind.args.model_validate({"question_id": str(qid)})
    with obs_setup("worker") as (handle, memory):
        tracer = require("reader.obs").tracer()
        with tracer.start_as_current_span("job tutor.answer", attributes={"reader.job.kind": "tutor.answer"}):
            async with w.engine.connect() as conn, conn.begin():
                await kind.handler(args, conn, JobContext(job_id=JOB_ID, key=KEY, attempts=0))
        return finished(handle, memory), captured.enqueued


def _by_name(spans: list[Any]) -> dict[str, Any]:
    return {s.name: s for s in spans}


def _no_canary(spans: list[Any], enqueued: list[Any]) -> None:
    dumped = json.dumps([dict(s.attributes) for s in spans], default=str) + json.dumps(enqueued)
    for canary in (QUESTION, ANSWER, BOOK):
        assert canary not in dumped


async def test_answer_spans_root_keys_and_assessments(
    pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = await world(pg, tmp_path)
    await execute(w.engine, "UPDATE material_spans SET text = text || ' ' || :c", c=BOOK)
    qid = await insert_question(w, question=f"Why {QUESTION}?")
    blocks = [
        {"text": ANSWER, "spans": ["s1"], "quote": "Miners vary the nonce until the hash"},
        {"text": "second", "spans": ["s9"], "quote": "not in any source"},  # unsupported: valid 1 of 2
    ]
    spans, enqueued = await _run(w, qid, FakeProvider([Reply(text=answer_json(blocks))]), monkeypatch)
    got = _by_name(spans)
    root = got["job tutor.answer"]
    lesson = w.lessons["ivan"]
    from sqlalchemy import text

    async with w.engine.connect() as conn:
        course = (await conn.execute(text("SELECT course_id FROM lessons WHERE id = :l"), {"l": lesson})).scalar()
    # FR-OBS-5 (e), FR-OBS-18: the root's ids, the prompt version and the final state.
    assert root.attributes["reader.lesson.id"] == str(lesson)
    assert root.attributes["reader.course.id"] == str(course)
    assert root.attributes["reader.prompt.version"] == PROMPT_VERSION
    assert root.attributes["reader.tutor.state"] == "answered"
    # FR-OBS-12 (e): the marker carries the lesson as the session, under the root.
    marker = got["job.session"]
    assert marker.attributes["session.id"] == str(lesson)
    assert marker.parent.span_id == root.context.span_id
    # FR-OBS-13: retrieval counts (FakeSearch: 3 one-span hits) and citation counts (2 blocks, 1 supported).
    retrieve, cited = got["tutor.retrieve"], got["tutor.citations"]
    assert (retrieve.attributes["reader.retrieval.hits"], retrieve.attributes["reader.retrieval.candidates"]) == (3, 3)
    assert (cited.attributes["reader.citations.total"], cited.attributes["reader.citations.valid"]) == (2, 1)
    for child in (retrieve, cited):
        assert child.parent.span_id == root.context.span_id
        assert child.status.status_code.name == "OK"
    # FR-OBS-14: exactly two assessments, for this job's trace.
    assert enqueued == [
        ("obs.feedback", {"job_id": JOB_ID, "key": KEY, "name": "tutor.citations_valid", "value": False,
                          "metadata": {"total": 2, "valid": 1}}, 60),
        ("obs.feedback", {"job_id": JOB_ID, "key": KEY, "name": "tutor.citation_share", "value": 0.5,
                          "metadata": {}}, 60),
    ]
    _no_canary(spans, enqueued)


async def test_all_valid_citations(pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = await world(pg, tmp_path)
    qid = await insert_question(w, question=f"Why {QUESTION}?")
    spans, enqueued = await _run(w, qid, FakeProvider([Reply(text=answer_json())]), monkeypatch)
    assert [(a["name"], a["value"], a["metadata"]) for _, a, _ in enqueued] == [
        ("tutor.citations_valid", True, {"total": 1, "valid": 1}),
        ("tutor.citation_share", 1.0, {}),
    ]
    _no_canary(spans, enqueued)


async def test_expired_question_marks_the_root(pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = await world(pg, tmp_path)
    params = need("reader.tutor.params")
    qid = await insert_question(w, question=f"Why {QUESTION}?", age_s=params.QUESTION_TTL_S + 60)
    spans, enqueued = await _run(w, qid, FakeProvider(), monkeypatch)
    got = _by_name(spans)
    assert got["job tutor.answer"].attributes["reader.tutor.state"] == "expired"
    assert got["job.session"].attributes["session.id"] == str(w.lessons["ivan"])
    assert "tutor.retrieve" not in got and enqueued == []


async def test_failed_answer_marks_the_root(pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = await world(pg, tmp_path)
    qid = await insert_question(w, question=f"Why {QUESTION}?")
    spans, enqueued = await _run(w, qid, FakeProvider([Reply(code="provider_missing")]), monkeypatch)
    root = _by_name(spans)["job tutor.answer"]
    assert root.attributes["reader.tutor.state"] == "failed"
    assert enqueued == []
    _no_canary(spans, enqueued)
