"""`python -I -m reader.tutor.live`: FR-TUTOR-14's server half, run in the worker container by the live check.

Asks about the first explain cell of the owner's first lesson whose course has a material, through the real
queue and the real worker, waits for a terminal state (≤ poll_deadline_s) and prints one JSON report of ids,
counts, flags, timings and costs. Never prints a question, a quote or an answer.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Final

from sqlalchemy import text

from reader import blobs
from reader.agent import ledger
from reader.config import load_config
from reader.db import make_engine
from reader.jobs.catalog import load_kinds
from reader.jobs.queue import Jobs
from reader.jobs.worker import make_app
from reader.serve.main import Env, _target
from reader.tutor import params, store

POLL_DEADLINE_S: Final = params.QUESTION_TTL_S + 30
TEMPLATE: Final = "Explain in your own words: {title}"


def _title(source: str) -> str | None:
    for line in source.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip() or None
    return None


async def _run() -> dict[str, Any]:
    env = Env.read(os.environ)
    config = load_config(Path(env.config))
    owner = next(u for u in config.users if u.role == "owner")
    target = _target(env)
    engine = make_engine(target)
    queue = make_app(target)
    await queue.open_async()
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT u.id, l.id, (SELECT s.sha256 FROM lesson_revision_stages s"
                        " WHERE s.lesson_id = l.id ORDER BY s.id DESC LIMIT 1)"
                        " FROM users u JOIN courses c ON c.owner_id = u.id"
                        " JOIN lessons l ON l.course_id = c.id"
                        " WHERE u.handle = :h AND c.material_sha256 IS NOT NULL"
                        " ORDER BY c.created_at, l.ordinal LIMIT 1"
                    ),
                    {"h": owner.handle},
                )
            ).one()
        owner_id, lesson_id, revision = row
        cells = await asyncio.to_thread(store.revision_cells, blobs.DEFAULT_ROOT, revision)
        cell = next(c for c in cells.values() if c.kind == "explain" and _title(c.text))
        title = _title(cell.text)
        async with engine.begin() as conn:
            qid = await store.insert_question(
                conn, owner_id, lesson_id, revision, cell.id, TEMPLATE.format(title=title)
            )
        await Jobs(engine, queue, load_kinds(), (target.password.reveal(),)).enqueue(
            params.KIND, {"question_id": str(qid)}
        )
        started = time.monotonic()
        while True:
            async with engine.connect() as conn:
                state, answer, grounded = (
                    await conn.execute(
                        text("SELECT state, answer, grounded FROM tutor_questions WHERE id = :q"), {"q": qid}
                    )
                ).one()
            if state != "queued" or time.monotonic() - started > POLL_DEADLINE_S:
                break
            await asyncio.sleep(2)
        seconds = time.monotonic() - started
        blocks = (answer or {}).get("blocks", [])
        return {
            "state": state,
            "grounded": bool(grounded),
            "citations": sum(len(b["citations"]) for b in blocks),
            "verified_blocks": sum(1 for b in blocks if b["supported"]),
            "blocks": len(blocks),
            "usd": float(await ledger.spent(engine, run_id=qid)),
            "seconds": round(seconds, 1),
        }
    finally:
        await queue.close_async()
        await engine.dispose()


def main() -> int:
    sys.stdout.write(json.dumps(asyncio.run(_run())) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
