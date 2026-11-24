"""SRV-TUTOR migration: cleared questions (FR-TUTOR-20). A cleared row keeps its state, model, error code,
timestamps and spend, and loses its text: the question CHECK allows '' exactly when `cleared_at` is set.

Revision ID: tutor_0002
Revises: tutor_0001
"""

from __future__ import annotations

from alembic import op

revision = "tutor_0002"
down_revision = "tutor_0001"
branch_labels = None
depends_on = None

UPGRADE = (
    "ALTER TABLE tutor_questions ADD COLUMN cleared_at timestamptz NULL",
    "ALTER TABLE tutor_questions DROP CONSTRAINT tutor_questions_question_check",
    "ALTER TABLE tutor_questions ADD CONSTRAINT tutor_questions_question_check CHECK ("
    "(cleared_at IS NULL AND char_length(question) BETWEEN 1 AND 1000)"
    " OR (cleared_at IS NOT NULL AND question = ''))",
)
DOWNGRADE = (
    "DELETE FROM tutor_questions WHERE cleared_at IS NOT NULL",
    "ALTER TABLE tutor_questions DROP CONSTRAINT tutor_questions_question_check",
    "ALTER TABLE tutor_questions ADD CONSTRAINT tutor_questions_question_check"
    " CHECK (char_length(question) BETWEEN 1 AND 1000)",
    "ALTER TABLE tutor_questions DROP COLUMN cleared_at",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
