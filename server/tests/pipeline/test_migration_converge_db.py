# @trace FR-PIPELINE-20, FR-PIPELINE-18
"""pipeline_0005 converges from either pre-state stamped `pipeline_0004`. [db]

Before V/manage-fix, two revisions claimed the id `pipeline_0004`: V/manage's (`cancelled_at`) and SRV-OBS phase 2's
(`ends`). A database migrated on either side is stamped `pipeline_0004` with only one of the two columns; an upgrade
to heads runs only pipeline_0005 on it and must leave both columns, the `ends` CHECK and the schema at heads.
"""

from __future__ import annotations

import pytest

from .conftest import Pg, target

pytestmark = pytest.mark.anyio

COLUMNS = (
    "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns"
    " WHERE table_name = 'compile_runs' AND column_name IN ('cancelled_at', 'ends') ORDER BY column_name"
)
CHECKS = (
    "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint"
    " WHERE conrelid = 'compile_runs'::regclass AND contype = 'c' AND pg_get_constraintdef(oid) LIKE '%ends%'"
)
PRE_STATES = {
    # the old `pipeline_0004_ends` applied: `ends` with its inline CHECK, no `cancelled_at`
    "old_ends": "ALTER TABLE compile_runs DROP COLUMN cancelled_at",
    # V/manage's `pipeline_0004_cancel` applied: `cancelled_at`, no `ends`
    "cancel": "ALTER TABLE compile_runs DROP COLUMN ends",
}


@pytest.mark.parametrize("pre_state", sorted(PRE_STATES))
async def test_pipeline_0005_converges_from_either_pipeline_0004(pg: Pg, pre_state: str) -> None:
    from sqlalchemy import text

    from reader.db import at_heads, make_engine, migrate

    engine = make_engine(target(pg))
    await migrate(engine)
    async with engine.connect() as conn:
        columns = (await conn.execute(text(COLUMNS))).all()
        checks = (await conn.execute(text(CHECKS))).all()
    assert [c[0] for c in columns] == ["cancelled_at", "ends"]
    assert [c[0] for c in checks] == ["compile_runs_ends_check"]

    async with engine.begin() as conn:
        await conn.execute(text(PRE_STATES[pre_state]))
        # rewind the later head too (pipeline_0006, FR-PIPELINE-20 revised: order_version) to the pre-state
        await conn.execute(text("ALTER TABLE compile_runs DROP COLUMN order_version"))
        await conn.execute(
            text("UPDATE alembic_version SET version_num = 'pipeline_0004' WHERE version_num = 'pipeline_0006'")
        )
        left = (await conn.execute(text(COLUMNS))).all()
    assert len(left) == 1
    assert await at_heads(engine) is False

    await migrate(engine)

    assert await at_heads(engine) is True
    async with engine.connect() as conn:
        assert (await conn.execute(text(COLUMNS))).all() == columns
        assert (await conn.execute(text(CHECKS))).all() == checks
    await engine.dispose()
