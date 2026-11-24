"""SRV-PIPELINE FR-PIPELINE-20: `compile_runs.cancelled_at`, the owner's cancel of an open run.

Revision ID: pipeline_0004
Revises: pipeline_0003 (same branch `pipeline`).
"""

from __future__ import annotations

from alembic import op

revision = "pipeline_0004"
down_revision = "pipeline_0003"
branch_labels = None
depends_on = None

# IF NOT EXISTS: idempotent like pipeline_0005, which also adds the column for a database that applied the old
# `pipeline_0004` (the `ends` revision under this id); this revision touches only `cancelled_at`.
UPGRADE = ("ALTER TABLE compile_runs ADD COLUMN IF NOT EXISTS cancelled_at timestamptz NULL",)
DOWNGRADE = ("ALTER TABLE compile_runs DROP COLUMN cancelled_at",)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
