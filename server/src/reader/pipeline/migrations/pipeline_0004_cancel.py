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

UPGRADE = ("ALTER TABLE compile_runs ADD COLUMN cancelled_at timestamptz NULL",)
DOWNGRADE = ("ALTER TABLE compile_runs DROP COLUMN cancelled_at",)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
