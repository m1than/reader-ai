"""SRV-OBS FR-OBS-10: one row per worker start, saying whether tracing was on, so the owner's views link only
traces that can exist (jobs that ran before tracing, or while it was off, have none)."""

from __future__ import annotations

from alembic import op

revision = "obs_0001"
down_revision: str | None = None
branch_labels = ("obs",)
depends_on = None


def upgrade() -> None:
    op.execute("CREATE TABLE obs_tracing (started_at timestamptz PRIMARY KEY, enabled boolean NOT NULL)")


def downgrade() -> None:
    op.execute("DROP TABLE obs_tracing")
