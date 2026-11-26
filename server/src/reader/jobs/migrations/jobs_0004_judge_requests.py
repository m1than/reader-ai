"""SRV-EVENTS FR-EVENTS-33: `judge_requests`, the only place a `judge.find` query and its card text live
(owner data, deleted with the user, at the job's end, or by the collectors past the deadline)."""

from __future__ import annotations

from alembic import op

revision = "jobs_0004"
down_revision: str | None = "jobs_0003"
branch_labels = None
depends_on = "0001_auth"  # SRV-AUTH's revision that creates `users`

# One statement per op.execute: asyncpg runs prepared statements, which hold a single command.
UPGRADE = (
    """CREATE TABLE judge_requests (
        request_id uuid PRIMARY KEY,
        owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        line text NOT NULL CHECK (line IN ('tutor', 'compile', 'mcp')),
        run_id uuid NOT NULL,
        run_cap_usd numeric(12,6) NOT NULL CHECK (run_cap_usd > 0),
        lesson_id uuid NULL,
        daily_budget_usd numeric(12,6) NULL,
        input_sha256 text NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
        created_at timestamptz NOT NULL DEFAULT now(),
        deadline timestamptz NOT NULL,
        query text NOT NULL,
        cards jsonb NOT NULL,
        pack_ids text[] NOT NULL,
        CHECK (deadline > created_at)
    )""",
    "CREATE INDEX judge_requests_deadline ON judge_requests (deadline)",
)
DOWNGRADE = ("DROP TABLE judge_requests",)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
