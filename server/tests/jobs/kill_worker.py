"""A real worker process for SRV-EVENTS's kill test: `reader.jobs.worker.run` with the test kind until killed.

Reads the database target as JSON on stdin (the password never goes into argv) and prints "started".
"""

from __future__ import annotations

import asyncio
import json
import sys

from conftest import effect_kind


async def main() -> None:
    from reader.config import Secret
    from reader.db import DbTarget, make_engine
    from reader.jobs import Catalog
    from reader.jobs.worker import run

    spec = json.loads(sys.stdin.readline())
    target = DbTarget(spec["host"], spec["port"], spec["user"], spec["database"], Secret(spec["password"]))
    engine = make_engine(target)
    print("started", flush=True)
    await run(engine, target, asyncio.Event(), Catalog([effect_kind(timeout_s=600.0)]))


if __name__ == "__main__":
    asyncio.run(main())
