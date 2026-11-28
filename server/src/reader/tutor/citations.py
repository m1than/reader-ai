"""SRV-TUTOR FR-TUTOR-22: the tutor's citation-bearing column, declared for SRV-INGEST's one-shot rewrite of
legacy citations (FR-INGEST-41). Data only; the command is SRV-INGEST's. Cleared rows (answer null) are
skipped."""

from __future__ import annotations

from typing import Final

from reader.ingest.migrate_citations import Target

REWRITERS: Final = (
    Target(
        name="tutor_questions.answer",
        select_sql="SELECT id, owner_id, answer FROM tutor_questions WHERE answer IS NOT NULL"
                   " ORDER BY id FOR UPDATE",
        update_sql="UPDATE tutor_questions SET answer = CAST(:doc AS jsonb) WHERE id = :key",
    ),
)
