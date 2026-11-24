"""SRV-INGEST FR-INGEST-19: uploads (owned) and ingest_hosts (the host watcher's one row, a system table).

Revision ID: ingest_0002 (branch `ingest`), down_revision ingest_0001. There is no `cancelled` state: cancel
and Discard delete the row and then unlink the inbox file, under the instance's upload lock.
"""

from __future__ import annotations

from alembic import op

revision = "ingest_0002"
down_revision: str | None = "ingest_0001"
branch_labels = None
depends_on = None

UPGRADE = (
    """CREATE TABLE uploads (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        owner_id uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
        sha256 text NOT NULL UNIQUE CHECK (sha256 ~ '^[0-9a-f]{64}$'),
        size bigint NOT NULL CHECK (size > 0),
        filename text NOT NULL,
        language text NOT NULL CHECK (language IN ('ru', 'en', 'other')),
        state text NOT NULL
            CHECK (state IN ('queued', 'claimed', 'converting', 'sending', 'received', 'failed')),
        code text NULL,
        attempts integer NOT NULL DEFAULT 0,
        claim text NULL CHECK (claim ~ '^[0-9a-f]{32}$'),
        lease_until timestamptz NULL,
        pages_done integer NULL,
        pages_total integer NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX uploads_queue_idx ON uploads (state, created_at)",
    """CREATE TABLE ingest_hosts (
        singleton boolean PRIMARY KEY CHECK (singleton),
        seen_at timestamptz NOT NULL,
        state text NOT NULL CHECK (state IN ('ready', 'setup_missing')),
        strays integer NOT NULL CHECK (strays >= 0),
        converter_version text NOT NULL,
        watcher_sha text NOT NULL,
        busy text NOT NULL CHECK (busy IN ('none', 'claim', 'cli_running'))
    )""",
)

DOWNGRADE = ("DROP TABLE ingest_hosts, uploads",)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
