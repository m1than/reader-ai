"""A real judge-lane worker process for SRV-EVENTS's killed-lane test: `reader.jobs.worker.run(lane="judge")` with the
stub judge kind until killed. Reads the database target as JSON on stdin and prints "started"."""

from __future__ import annotations

import asyncio
import json
import sys

from conftest import effect_kind, judge_stub_kind


async def main() -> None:
    from reader.config import Secret
    from reader.db import DbTarget, make_engine
    from reader.jobs import Catalog
    from reader.jobs.worker import run

    spec = json.loads(sys.stdin.readline())
    target = DbTarget(spec["host"], spec["port"], spec["user"], spec["database"], Secret(spec["password"]))
    engine = make_engine(target)
    print("started", flush=True)
    catalog = Catalog([effect_kind(), judge_stub_kind()])
    await run(engine, target, asyncio.Event(), catalog, hooks=(), lane="judge")


if __name__ == "__main__":
    asyncio.run(main())
