"""The worker-container runtime of the ingest exec commands: config, engine and the job queue (SRV-INGEST).

`python -I -m reader.ingest.receive|status|requeue` run inside the worker container (its default uid
10001), with the same environment, config and database password as `reader-server --role worker`. Every
failure prints one JSON line `{"error": <code>}` and exits 1; nothing printed carries a value or any text.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from reader.config import ConfigError, ReaderConfig, load_config
from reader.db import make_engine
from reader.ingest.errors import Cancel, IngestError
from reader.jobs.catalog import load_kinds
from reader.jobs.queue import Jobs
from reader.jobs.worker import make_app


@dataclass
class Runtime:
    config: ReaderConfig
    engine: AsyncEngine
    jobs: Jobs


@asynccontextmanager
async def runtime() -> AsyncIterator[Runtime]:
    from reader.serve.main import Env, Failure, _target

    try:
        env = Env.read(os.environ)
        config = load_config(env.config)
        target = _target(env)
    except (Failure, ConfigError):
        raise IngestError("runtime_unavailable") from None
    engine = make_engine(target)
    app = make_app(target)
    try:
        await app.open_async()
    except Exception:
        await engine.dispose()
        raise IngestError("database_unreachable") from None
    try:
        yield Runtime(config, engine, Jobs(engine, app, load_kinds(), [target.password.reveal()]))
    finally:
        await app.close_async()
        await engine.dispose()


def emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def run_command(work: Callable[[Runtime], Awaitable[dict[str, Any]]]) -> int:
    async def main() -> dict[str, Any]:
        async with runtime() as rt:
            return await work(rt)

    try:
        emit(asyncio.run(main()))
    except Cancel:  # FR-INGEST-20's one cancel shape (receive under a claim, FR-INGEST-23)
        sys.stdout.write('{"cancel":true}\n')
        sys.stdout.flush()
        return 3
    except IngestError as error:
        emit({"error": error.code})
        return 1
    except Exception:  # driver or IO detail is never printed
        emit({"error": "unavailable"})
        return 1
    return 0
