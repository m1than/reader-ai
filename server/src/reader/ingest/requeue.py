"""`python -I -m reader.ingest.requeue --material-sha256 <sha>`: requeue a dead import (FR-INGEST-12).

Calls SRV-EVENTS `requeue_dead` for the material's key; the stored artifact is imported again, nothing is
converted.
"""

from __future__ import annotations

import sys
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncEngine

from reader.ingest.jobs import KIND, key_for
from reader.ingest.status import current_version, sha_argument


class Requeuer(Protocol):
    async def requeue_dead(self, kind_name: str, key: str) -> Any: ...


async def requeue(engine: AsyncEngine, jobs: Requeuer, material_sha256: str) -> dict[str, Any]:
    version = await current_version(engine, material_sha256)
    result = await jobs.requeue_dead(KIND, key_for(material_sha256, version))
    return {"result": str(result.kind), "converter_version": version}


def main(argv: list[str] | None = None) -> int:
    from reader.ingest.runtime import run_command

    sha = sha_argument(argv, "reader.ingest.requeue")

    async def work(rt: Any) -> dict[str, Any]:
        return await requeue(rt.engine, rt.jobs, sha)

    return run_command(work)


if __name__ == "__main__":
    sys.exit(main())
