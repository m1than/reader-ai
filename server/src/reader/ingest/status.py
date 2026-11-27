"""`python -I -m reader.ingest.status --material-sha256 <sha>`: a material's import status (FR-INGEST-12).

The key is (material_sha256, CONVERTER_VERSION) only (revision 3: no frozen version); a succeeded key whose
pack is collected or purged reports `collected` or `purged`.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncEngine

import reader.ingest as ingest
from reader.ingest import store
from reader.ingest.jobs import KIND, key_for


class StatusSource(Protocol):
    async def job_status(self, kind_name: str, key: str) -> str: ...


async def current_version(engine: AsyncEngine, material_sha256: str) -> str:
    del engine, material_sha256
    return ingest.CONVERTER_VERSION


async def status(engine: AsyncEngine, jobs: StatusSource, material_sha256: str) -> dict[str, Any]:
    version = await current_version(engine, material_sha256)
    state = await jobs.job_status(KIND, key_for(material_sha256, version))
    if state == "succeeded":
        async with engine.connect() as conn:
            gone = await store.pack_state(conn, material_sha256, version)
        state = state if gone == "live" else gone
    return {"status": state, "converter_version": version}


def sha_argument(argv: list[str] | None, prog: str) -> str:
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument("--material-sha256", required=True)
    value = str(parser.parse_args(argv).material_sha256)
    if not store_sha(value):
        parser.error("material sha256 must be 64 lowercase hex characters")
    return value


def store_sha(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def main(argv: list[str] | None = None) -> int:
    from reader.ingest.runtime import run_command

    sha = sha_argument(argv, "reader.ingest.status")

    async def work(rt: Any) -> dict[str, Any]:
        return await status(rt.engine, rt.jobs, sha)

    return run_command(work)


if __name__ == "__main__":
    sys.exit(main())
