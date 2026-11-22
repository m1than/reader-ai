"""SRV-LESSON fixture loader, run as a subprocess against Postgres.

@trace FR-LESSON-13
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import F01, Pg, need, scalar, world

pytestmark = pytest.mark.anyio
F01_SHA = hashlib.sha256(F01).hexdigest()
LINE = re.compile(r"course=([0-9a-f-]{36}) lesson=([0-9a-f-]{36}) sha256=([0-9a-f]{64})\n")


def _load(pg: Pg, tmp: Path, handle: str) -> subprocess.CompletedProcess[str]:
    secrets_root = tmp / "secrets"
    secrets_root.mkdir(exist_ok=True)
    (secrets_root / "db-password").write_text(pg.password, encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp),
        "READER_DB_HOST": pg.host,
        "READER_DB_PORT": str(pg.port),
        "READER_DB_USER": pg.user,
        "READER_DB_NAME": pg.database,
        "READER_SECRETS_ROOT": str(secrets_root),
    }
    argv = [sys.executable, "-m", "reader.lessons.fixture", "load", "--handle", handle]
    return subprocess.run(  # noqa: S603 — the module under test, fixed argv
        [*argv, "--blobs-root", str(tmp / "blobs")],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )


async def test_load_is_idempotent(pg: Pg, tmp_path: Path) -> None:
    need("reader.lessons.fixture")
    w = await world(pg, tmp_path)
    first = _load(pg, tmp_path, "ivan")
    assert first.returncode == 0, first.stderr
    match = LINE.fullmatch(first.stdout)
    assert match is not None, first.stdout
    assert match.group(3) == F01_SHA
    second = _load(pg, tmp_path, "ivan")
    assert (second.returncode, second.stdout) == (0, first.stdout)
    assert await scalar(w.engine, "SELECT count(*) FROM courses") == 1
    assert await scalar(w.engine, "SELECT count(*) FROM lessons") == 1
    assert await scalar(w.engine, "SELECT count(*) FROM lesson_revisions") == 1
    assert await scalar(w.engine, "SELECT count(*) FROM lesson_revision_stages WHERE stage = 'fixture'") == 2
    assert await scalar(w.engine, "SELECT title FROM courses") == "Fixture course (synthetic)"
    assert await scalar(w.engine, "SELECT title FROM lessons") == "F01 — fixture lesson (synthetic)"
    assert (tmp_path / "blobs" / "sha256" / F01_SHA[:2] / F01_SHA).read_bytes() == F01
    await w.engine.dispose()


async def test_unknown_and_disabled_users(pg: Pg, tmp_path: Path) -> None:
    need("reader.lessons.fixture")
    w = await world(pg, tmp_path)
    unknown = _load(pg, tmp_path, "nobody")
    assert (unknown.returncode, unknown.stdout, unknown.stderr) == (1, "", "reader-lessons: unknown_handle\n")
    from sqlalchemy import text

    async with w.engine.begin() as conn:
        await conn.execute(text("UPDATE users SET disabled_at = now() WHERE handle = 'anna'"))
    disabled = _load(pg, tmp_path, "anna")
    expected = (1, "", "reader-lessons: user_disabled\n")
    assert (disabled.returncode, disabled.stdout, disabled.stderr) == expected
    assert await scalar(w.engine, "SELECT count(*) FROM courses") == 0
    await w.engine.dispose()


def test_database_unreachable(tmp_path: Path) -> None:
    need("reader.lessons.fixture")
    unreachable = Pg("127.0.0.1", 1, "postgres", "canary-pw-91c2", "postgres")
    result = _load(unreachable, tmp_path, "ivan")
    assert (result.returncode, result.stdout) == (1, "")
    assert result.stderr == "reader-lessons: database_unreachable\n"
    assert "canary-pw-91c2" not in result.stderr
