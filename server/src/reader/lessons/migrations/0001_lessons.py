"""SRV-LESSON migration: courses, lessons, revisions, stage records (FR-LESSON-14).

Revision ID: lessons_0001
Revises: 0001_auth (a branch off SRV-AUTH's base; it needs users.id)

A `down_revision`, not `depends_on`: with `depends_on` 0001_auth would stay a script head while
alembic_version records only lessons_0001, and SRV-AUTH's "at all heads" check (FR-AUTH-2) compares the two
sets.
"""

from __future__ import annotations

from alembic import op

revision = "lessons_0001"
down_revision = "0001_auth"
branch_labels = ("lessons",)
depends_on = None

_TITLE = "CHECK (char_length(title) BETWEEN 1 AND 200)"
_SHA = "~ '^[0-9a-f]{64}$'"

# One statement per op.execute: asyncpg runs prepared statements, which hold a single command.
UPGRADE = (
    f"""CREATE TABLE courses (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        owner_id uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
        title text NOT NULL {_TITLE},
        material_sha256 text NULL CHECK (material_sha256 {_SHA}),
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (id, owner_id)
    )""",
    "CREATE UNIQUE INDEX courses_unsourced_title ON courses (owner_id, title) WHERE material_sha256 IS NULL",
    f"""CREATE TABLE lessons (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        owner_id uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
        course_id uuid NOT NULL,
        ordinal integer NOT NULL CHECK (ordinal >= 1),
        title text NOT NULL {_TITLE},
        published_sha256 text NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (course_id, ordinal),
        UNIQUE (id, owner_id),
        CONSTRAINT lessons_course_owner FOREIGN KEY (course_id, owner_id)
            REFERENCES courses (id, owner_id) ON DELETE RESTRICT
    )""",
    f"""CREATE TABLE lesson_revisions (
        seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
        owner_id uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
        lesson_id uuid NOT NULL,
        sha256 text NOT NULL CHECK (sha256 {_SHA}),
        byte_size integer NOT NULL CHECK (byte_size > 0),
        cell_count integer NOT NULL CHECK (cell_count >= 0),
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (lesson_id, sha256),
        CONSTRAINT lesson_revisions_lesson_owner FOREIGN KEY (lesson_id, owner_id)
            REFERENCES lessons (id, owner_id) ON DELETE RESTRICT
    )""",
    """ALTER TABLE lessons ADD CONSTRAINT lessons_published_revision FOREIGN KEY (id, published_sha256)
        REFERENCES lesson_revisions (lesson_id, sha256) ON DELETE RESTRICT""",
    """CREATE TABLE lesson_revision_stages (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        owner_id uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
        lesson_id uuid NOT NULL,
        sha256 text NOT NULL,
        stage text NOT NULL,
        provenance jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT lesson_revision_stages_revision FOREIGN KEY (lesson_id, sha256)
            REFERENCES lesson_revisions (lesson_id, sha256) ON DELETE RESTRICT,
        CONSTRAINT lesson_revision_stages_lesson_owner FOREIGN KEY (lesson_id, owner_id)
            REFERENCES lessons (id, owner_id) ON DELETE RESTRICT
    )""",
    "CREATE INDEX lesson_revision_stages_revision_idx ON lesson_revision_stages (lesson_id, sha256)",
    """CREATE FUNCTION lessons_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION '% rows are immutable', TG_TABLE_NAME;
    END
    $$""",
    """CREATE TRIGGER lesson_revisions_immutable BEFORE UPDATE OR DELETE ON lesson_revisions
        FOR EACH ROW EXECUTE FUNCTION lessons_immutable()""",
    """CREATE TRIGGER lesson_revision_stages_immutable BEFORE UPDATE OR DELETE ON lesson_revision_stages
        FOR EACH ROW EXECUTE FUNCTION lessons_immutable()""",
)

DOWNGRADE = (
    "DROP TABLE lesson_revision_stages, lesson_revisions, lessons, courses",
    "DROP FUNCTION lessons_immutable()",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
