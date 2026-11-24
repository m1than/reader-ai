"""SRV-INGEST FR-INGEST-30..32 (V/manage): a material's display title override, its tombstone and its version.

Revision ID: ingest_0003 (branch `ingest`), down_revision ingest_0002. Delete is soft (no purge): rows and
files stay; `deleted_at` hides the material until its restore.
"""

from __future__ import annotations

from alembic import op

revision = "ingest_0003"
down_revision: str | None = "ingest_0002"
branch_labels = None
depends_on = None

UPGRADE = (
    "ALTER TABLE materials ADD COLUMN title text NULL CHECK (char_length(title) BETWEEN 1 AND 200)",
    "ALTER TABLE materials ADD COLUMN deleted_at timestamptz NULL",
    "ALTER TABLE materials ADD COLUMN version integer NOT NULL DEFAULT 1 CHECK (version >= 1)",
)

DOWNGRADE = ("ALTER TABLE materials DROP COLUMN version, DROP COLUMN deleted_at, DROP COLUMN title",)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
