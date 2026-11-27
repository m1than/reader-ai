"""SRV-SEARCH migration: the span-vector overlay rows (FR-SEARCH-4, ADR-0039 revision).

Revision ID: search_0002
Revises: search_0001; depends on SRV-INGEST's ingest_0004_packs (the `overlays` table its foreign key names).

Rows are keyed by owner, because two owners of the same bytes share a pack id and an overlay id. `fts` is a
plain column computed at insert from the passage; no text column. A trigger keeps a sealed overlay's rows
immutable (ADR-0039 P1): INSERT and DELETE of a sealed overlay's rows, every UPDATE and every TRUNCATE are
refused; only `forget_overlay` deletes, under the transaction-local setting `reader.search_delete` (as
SRV-AGENT's `reader.judge_delete`).
"""

from __future__ import annotations

from alembic import op

revision = "search_0002"
down_revision = "search_0001"
branch_labels = None
depends_on = "ingest_0004_packs"

_SHA = "~ '^[0-9a-f]{64}$'"

UPGRADE = (
    f"""CREATE TABLE search_span_vectors (
        owner_id uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
        overlay_id text NOT NULL CHECK (overlay_id {_SHA}),
        pack_id text NOT NULL CHECK (pack_id {_SHA}),
        locator text NOT NULL,
        window_no integer NOT NULL CHECK (window_no >= 0),
        char_start integer NOT NULL CHECK (char_start >= 0),
        char_stop integer NOT NULL CHECK (char_stop >= char_start),
        model text NOT NULL,
        fts_config text NOT NULL CHECK (fts_config IN ('russian', 'english', 'simple')),
        fts tsvector NOT NULL,
        embedding vector(1024) NOT NULL,
        PRIMARY KEY (owner_id, overlay_id, locator, window_no),
        FOREIGN KEY (owner_id, overlay_id) REFERENCES overlays (owner_id, overlay_id) ON DELETE RESTRICT
    )""",
    "CREATE INDEX search_span_vectors_fts ON search_span_vectors USING gin (fts)",
    """CREATE FUNCTION search_span_vectors_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE owner uuid; oid text;
    BEGIN
        IF TG_OP = 'TRUNCATE' OR TG_OP = 'UPDATE' THEN
            RAISE EXCEPTION 'search_span_vectors rows are immutable';
        END IF;
        IF TG_OP = 'INSERT' THEN owner := NEW.owner_id; oid := NEW.overlay_id;
        ELSE owner := OLD.owner_id; oid := OLD.overlay_id; END IF;
        IF EXISTS (SELECT 1 FROM overlays
                   WHERE owner_id = owner AND overlay_id = oid AND state = 'sealed') THEN
            IF TG_OP = 'DELETE' AND coalesce(current_setting('reader.search_delete', true), '') = 'on'
            THEN RETURN OLD; END IF;
            RAISE EXCEPTION 'rows of a sealed overlay are immutable';
        END IF;
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END $$""",
    "CREATE TRIGGER search_span_vectors_guard BEFORE INSERT OR UPDATE OR DELETE ON search_span_vectors"
    " FOR EACH ROW EXECUTE FUNCTION search_span_vectors_guard()",
    "CREATE TRIGGER search_span_vectors_truncate BEFORE TRUNCATE ON search_span_vectors"
    " FOR EACH STATEMENT EXECUTE FUNCTION search_span_vectors_guard()",
)

DOWNGRADE = ("DROP TABLE search_span_vectors", "DROP FUNCTION search_span_vectors_guard")


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
