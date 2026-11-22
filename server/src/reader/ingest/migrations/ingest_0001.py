"""SRV-INGEST FR-INGEST-14: materials, ingests, spans and outline, each owned (`owner_id` -> users.id).

Revision ID: ingest_0001 (branch `ingest`); depends on SRV-AUTH's base revision `0001_auth`, which creates
`users`.
`ingests` is unique per material: the material's one row is its frozen converter version (FR-INGEST-17).
"""

from __future__ import annotations

from alembic import op

revision = "ingest_0001"
down_revision: str | None = None
branch_labels = ("ingest",)
depends_on = "0001_auth"

_OWNER = "owner_id uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT"
_KINDS = "'text', 'heading', 'list_item', 'formula', 'code', 'table', 'figure'"

# One statement per op.execute: asyncpg runs prepared statements, which hold a single command.
UPGRADE = (
    f"""CREATE TABLE materials (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        {_OWNER},
        sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{{64}}$'),
        config_id text NOT NULL,
        page_count integer NOT NULL CHECK (page_count BETWEEN 1 AND 5000),
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (owner_id, sha256)
    )""",
    f"""CREATE TABLE ingests (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        {_OWNER},
        material_id uuid NOT NULL REFERENCES materials (id) ON DELETE RESTRICT,
        converter_version text NOT NULL,
        artifact_id text NOT NULL CHECK (artifact_id ~ '^[0-9a-f]{{64}}$'),
        span_count integer NOT NULL,
        outline_count integer NOT NULL,
        outline_source text NOT NULL CHECK (outline_source IN ('bookmarks', 'headings')),
        pages_without_text integer[] NOT NULL,
        imported_at timestamptz NOT NULL DEFAULT now()
    )""",
    "CREATE UNIQUE INDEX ingests_one_per_material ON ingests (material_id)",
    f"""CREATE TABLE material_spans (
        ingest_id uuid NOT NULL REFERENCES ingests (id) ON DELETE CASCADE,
        {_OWNER},
        seq integer NOT NULL CHECK (seq >= 0),
        page integer NOT NULL CHECK (page >= 1),
        kind text NOT NULL CHECK (kind IN ({_KINDS})),
        level integer NULL,
        decoded boolean NULL,
        text text NOT NULL,
        x0 real NOT NULL,
        y0 real NOT NULL,
        x1 real NOT NULL,
        y1 real NOT NULL,
        PRIMARY KEY (ingest_id, seq)
    )""",
    "CREATE INDEX material_spans_page_idx ON material_spans (ingest_id, page, seq)",
    f"""CREATE TABLE material_outline (
        ingest_id uuid NOT NULL REFERENCES ingests (id) ON DELETE CASCADE,
        {_OWNER},
        seq integer NOT NULL CHECK (seq >= 0),
        level integer NOT NULL CHECK (level BETWEEN 1 AND 16),
        title text NOT NULL,
        page integer NULL,
        PRIMARY KEY (ingest_id, seq)
    )""",
)

DOWNGRADE = ("DROP TABLE material_outline, material_spans, ingests, materials",)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
