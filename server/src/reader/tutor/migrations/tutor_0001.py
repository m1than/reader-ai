"""SRV-TUTOR migration: `tutor_questions` (FR-TUTOR-1..4, -9, -10, INV-TUTOR-7).

Revision ID: tutor_0001
Revises: lessons_0001 (a branch off SRV-LESSON's head; it needs lessons and lesson_revisions)
"""

from __future__ import annotations

from alembic import op

revision = "tutor_0001"
down_revision = "lessons_0001"
branch_labels = ("tutor",)
depends_on = None

_STATES = "'queued', 'answered', 'failed', 'refused', 'expired'"

# One statement per op.execute: asyncpg runs prepared statements, which hold a single command.
UPGRADE = (
    f"""CREATE TABLE tutor_questions (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        owner_id uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
        lesson_id uuid NOT NULL,
        revision_sha256 text NOT NULL,
        cell_id text NULL,
        question text NOT NULL CHECK (char_length(question) BETWEEN 1 AND 1000),
        state text NOT NULL CHECK (state IN ({_STATES})),
        answer jsonb NULL,
        grounded boolean NULL,
        error_code text NULL,
        model text NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        finished_at timestamptz NULL,
        CONSTRAINT tutor_questions_lesson_owner FOREIGN KEY (lesson_id, owner_id)
            REFERENCES lessons (id, owner_id) ON DELETE RESTRICT,
        CONSTRAINT tutor_questions_revision FOREIGN KEY (lesson_id, revision_sha256)
            REFERENCES lesson_revisions (lesson_id, sha256) ON DELETE RESTRICT
    )""",
    "CREATE UNIQUE INDEX tutor_questions_one_queued ON tutor_questions (owner_id) WHERE state = 'queued'",
    "CREATE INDEX tutor_questions_history ON tutor_questions (owner_id, lesson_id, created_at)",
)

DOWNGRADE = ("DROP TABLE tutor_questions",)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
