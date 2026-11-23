"""SRV-PIPELINE FR-PIPELINE-14: `course_plans.origin` (manifest | study_plan).

Revision ID: pipeline_0002
Revises: pipeline_0001 (same branch `pipeline`).
"""

from __future__ import annotations

from alembic import op

revision = "pipeline_0002"
down_revision = "pipeline_0001"
branch_labels = None
depends_on = None

UPGRADE = (
    "ALTER TABLE course_plans ADD COLUMN origin text NOT NULL DEFAULT 'manifest'"
    " CHECK (origin IN ('manifest', 'study_plan'))",
)
DOWNGRADE = ("ALTER TABLE course_plans DROP COLUMN origin",)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
