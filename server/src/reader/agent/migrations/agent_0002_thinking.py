"""SRV-AGENT migration: `agent_spend.thinking` (settle rows: the SDK's output_tokens_details.thinking_tokens).

Additive and nullable: existing rows keep null (not recorded). Paired with SRV-TUTOR's bench (NFR-TUTOR-1).

Revision ID: agent_0002
Revises: agent_0001
"""

from __future__ import annotations

from alembic import op

revision = "agent_0002"
down_revision = "agent_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE agent_spend ADD COLUMN thinking bigint NULL CHECK (thinking >= 0)")


def downgrade() -> None:
    op.execute("ALTER TABLE agent_spend DROP COLUMN thinking")
