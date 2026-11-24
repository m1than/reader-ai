"""SRV-LESSON FR-LESSON-20: course positions, tombstones and versions; the learner tables.

Revision ID: lessons_0002
Revises: lessons_0001 (same branch `lessons`).

Existing courses are ranked by `created_at` descending per owner (1 = newest, the list's former order). Every
new foreign key is ON DELETE RESTRICT and not deferrable (FR-LESSON-15's catalog check covers them).
"""

from __future__ import annotations

from alembic import op

revision = "lessons_0002"
down_revision = "lessons_0001"
branch_labels = None
depends_on = None

_LESSON_FK = "FOREIGN KEY (lesson_id, owner_id) REFERENCES lessons (id, owner_id) ON DELETE RESTRICT"
_OWNER = "owner_id uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT"
_USER = "user_id uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT"

# One statement per op.execute: asyncpg runs prepared statements, which hold a single command.
UPGRADE = (
    """ALTER TABLE courses
        ADD COLUMN description text NULL CHECK (char_length(description) <= 2000),
        ADD COLUMN position integer NULL CHECK (position >= 1),
        ADD COLUMN archived_at timestamptz NULL,
        ADD COLUMN deleted_at timestamptz NULL,
        ADD COLUMN deleted_by_material text NULL,
        ADD COLUMN version integer NOT NULL DEFAULT 1,
        ADD COLUMN order_source text NOT NULL DEFAULT 'plan' CHECK (order_source IN ('plan', 'manual')),
        ADD COLUMN last_position integer NULL""",
    """UPDATE courses SET position = r.n FROM (
        SELECT id, row_number() OVER (PARTITION BY owner_id ORDER BY created_at DESC, id) AS n FROM courses
    ) r WHERE courses.id = r.id""",
    """ALTER TABLE courses ADD CONSTRAINT courses_position_state
        CHECK ((archived_at IS NULL AND deleted_at IS NULL) = (position IS NOT NULL))""",
    "CREATE UNIQUE INDEX courses_owner_position ON courses (owner_id, position) WHERE position IS NOT NULL",
    "DROP INDEX courses_unsourced_title",
    """CREATE UNIQUE INDEX courses_unsourced_title ON courses (owner_id, title)
        WHERE material_sha256 IS NULL AND deleted_at IS NULL""",
    """ALTER TABLE lessons
        ADD COLUMN skipped_at timestamptz NULL,
        ADD COLUMN deleted_at timestamptz NULL,
        ADD COLUMN version integer NOT NULL DEFAULT 1""",
    f"""CREATE TABLE lesson_progress (
        {_OWNER},
        {_USER},
        lesson_id uuid NOT NULL,
        done_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (user_id, lesson_id),
        CONSTRAINT lesson_progress_lesson_owner {_LESSON_FK}
    )""",
    f"""CREATE TABLE lesson_notes (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        {_OWNER},
        {_USER},
        lesson_id uuid NOT NULL,
        cell_id text NULL,
        body text NOT NULL CHECK (char_length(body) BETWEEN 1 AND 10000),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        CONSTRAINT lesson_notes_lesson_owner {_LESSON_FK}
    )""",
    "CREATE INDEX lesson_notes_reader ON lesson_notes (user_id, lesson_id, created_at)",
    f"""CREATE TABLE lesson_bookmarks (
        {_OWNER},
        {_USER},
        lesson_id uuid NOT NULL,
        cell_id text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (user_id, lesson_id, cell_id),
        CONSTRAINT lesson_bookmarks_lesson_owner {_LESSON_FK}
    )""",
)

DOWNGRADE = (
    "DROP TABLE lesson_bookmarks, lesson_notes, lesson_progress",
    "ALTER TABLE lessons DROP COLUMN skipped_at, DROP COLUMN deleted_at, DROP COLUMN version",
    "DROP INDEX courses_unsourced_title",
    "CREATE UNIQUE INDEX courses_unsourced_title ON courses (owner_id, title) WHERE material_sha256 IS NULL",
    "DROP INDEX courses_owner_position",
    """ALTER TABLE courses DROP CONSTRAINT courses_position_state, DROP COLUMN description,
        DROP COLUMN position, DROP COLUMN archived_at, DROP COLUMN deleted_at,
        DROP COLUMN deleted_by_material, DROP COLUMN version, DROP COLUMN order_source,
        DROP COLUMN last_position""",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
