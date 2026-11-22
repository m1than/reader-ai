"""The tutor bench (SRV-TUTOR NFR-TUTOR-1..3, VEC-TUTOR-7): `python -I -m reader.tutor.bench` in the worker.

Reads one JSON object on stdin: `{"lesson_ordinal": int, "fixed": [{"id": str, "question": str}]}`. Generates
one question per explain cell of the lesson's current revision (the first TAKE, in notebook order), adds the
fixed questions, and asks them one at a time through the real queue and worker: each question is inserted and
enqueued, and its seconds run from the insert to its terminal state, polled every 0.5 s. Prints one JSON
object of numbers, flags and ids: never a question, a title, a quote or an answer.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from reader import blobs
from reader.agent import ledger
from reader.tutor import params, store

CELL_KIND: Final = "explain"
TAKE: Final = 20
TEMPLATE: Final = "Explain in your own words: {cell_title}"
POLL_S: Final = 0.5
POLL_DEADLINE_S: Final = params.QUESTION_TTL_S + 30
THRESHOLDS: Final = {"p95_s": 30.0, "mean_usd": 0.06, "grounded_share": 0.8}  # NFR-TUTOR-1, -2, -3
_LESSON: Final = (
    "SELECT u.id, l.id, (SELECT s.sha256 FROM lesson_revision_stages s"
    " WHERE s.lesson_id = l.id ORDER BY s.id DESC LIMIT 1)"
    " FROM users u JOIN courses c ON c.owner_id = u.id JOIN lessons l ON l.course_id = c.id"
    " WHERE u.handle = :h AND c.material_sha256 IS NOT NULL AND l.ordinal = :n"
    " ORDER BY c.created_at LIMIT 1"
)
_STATE: Final = "SELECT state, answer, grounded, error_code FROM tutor_questions WHERE id = :q"
# thinking: SRV-AGENT's settle rows (agent_0002); null when no settle row recorded it
_OUTPUT: Final = (
    "SELECT coalesce(sum(output), 0), sum(thinking) FROM agent_spend WHERE run_id = :q AND kind = 'settle'"
)


class BenchError(Exception):
    pass


def _source(cell: dict[str, Any]) -> str:
    source = cell.get("source", "")
    return "".join(source) if isinstance(source, list) else str(source)


def title_of(cell: dict[str, Any]) -> str | None:
    for line in _source(cell).splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip() or None
    return None


def questions(notebook: dict[str, Any]) -> list[tuple[str, str]]:
    """(cell id, question) for the first TAKE explain cells, in notebook order; BenchError before any
    question when there is no explain cell or a selected one has no title or id."""
    cells = [
        c
        for c in notebook.get("cells", [])
        if ((c.get("metadata") or {}).get("reader") or {}).get("kind") == CELL_KIND
    ][:TAKE]
    if not cells:
        raise BenchError("no explain cell in the revision")
    out = []
    for cell in cells:
        title = title_of(cell)
        if title is None or not isinstance(cell.get("id"), str):
            raise BenchError("an explain cell has no title or id")
        out.append((cell["id"], TEMPLATE.format(cell_title=title)))
    return out


def p95(samples: Sequence[float]) -> float:
    """NFR-TUTOR-1: nearest rank, rank ceil(0.95 n) of the samples sorted ascending."""
    ordered = sorted(samples)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def summary(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    n = len(records)
    blocks = sum(r["blocks"] for r in records)
    verified = sum(r["blocks_with_verified_quote"] for r in records)
    p95_s = p95([r["wall_s"] for r in records]) if n else None
    mean_usd = round(sum(r["usd"] for r in records) / n, 6) if n else None
    grounded = round(sum(1 for r in records if r["grounded"]) / n, 4) if n else None
    return {
        "n": n,
        "p95_s": p95_s,
        "mean_usd": mean_usd,
        "grounded_share": grounded,
        "verified_block_share": round(verified / blocks, 4) if blocks else None,
        "pass": {
            "NFR-TUTOR-1": p95_s is not None and p95_s <= THRESHOLDS["p95_s"],
            "NFR-TUTOR-2": mean_usd is not None and mean_usd <= THRESHOLDS["mean_usd"],
            "NFR-TUTOR-3": grounded is not None and grounded >= THRESHOLDS["grounded_share"],
        },
    }


@dataclass(frozen=True)
class Target:
    owner: uuid.UUID
    lesson: uuid.UUID
    revision: str


async def _ask(
    engine: AsyncEngine, jobs: Any, t: Target, cell: str | None, question: str, label: str
) -> dict[str, Any]:
    async with engine.begin() as conn:
        qid = await store.insert_question(conn, t.owner, t.lesson, t.revision, cell, question)
    started = time.monotonic()
    await jobs.enqueue(params.KIND, {"question_id": str(qid)})
    while True:
        async with engine.connect() as conn:
            state, answer, grounded, code = (await conn.execute(text(_STATE), {"q": qid})).one()
        if state != "queued" or time.monotonic() - started > POLL_DEADLINE_S:
            break
        await asyncio.sleep(POLL_S)
    wall = time.monotonic() - started
    async with engine.connect() as conn:
        output, thinking = (await conn.execute(text(_OUTPUT), {"q": qid})).one()
    blocks = (answer or {}).get("blocks", [])
    return {
        "id": label,
        "question_id": str(qid),
        "state": state,
        "error_code": code,
        "wall_s": round(wall, 2),
        "usd": float(await ledger.spent(engine, run_id=qid)),
        "output_tokens": int(output),
        "thinking_tokens": None if thinking is None else int(thinking),
        "grounded": bool(grounded),
        "blocks": len(blocks),
        "blocks_with_verified_quote": sum(1 for b in blocks if b["supported"]),
    }


async def _run(spec: dict[str, Any]) -> dict[str, Any]:
    from reader.config import load_config
    from reader.db import make_engine
    from reader.jobs.catalog import load_kinds
    from reader.jobs.queue import Jobs
    from reader.jobs.worker import make_app
    from reader.serve.main import Env, _target

    env = Env.read(os.environ)
    config = load_config(Path(env.config))
    handle = next(u.handle for u in config.users if u.role == "owner")
    db = _target(env)
    engine = make_engine(db)
    queue = make_app(db)
    await queue.open_async()
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(text(_LESSON), {"h": handle, "n": int(spec["lesson_ordinal"])})).one()
        t = Target(row[0], row[1], row[2])
        notebook = json.loads(await asyncio.to_thread(blobs.get, blobs.DEFAULT_ROOT, t.revision))
        asked: list[tuple[str | None, str, str]] = [(c, q, f"cell:{c}") for c, q in questions(notebook)]
        asked += [(None, str(f["question"]), f"fixed:{f['id']}") for f in spec.get("fixed", [])]
        jobs = Jobs(engine, queue, load_kinds(), (db.password.reveal(),))
        records = [await _ask(engine, jobs, t, c, q, label) for c, q, label in asked]
        report = {"lesson_id": str(t.lesson), "revision": t.revision, "records": records}
        return {**report, "summary": summary(records)}
    finally:
        await queue.close_async()
        await engine.dispose()


def main() -> int:
    try:
        report = asyncio.run(_run(json.loads(sys.stdin.read())))
    except BenchError as error:
        sys.stderr.write(f"bench: {error}\n")
        return 1
    sys.stdout.write(json.dumps(report) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
