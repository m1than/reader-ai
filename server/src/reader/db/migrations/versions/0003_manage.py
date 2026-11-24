"""SRV-AUTH manage migration (FR-AUTH-45): an optional label on each passkey.

Revision ID: 0003_manage
Revises: 0002_signup
"""

from __future__ import annotations

from alembic import op

revision = "0003_manage"
down_revision = "0002_signup"
branch_labels = None
depends_on = None

UPGRADE = (
    "ALTER TABLE webauthn_credentials ADD COLUMN label text NULL "
    "CONSTRAINT webauthn_credentials_label CHECK (char_length(label) BETWEEN 1 AND 64)",
)
DOWNGRADE = ("ALTER TABLE webauthn_credentials DROP COLUMN label",)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
