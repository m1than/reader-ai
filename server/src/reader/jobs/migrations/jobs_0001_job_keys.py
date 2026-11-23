"""SRV-EVENTS FR-EVENTS-11: the per-key job table, after procrastinate's own schema (its shipped chain)."""

from __future__ import annotations

from alembic import op

revision = "jobs_0001"
down_revision: str | None = None
branch_labels = ("jobs",)
depends_on = "procrastinate_0043"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE job_keys (
            kind text NOT NULL,
            key text NOT NULL,
            args jsonb NOT NULL,
            attempts integer NOT NULL DEFAULT 0,
            outcome text NULL CHECK (outcome IN ('succeeded', 'dead')),
            error_code text NULL,
            job_id bigint NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz NULL,
            PRIMARY KEY (kind, key)
        )
        """
    )
    op.execute("CREATE INDEX job_keys_open_updated_idx ON job_keys (updated_at) WHERE outcome IS NULL")


def downgrade() -> None:
    op.execute("DROP TABLE job_keys")
