"""SRV-CONFIG migration: `settings_overrides`, the owner's overlay on reader.yaml (FR-CONFIG-26, ADR-0036).

Revision ID: config_0001
Revises: none (its own branch `config`); depends on 0001_auth only for ordering, no foreign key: `updated_by`
names a user without blocking SRV-AUTH FR-AUTH-43's delete, as `audit_log.actor_id` does.
"""

from __future__ import annotations

from alembic import op

revision = "config_0001"
down_revision: str | None = None
branch_labels = ("config",)
depends_on = "0001_auth"

UPGRADE = (
    """CREATE TABLE settings_overrides (
        key text PRIMARY KEY,
        value jsonb NOT NULL,
        updated_by uuid NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now(),
        version integer NOT NULL CHECK (version >= 1)
    )""",
)
DOWNGRADE = ("DROP TABLE settings_overrides",)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
