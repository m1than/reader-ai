"""SRV-PIPELINE FR-PIPELINE-18 (paired SRV-OBS phase 2): `compile_runs.ends`, the run's count of recorded ends
(finished, failed or dead); the `end` of each `obs.runs` enqueue. A resumed run that ends again counts 2.

Revision ID: pipeline_0005
Revises: pipeline_0004 (same branch `pipeline`; renumbered at the merge of V/manage, whose pipeline_0004 adds
`cancelled_at`: two revisions with one id left the branch with an undefined order).

Convergent (V/manage-fix2): a database that applied the old `pipeline_0004` (the `ends` revision under that id)
carries `ends` and no `cancelled_at`, and alembic runs only this revision on it. So every statement is idempotent:
both columns are added if missing, and the CHECK is added under the name Postgres gave the old inline CHECK
(`compile_runs_ends_check`) only if no constraint of that name exists on the table.
"""

from __future__ import annotations

from alembic import op

revision = "pipeline_0005"
down_revision = "pipeline_0004"
branch_labels = None
depends_on = None

UPGRADE = (
    "ALTER TABLE compile_runs ADD COLUMN IF NOT EXISTS cancelled_at timestamptz NULL",
    "ALTER TABLE compile_runs ADD COLUMN IF NOT EXISTS ends integer NOT NULL DEFAULT 0",
    "DO $$ BEGIN"
    " IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'compile_runs_ends_check'"
    " AND conrelid = 'compile_runs'::regclass) THEN"
    " ALTER TABLE compile_runs ADD CONSTRAINT compile_runs_ends_check CHECK (ends BETWEEN 0 AND 100);"
    " END IF; END $$",
)
# `cancelled_at` belongs to pipeline_0004, whose downgrade drops it.
DOWNGRADE = ("ALTER TABLE compile_runs DROP COLUMN ends",)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
