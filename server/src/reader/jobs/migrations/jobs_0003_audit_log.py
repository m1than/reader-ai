"""SRV-EVENTS FR-EVENTS-30: the append-only audit table of the management layer (system table, no owner)."""

from __future__ import annotations

from alembic import op

revision = "jobs_0003"
down_revision: str | None = "jobs_0002"
branch_labels = None
depends_on = None

# One statement per op.execute: asyncpg runs prepared statements, which hold a single command.
UPGRADE = (
    """CREATE TABLE audit_log (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        at timestamptz NOT NULL DEFAULT now(),
        actor_id uuid NOT NULL,
        action text NOT NULL,
        object_kind text NOT NULL,
        object_id text NOT NULL,
        detail jsonb NOT NULL DEFAULT '{}'::jsonb
    )""",
    """CREATE FUNCTION audit_log_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'audit_log is append-only';
    END
    $$""",
    """CREATE TRIGGER audit_log_no_change BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION audit_log_append_only()""",
    """CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
        FOR EACH STATEMENT EXECUTE FUNCTION audit_log_append_only()""",
)
DOWNGRADE = ("DROP TABLE audit_log", "DROP FUNCTION audit_log_append_only()")


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
