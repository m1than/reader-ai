"""`python -I -m reader.pipeline.live <plan lesson id>`: FR-PIPELINE-13's live compile (worker container).

Starts one run of the owner's lesson (imported by the G1 course import), waits for it to finish (at most an
hour) and prints a JSON report: state, refusals, spent, cap, and the settled and SDK-reported ledger sums.
Spends credit; the coordinator schedules it from the owner's test budget. Never prints a token, a prompt or
an output.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import text

from reader.config.effective import effective_settings
from reader.db import make_engine
from reader.jobs import load_kinds
from reader.jobs.queue import Jobs
from reader.jobs.worker import make_app
from reader.pipeline import runs
from reader.pipeline import runtime as runtime_mod
from reader.serve.main import Env, _target

WAIT_S: Final = 3600.0
POLL_S: Final = 10.0


async def _run(plan_lesson_id: str) -> dict[str, Any]:
    target = _target(Env.read(os.environ))
    engine = make_engine(target)
    queue_app = make_app(target)
    await queue_app.open_async()
    try:
        jobs = Jobs(engine, queue_app, load_kinds(), [target.password.reveal()])
        async with engine.connect() as conn:
            owner, lesson = (
                await conn.execute(
                    text(
                        "SELECT p.owner_id, p.lesson_id FROM course_plans p JOIN users u ON u.id = p.owner_id"
                        " WHERE p.plan_lesson_id = :p AND u.role = 'owner' LIMIT 1"
                    ),
                    {"p": plan_lesson_id},
                )
            ).one()
        rt = runtime_mod.get(engine)
        async with engine.connect() as conn:  # FR-PIPELINE-13 revised: effective budgets, not the snapshot
            settings = await effective_settings(conn, rt.config)
        budgets = {k: str(v) for k, v in settings.task_budgets.items() if v is not None}
        started = time.monotonic()
        run = await runs.create_run(engine, jobs, owner, lesson, Decimal("8.00"))
        deadline = time.monotonic() + WAIT_S
        status = await runs.status(engine, jobs, owner, run)
        while status["state"] in ("running", "stalled") and time.monotonic() < deadline:
            await asyncio.sleep(POLL_S)
            status = await runs.status(engine, jobs, owner, run)
        async with engine.connect() as conn:
            settled, sdk = (
                await conn.execute(
                    text(
                        "SELECT coalesce(sum(settled_usd), 0), coalesce(sum(sdk_usd), 0) FROM agent_spend"
                        " WHERE run_id = :r AND kind = 'settle'"
                    ),
                    {"r": run},
                )
            ).one()
            by_task = {
                str(t): str(u)
                for t, u in await conn.execute(
                    text(
                        "SELECT task, coalesce(sum(settled_usd), 0) FROM agent_spend WHERE run_id = :r"
                        " AND kind = 'settle' GROUP BY task"
                    ),
                    {"r": run},
                )
            }
            planned = [
                {"planned_usd": r[0], "planned_judge_usd": r[1]}
                for r in await conn.execute(
                    text(
                        "SELECT report ->> 'planned_usd', report ->> 'planned_judge_usd' FROM compile_steps"
                        " WHERE run_id = :r AND report ? 'planned_usd' ORDER BY step"
                    ),
                    {"r": run},
                )
            ]
        return {
            "wall_s": round(time.monotonic() - started, 1),
            "effective_task_budgets": budgets,
            "spent_by_task": by_task,
            "loop_plans": planned,
            "loops": status["loops"],
            "stop": status["stop"],
            "refused_limit": status["refused_limit"],
            "run_id": str(run),
            "state": status["state"],
            "refusals": status["refusals"],
            "spent_usd": status["spent_usd"],
            "cap_usd": status["cap_usd"],
            "settled_usd": str(settled),
            "sdk_usd": str(sdk),
            "steps": [s["stage"] for s in status["steps"]],
        }
    finally:
        await queue_app.close_async()
        await engine.dispose()


def main() -> int:
    if len(sys.argv) != 2:
        sys.stderr.write("usage: python -I -m reader.pipeline.live <plan lesson id>\n")
        return 2
    sys.stdout.write(json.dumps(asyncio.run(_run(sys.argv[1]))) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
