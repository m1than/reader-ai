"""SRV-AGENT migration: ledger lines and recorded judge outputs (ADR-0039; FR-AGENT-19, FR-AGENT-20).

`agent_spend.line` is additive and nullable (rows written before it keep null). `judge_outputs` is keyed per
owner; its trigger refuses UPDATE and TRUNCATE, and DELETE unless the transaction-local setting
`reader.judge_delete` is `on`, which only `reader.agent.judges`' expire and forget functions set.

Revision ID: agent_0004
Revises: agent_0003
"""

from __future__ import annotations

from alembic import op

revision = "agent_0004"
down_revision = "agent_0003"
branch_labels = None
depends_on = None

UPGRADE = (
    "ALTER TABLE agent_spend ADD COLUMN line text NULL"
    " CHECK (line IN ('compile', 'tutor', 'freshness', 'mcp', 'index', 'eval'))",
    """CREATE TABLE judge_outputs (
        owner_id uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
        input_sha256 text NOT NULL,
        judge text NOT NULL,
        output jsonb NOT NULL,
        pack_ids text[] NOT NULL DEFAULT '{}',
        overlay_id text NULL,
        request_id uuid NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (owner_id, input_sha256, judge)
    )""",
    "CREATE INDEX judge_outputs_created ON judge_outputs (created_at)",
    """CREATE FUNCTION judge_outputs_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'DELETE' AND current_setting('reader.judge_delete', true) = 'on' THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'judge_outputs: % refused', TG_OP;
    END $$""",
    """CREATE TRIGGER judge_outputs_no_change BEFORE UPDATE OR DELETE ON judge_outputs
        FOR EACH ROW EXECUTE FUNCTION judge_outputs_guard()""",
    """CREATE TRIGGER judge_outputs_no_truncate BEFORE TRUNCATE ON judge_outputs
        FOR EACH STATEMENT EXECUTE FUNCTION judge_outputs_guard()""",
)
DOWNGRADE = (
    "DROP TABLE judge_outputs",
    "DROP FUNCTION judge_outputs_guard()",
    "ALTER TABLE agent_spend DROP COLUMN line",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
