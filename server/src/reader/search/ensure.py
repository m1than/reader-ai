"""`python -I -m reader.search.ensure <material id or sha256>` (SRV-SEARCH FR-SEARCH-9), run in the worker.

Prints the resulting status (none | queued | running | ready | dead) or `no_ingest` (exit 1). Uses the
worker's environment: READER_CONFIG and the READER_DB_* variables of SRV-AUTH, the password from
/run/secrets.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys

from reader.config import load_config
from reader.db import make_engine
from reader.jobs.catalog import Catalog
from reader.jobs.queue import Jobs
from reader.jobs.worker import make_app
from reader.search.jobs import JOB_KINDS, ensure
from reader.serve.main import Env, _target

SHA = re.compile(r"^[0-9a-f]{64}$")


async def _run(material: str, env: Env) -> str:
    config = load_config(env.config)
    if not SHA.fullmatch(material):
        found = next((m.sha256 for m in config.materials if m.id == material), None)
        if found is None:
            return "no_ingest"
        material = found
    owner = next(u.handle for u in config.users if u.role == "owner")
    target = _target(env)
    engine = make_engine(target)
    app = make_app(target)
    await app.open_async()
    try:
        from sqlalchemy import text

        async with engine.connect() as conn:
            owner_id = (
                await conn.execute(text("SELECT id FROM users WHERE handle = :h"), {"h": owner})
            ).scalar_one()
        return await ensure(Jobs(engine, app, Catalog(JOB_KINDS)), owner_id, material)
    finally:
        await app.close_async()
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -I -m reader.search.ensure <material id or sha256>", file=sys.stderr)
        return 2
    status = asyncio.run(_run(args[0], Env.read(os.environ)))
    print(status)
    return 1 if status == "no_ingest" else 0


if __name__ == "__main__":
    sys.exit(main())
