"""SRV-FRESH migration: `material_bibliography` and `freshness_reports` (FR-FRESH-1, -10, -11; INV-FRESH-4).

Revision ID: freshness_0001
Revises: none (its own branch `freshness`); depends on ingest_0001 (materials, ingests; users through
0001_auth).
"""

from __future__ import annotations

from alembic import op

revision = "freshness_0001"
down_revision: str | None = None
branch_labels = ("freshness",)
depends_on = "ingest_0001"

_OWNER = "owner_id uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT"
_SHA = "CHECK ({0} ~ '^[0-9a-f]{{64}}$')"

# One statement per op.execute: asyncpg runs prepared statements, which hold a single command.
UPGRADE = (
    f"""CREATE TABLE material_bibliography (
        {_OWNER},
        material_id uuid NOT NULL REFERENCES materials (id) ON DELETE RESTRICT,
        title text NOT NULL,
        authors text[] NOT NULL,
        edition text NULL,
        year integer NULL,
        publisher text NULL,
        isbn text NULL,
        original_title text NULL,
        original_authors text[] NULL,
        original_edition text NULL,
        original_year integer NULL,
        source text NOT NULL CHECK (source IN ('owner', 'scan')),
        sha256 text NOT NULL {_SHA.format("sha256")},
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (owner_id, material_id)
    )""",
    f"""CREATE TABLE freshness_reports (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        {_OWNER},
        material_id uuid NOT NULL REFERENCES materials (id) ON DELETE RESTRICT,
        ingest_id uuid NOT NULL REFERENCES ingests (id) ON DELETE RESTRICT,
        status text NOT NULL CHECK (status IN ('running', 'done', 'failed', 'budget_refused')),
        scan_inputs_sha256 text NOT NULL {_SHA.format("scan_inputs_sha256")},
        inputs_sha256 text NULL,
        scan_reused_from uuid NULL REFERENCES freshness_reports (id) ON DELETE RESTRICT,
        bibliography jsonb NULL,
        sample jsonb NOT NULL,
        claims jsonb NULL,
        scan_bibliography jsonb NULL,
        searches jsonb NULL,
        verdict text NULL CHECK (verdict IN ('current', 'partly_outdated', 'outdated')),
        reasons jsonb NULL,
        claim_status jsonb NULL,
        recommendations jsonb NULL,
        dropped jsonb NULL,
        web_search_requests integer NULL,
        cost_usd numeric(12, 6) NULL,
        models jsonb NOT NULL DEFAULT '{{}}'::jsonb,
        prompt_versions jsonb NOT NULL DEFAULT '{{}}'::jsonb,
        error text NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        finished_at timestamptz NULL
    )""",
    "CREATE INDEX freshness_reports_latest ON freshness_reports (owner_id, material_id, created_at DESC)",
    "CREATE UNIQUE INDEX freshness_reports_one_running ON freshness_reports (owner_id)"
    " WHERE status = 'running'",
)

DOWNGRADE = ("DROP TABLE freshness_reports, material_bibliography",)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
