"""SRV-EVENTS FR-EVENTS-18: the index behind `recent_keys_of` (kind, outcome, finished_at)."""

from __future__ import annotations

from alembic import op

revision = "jobs_0002"
down_revision: str | None = "jobs_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE INDEX job_keys_kind_outcome_finished ON job_keys (kind, outcome, finished_at)")


def downgrade() -> None:
    op.execute("DROP INDEX job_keys_kind_outcome_finished")
