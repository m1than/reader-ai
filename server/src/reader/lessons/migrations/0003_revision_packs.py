"""SRV-LESSON FR-LESSON-32: the packs a revision cites (`revision_packs`), append-only (INV-LESSON-6).

Revision ID: lessons_0003
Revises: lessons_0002 (same branch `lessons`).

No backfill: revisions stored before this migration hold legacy span ids only, which need no row (SRV-INGEST
FR-INGEST-49). `pack_id` has no foreign key: base packs are SRV-INGEST's table (INV-INGEST-16).
"""

from __future__ import annotations

from alembic import op

revision = "lessons_0003"
down_revision = "lessons_0002"
branch_labels = None
depends_on = None

# One statement per op.execute: asyncpg runs prepared statements, which hold a single command.
UPGRADE = (
    """CREATE TABLE revision_packs (
        owner_id uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
        lesson_id uuid NOT NULL,
        sha256 text NOT NULL,
        pack_id text NOT NULL CHECK (pack_id ~ '^[0-9a-f]{64}$'),
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (lesson_id, sha256, pack_id),
        CONSTRAINT revision_packs_revision FOREIGN KEY (lesson_id, sha256)
            REFERENCES lesson_revisions (lesson_id, sha256) ON DELETE RESTRICT,
        CONSTRAINT revision_packs_lesson_owner FOREIGN KEY (lesson_id, owner_id)
            REFERENCES lessons (id, owner_id) ON DELETE RESTRICT
    )""",
    """CREATE TRIGGER revision_packs_immutable BEFORE UPDATE OR DELETE ON revision_packs
        FOR EACH ROW EXECUTE FUNCTION lessons_immutable()""",
    """CREATE TRIGGER revision_packs_no_truncate BEFORE TRUNCATE ON revision_packs
        FOR EACH STATEMENT EXECUTE FUNCTION lessons_immutable()""",
)

DOWNGRADE = ("DROP TABLE revision_packs",)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
