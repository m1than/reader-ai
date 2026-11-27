"""SRV-PIPELINE FR-PIPELINE-20 revised: `compile_runs.order_version`, the check order a run was created under.

Existing rows get 1 (the approved order: draft → fact-check → review → one loop); new runs get 2 (ADR-0039's
order). The worker cancels open v1 runs at its start (`runs.cancel_legacy`).

Revision ID: pipeline_0006
Revises: pipeline_0005 (same branch `pipeline`).
"""

from __future__ import annotations

from alembic import op

revision = "pipeline_0006"
down_revision = "pipeline_0005"
branch_labels = None
depends_on = None

UPGRADE = (
    "ALTER TABLE compile_runs ADD COLUMN order_version int NOT NULL DEFAULT 1",
    "ALTER TABLE compile_runs ALTER COLUMN order_version SET DEFAULT 2",
)
DOWNGRADE = ("ALTER TABLE compile_runs DROP COLUMN order_version",)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
