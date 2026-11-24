"""SRV-AGENT migration: `agent_spend.web_search_requests` (tool calls' settle rows: searches, FR-AGENT-17).

Additive and nullable: existing rows and tool-less calls keep null. ADR-0030.

Revision ID: agent_0003
Revises: agent_0002
"""

from __future__ import annotations

from alembic import op

revision = "agent_0003"
down_revision = "agent_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agent_spend ADD COLUMN web_search_requests int NULL CHECK (web_search_requests >= 0)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agent_spend DROP COLUMN web_search_requests")
