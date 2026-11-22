"""SRV-SEARCH migration: the vector extension, search_chunks and search_embeds (FR-SEARCH-4).

Revision ID: search_0001
Revises: 0001_auth (a branch off SRV-AUTH's base; it needs users.id)

SRV-INGEST's migration is not on the integration branch yet (declared deviation): `ingest_id` is a plain
uuid without a foreign key; the follow-up V.SRV-SEARCH-fk re-parents this branch onto the ingest head and
adds the keys. `fts_config` is text with a CHECK, not regconfig, because pg_upgrade refuses user tables with
reg* columns other than regclass, regrole and regtype. No vector index: an exact scan at slice scale
(declared deviation).
"""

from __future__ import annotations

from alembic import op

revision = "search_0001"
down_revision = "0001_auth"
branch_labels = ("search",)
depends_on = None

_SHA = "~ '^[0-9a-f]{64}$'"

# One statement per op.execute: asyncpg runs prepared statements, which hold a single command.
UPGRADE = (
    "CREATE EXTENSION IF NOT EXISTS vector",
    f"""CREATE TABLE search_chunks (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        owner_id uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
        ingest_id uuid NOT NULL,
        material_sha256 text NOT NULL CHECK (material_sha256 {_SHA}),
        model text NOT NULL,
        params_sha256 text NOT NULL CHECK (params_sha256 {_SHA}),
        fts_config text NOT NULL CHECK (fts_config IN ('russian', 'english', 'simple')),
        page integer NOT NULL CHECK (page >= 1),
        chunk_no integer NOT NULL CHECK (chunk_no >= 0),
        char_start integer NOT NULL,
        char_stop integer NOT NULL,
        span_seqs integer[] NOT NULL,
        primary_seq integer NOT NULL,
        text text NOT NULL,
        fts tsvector GENERATED ALWAYS AS (
            to_tsvector(
                CASE fts_config
                    WHEN 'russian' THEN 'russian'::regconfig
                    WHEN 'english' THEN 'english'::regconfig
                    ELSE 'simple'::regconfig
                END,
                text
            )
        ) STORED,
        embedding vector(1024) NOT NULL,
        UNIQUE (ingest_id, model, params_sha256, chunk_no)
    )""",
    "CREATE INDEX search_chunks_fts ON search_chunks USING gin (fts)",
    "CREATE INDEX search_chunks_owner ON search_chunks (owner_id, model, params_sha256)",
    f"""CREATE TABLE search_embeds (
        owner_id uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
        ingest_id uuid NOT NULL,
        model text NOT NULL,
        params_sha256 text NOT NULL CHECK (params_sha256 {_SHA}),
        chunk_count integer NOT NULL CHECK (chunk_count >= 0),
        embedded_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (ingest_id, model, params_sha256)
    )""",
)

DOWNGRADE = ("DROP TABLE search_embeds", "DROP TABLE search_chunks")


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
