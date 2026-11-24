"""SRV-PIPELINE FR-PIPELINE-16: `compile_runs.stage_error`, {step, input_sha256, code} of a stopped step.

Revision ID: pipeline_0003
Revises: pipeline_0002 (same branch `pipeline`).
"""

from __future__ import annotations

from alembic import op

revision = "pipeline_0003"
down_revision = "pipeline_0002"
branch_labels = None
depends_on = None

UPGRADE = ("ALTER TABLE compile_runs ADD COLUMN stage_error jsonb NULL",)
DOWNGRADE = ("ALTER TABLE compile_runs DROP COLUMN stage_error",)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
