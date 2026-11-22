"""Fixture loader (SRV-LESSON FR-LESSON-13).

Usage: `python -m reader.lessons.fixture load --handle <h> [--blobs-root DIR]`.

Puts the synthetic fixture lesson F01 into a user's account; idempotent. The database is reached exactly as
`reader-server` reaches it (SRV-AUTH FR-AUTH-1). Failures print one fixed line and exit 1.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path
from typing import Final

from sqlalchemy import text

from reader.db import make_engine
from reader.lessons import store
from reader.serve.main import Env, _target

COURSE_TITLE: Final = "Fixture course (synthetic)"
LESSON_TITLE: Final = "F01 — fixture lesson (synthetic)"
F01: Final = Path(__file__).parent / "fixtures" / "F01.ipynb"
BLOBS_ROOT: Final = Path("/var/lib/reader/blobs")


class _Refused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def _load(handle: str, blobs_root: Path) -> str:
    engine = make_engine(_target(Env.read(os.environ)))
    try:
        async with engine.begin() as conn:
            user = (
                await conn.execute(text("SELECT id, disabled_at FROM users WHERE handle = :h"), {"h": handle})
            ).first()
            if user is None:
                raise _Refused("unknown_handle")
            if user[1] is not None:
                raise _Refused("user_disabled")
            owner: uuid.UUID = user[0]
            course = (
                await conn.execute(
                    text(
                        "SELECT id FROM courses"
                        " WHERE owner_id = :o AND title = :t AND material_sha256 IS NULL"
                    ),
                    {"o": owner, "t": COURSE_TITLE},
                )
            ).scalar()
            if course is None:
                course = await store.create_course(conn, owner, COURSE_TITLE, None)
            lesson = await store.ensure_lesson(conn, owner, course, 1, LESSON_TITLE)
            sha256, _ = await store.write_revision(
                conn, blobs_root, owner, lesson, F01.read_bytes(), "fixture", {}
            )
        return f"course={course} lesson={lesson} sha256={sha256}\n"
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m reader.lessons.fixture")
    commands = parser.add_subparsers(dest="command", required=True)
    load = commands.add_parser("load")
    load.add_argument("--handle", required=True)
    load.add_argument("--blobs-root", type=Path, default=BLOBS_ROOT)
    args = parser.parse_args(argv)
    try:
        line = asyncio.run(_load(args.handle, args.blobs_root))
    except _Refused as refused:
        sys.stderr.write(f"reader-lessons: {refused.code}\n")
        return 1
    except Exception:
        # Any database or secret failure: one fixed line, no detail (it could name the endpoint).
        sys.stderr.write("reader-lessons: database_unreachable\n")
        return 1
    sys.stdout.write(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
