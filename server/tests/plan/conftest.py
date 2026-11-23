"""Fixtures and helpers for the SRV-PLAN tests (spec/modules/SRV-PLAN.yaml).

@trace VEC-PLAN-1, VEC-PLAN-2

Modules under test are imported through `need()` inside test bodies (Red phase: an assertion failure, not a
collection error). [db] tests reuse SRV-PIPELINE's environment (a migrated Postgres with users, the job machinery and
a fake provider, so no credit is spent) and add a synthetic material of their own to SRV-INGEST's tables: page
characters and an outline whose titles all carry a canary word (INV-PLAN-4). No book text.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from pipeline.conftest import (  # noqa: F401  (session fixtures re-exported for this package)
    ORIGIN,
    SESSION_COOKIE,
    Env,
    Pg,
    need,
    pg,
    pg_container,
    pipeline_env,
    session_cookie,
)

REPO = Path(__file__).resolve().parents[3]
FIXTURES = REPO / "spec" / "fixtures" / "SRV-PLAN"
SPEC = REPO / "spec" / "modules" / "SRV-PLAN.yaml"
PLAN_SHA = "cd" * 32
MATERIAL_ID = "synthetic-plan"
CANARY = "CANARY-TITLE-9f3e"
# 30 pages: 2,000 characters each except two pages without text (a figure page and a blank page).
PAGE_CHARS: tuple[int, ...] = tuple(0 if p in (5, 30) else 2000 for p in range(1, 31))
OUTLINE: tuple[tuple[int, int | None, str], ...] = (
    (1, 1, f"Chapter one {CANARY}"),
    (2, 4, f"Section one {CANARY}"),
    (1, 11, f"Chapter two {CANARY}"),
    (2, None, f"Unplaced {CANARY}"),
    (1, 21, f"Chapter three {CANARY}"),
)


def load_yaml(path: Path) -> Any:
    return YAML(typ="safe").load(path.read_text(encoding="utf-8"))


def spec_bounds() -> dict[str, Any]:
    data: dict[str, Any] = load_yaml(SPEC)["bounds"]
    return data


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def today() -> str:
    return dt.datetime.now(dt.UTC).date().isoformat()


async def import_plan_material(
    engine: Any,
    owner: uuid.UUID,
    page_chars: Sequence[int] = PAGE_CHARS,
    outline: Sequence[tuple[int, int | None, str]] = OUTLINE,
    sha: str = PLAN_SHA,
    config_id: str = MATERIAL_ID,
) -> uuid.UUID:
    """A material in SRV-INGEST's tables (ingest_0001) with one span per page that has text."""
    from sqlalchemy import text

    async with engine.begin() as conn:
        mid = (
            await conn.execute(
                text(
                    "INSERT INTO materials (owner_id, sha256, config_id, page_count) VALUES (:o, :s, :c, :n)"
                    " RETURNING id"
                ),
                {"o": owner, "s": sha, "c": config_id, "n": len(page_chars)},
            )
        ).scalar_one()
        spans = [(p, n) for p, n in enumerate(page_chars, start=1) if n > 0]
        iid: uuid.UUID = (
            await conn.execute(
                text(
                    "INSERT INTO ingests (owner_id, material_id, converter_version, artifact_id, span_count,"
                    " outline_count, outline_source, pages_without_text)"
                    " VALUES (:o, :m, 'v1', :a, :n, :k, 'bookmarks', '{}') RETURNING id"
                ),
                {"o": owner, "m": mid, "a": "0" * 64, "n": len(spans), "k": len(outline)},
            )
        ).scalar_one()
        for seq, (page, n) in enumerate(spans):
            await conn.execute(
                text(
                    "INSERT INTO material_spans (ingest_id, owner_id, seq, page, kind, text, x0, y0, x1, y1)"
                    " VALUES (:i, :o, :q, :p, 'text', :t, 0, 0, 0, 0)"
                ),
                {"i": iid, "o": owner, "q": seq, "p": page, "t": "x" * n},
            )
        for seq, (level, page, title) in enumerate(outline):
            await conn.execute(
                text(
                    "INSERT INTO material_outline (ingest_id, owner_id, seq, level, title, page)"
                    " VALUES (:i, :o, :q, :l, :t, :p)"
                ),
                {"i": iid, "o": owner, "q": seq, "l": level, "t": title, "p": page},
            )
    return iid


def materials() -> tuple[Any, ...]:
    from reader.config.model import Material

    return (
        Material(id=MATERIAL_ID, file="synthetic.pdf", sha256=PLAN_SHA, license="test", language="en"),
        Material(id="never-imported", file="other.pdf", sha256="ef" * 32, license="test", language="en"),
    )


@asynccontextmanager
async def plan_env(database: Pg, tmp: Path) -> AsyncIterator[Env]:
    async with pipeline_env(database, tmp) as env:
        await import_plan_material(env.engine, env.users["ivan"])
        # The pipeline's fake span reader serves spans by page; give it this material's pages so a planned
        # lesson can start a (fake, free) compile run.
        source = need("reader.pipeline.source")
        env.spans.spans = [
            source.Span(f"{PLAN_SHA}:{p}", p, "text", "x" * n) for p, n in enumerate(PAGE_CHARS, start=1) if n
        ]
        yield env


@asynccontextmanager
async def client(env: Env, handle: str | None = "ivan") -> AsyncIterator[Any]:
    import httpx

    from reader.api.app import create_app
    from reader.auth.policy import settings_from_config

    app = create_app(settings_from_config(env.config), env.engine, run_cleanup_task=False)
    app.state.blobs_root = env.blobs
    app.state.jobs = env.jobs
    app.state.ingest_materials = materials()
    cookies = {SESSION_COOKIE: await session_cookie(env, handle)} if handle else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers={"Origin": ORIGIN}, cookies=cookies
    ) as c:
        yield c


def body(weeks: int = 2, minutes: int = 60, **extra: Any) -> dict[str, Any]:
    return {
        "material_id": MATERIAL_ID,
        "total_weeks": weeks,
        "minutes_per_day": minutes,
        "today": today(),
        **extra,
    }
