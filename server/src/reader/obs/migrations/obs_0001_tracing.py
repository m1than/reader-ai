"""SRV-OBS FR-OBS-10: one row per worker start, saying whether tracing was on, so the owner's views link only
traces that can exist (jobs that ran before tracing, or while it was off, have none).

Backfill: a stack that ran SRV-OBS before this table existed has traced jobs since its first worker start
with the obs code, which enqueued that day's `obs.retention` key. One row (that key row's created_at,
enabled) keeps their links; it assumes tracing was on then (the default; declared in FR-OBS-10). A stack
without such a key gets no row, so nothing before its first recorded start is linked.
"""

from __future__ import annotations

from alembic import op

revision = "obs_0001"
down_revision: str | None = None
branch_labels = ("obs",)
depends_on = "jobs_0001"  # the backfill reads job_keys

BACKFILL = (
    "INSERT INTO obs_tracing (started_at, enabled)"
    " SELECT min(created_at), true FROM job_keys WHERE kind = 'obs.retention'"
    " HAVING min(created_at) IS NOT NULL"
)


def upgrade() -> None:
    op.execute("CREATE TABLE obs_tracing (started_at timestamptz PRIMARY KEY, enabled boolean NOT NULL)")
    op.execute(BACKFILL)


def downgrade() -> None:
    op.execute("DROP TABLE obs_tracing")
