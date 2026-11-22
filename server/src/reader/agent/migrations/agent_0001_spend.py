"""SRV-AGENT migration: the append-only spend ledger `agent_spend` (FR-AGENT-9).

Revision ID: agent_0001
Revises: 0001_auth (a branch off SRV-AUTH's base; it needs users.id)
"""

from __future__ import annotations

from alembic import op

revision = "agent_0001"
down_revision = "0001_auth"
branch_labels = ("agent",)
depends_on = None

# One statement per op.execute: asyncpg runs prepared statements, which hold a single command.
UPGRADE = (
    """CREATE TABLE agent_spend (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        owner_id uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
        run_id uuid NOT NULL,
        lesson_id uuid NULL,
        stage text NOT NULL,
        task text NOT NULL,
        attempt integer NOT NULL CHECK (attempt >= 0),
        kind text NOT NULL CHECK (kind IN ('reserve', 'settle', 'void')),
        reservation_id bigint NULL REFERENCES agent_spend (id) ON DELETE RESTRICT,
        model text NOT NULL,
        usd numeric(12, 6) NOT NULL CHECK (usd >= 0),
        sdk_usd numeric(12, 6) NULL,
        settled_usd numeric(12, 6) NULL CHECK (settled_usd >= 0),
        upstream jsonb NULL,
        input bigint NULL,
        output bigint NULL,
        cache_write_5m bigint NULL,
        cache_write_1h bigint NULL,
        cache_read bigint NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        CHECK ((kind = 'reserve') = (reservation_id IS NULL)),
        CHECK ((kind = 'settle') = (settled_usd IS NOT NULL))
    )""",
    # One settlement or one void per reservation, never both.
    "CREATE UNIQUE INDEX agent_spend_closed_once ON agent_spend (reservation_id)"
    " WHERE reservation_id IS NOT NULL",
    "CREATE INDEX agent_spend_run ON agent_spend (run_id, task)",
    "CREATE INDEX agent_spend_owner_time ON agent_spend (owner_id, created_at)",
    """CREATE FUNCTION agent_spend_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'agent_spend is append-only';
    END
    $$""",
    """CREATE TRIGGER agent_spend_no_change BEFORE UPDATE OR DELETE ON agent_spend
        FOR EACH ROW EXECUTE FUNCTION agent_spend_append_only()""",
    """CREATE TRIGGER agent_spend_no_truncate BEFORE TRUNCATE ON agent_spend
        FOR EACH STATEMENT EXECUTE FUNCTION agent_spend_append_only()""",
)

DOWNGRADE = ("DROP TABLE agent_spend", "DROP FUNCTION agent_spend_append_only()")


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
