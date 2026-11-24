"""SRV-PIPELINE FR-PIPELINE-18 (paired SRV-OBS phase 2): `compile_runs.ends`, the run's count of recorded ends
(finished, failed or dead); the `end` of each `obs.runs` enqueue. A resumed run that ends again counts 2.

Revision ID: pipeline_0005
Revises: pipeline_0004 (same branch `pipeline`; renumbered at the merge of V/manage, whose pipeline_0004 adds
`cancelled_at`: two revisions with one id left the branch with an undefined order).
"""

from __future__ import annotations

from alembic import op

revision = "pipeline_0005"
down_revision = "pipeline_0004"
branch_labels = None
depends_on = None

UPGRADE = (
    "ALTER TABLE compile_runs ADD COLUMN ends integer NOT NULL DEFAULT 0 CHECK (ends BETWEEN 0 AND 100)",
)
DOWNGRADE = ("ALTER TABLE compile_runs DROP COLUMN ends",)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
