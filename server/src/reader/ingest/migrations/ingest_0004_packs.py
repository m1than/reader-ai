"""SRV-INGEST FR-INGEST-14 revision 3 (ADR-0039): base packs, overlays, the manifest and their triggers.

Revision ID: ingest_0004_packs (branch `ingest`), down_revision ingest_0003. Registers every existing
`ingests` row as a sealed base pack of its converter version, backfills its spans' locators and quote hashes
(FR-INGEST-33/-34 over the stored rows), builds a sealed profile overlay per pack and writes one first
manifest entry per owner (FR-INGEST-40's legacy registration). reader.yaml is not read here: a declared
material's profile has license and language null until the worker-start reconcile (FR-INGEST-36). No
`search` revision.
"""

from __future__ import annotations

import json
from typing import Any

from alembic import op
from sqlalchemy import text

from reader.ingest import PARSE_CONFIGS, locator, manifest, overlays, packs

revision = "ingest_0004_packs"
down_revision: str | None = "ingest_0003"
branch_labels = None
depends_on = None

_OWNER = "owner_id uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT"
_HEX = "~ '^[0-9a-f]{64}$'"
_PACK_FK = "FOREIGN KEY (owner_id, pack_id) REFERENCES base_packs (owner_id, pack_id) ON DELETE RESTRICT"
_OVERLAY_FK = (
    "FOREIGN KEY (owner_id, overlay_id) REFERENCES overlays (owner_id, overlay_id) ON DELETE RESTRICT"
)

TABLES = (
    "DROP INDEX ingests_one_per_material",
    "CREATE UNIQUE INDEX ingests_one_per_version ON ingests (material_id, converter_version)",
    "ALTER TABLE material_spans ADD COLUMN locator text NULL, ADD COLUMN quote_sha256 text NULL",
    f"""CREATE TABLE base_packs (
        {_OWNER},
        pack_id text NOT NULL CHECK (pack_id {_HEX}),
        material_sha256 text NOT NULL CHECK (material_sha256 {_HEX}),
        ingest_id uuid NOT NULL UNIQUE REFERENCES ingests (id) ON DELETE RESTRICT,
        parse_config jsonb NOT NULL,
        state text NOT NULL CHECK (state IN ('building', 'sealed')),
        created_at timestamptz NOT NULL DEFAULT now(),
        sealed_at timestamptz NULL,
        collected_at timestamptz NULL,
        purged_at timestamptz NULL,
        PRIMARY KEY (owner_id, pack_id)
    )""",
    f"""CREATE TABLE overlays (
        {_OWNER},
        overlay_id text NOT NULL CHECK (overlay_id {_HEX}),
        pack_id text NOT NULL,
        kind text NOT NULL CHECK (kind IN ('profile', 'tags', 'vectors', 'prefixes', 'summaries')),
        builder_version text NOT NULL,
        inputs jsonb NOT NULL,
        state text NOT NULL CHECK (state IN ('building', 'sealed')),
        created_at timestamptz NOT NULL DEFAULT now(),
        sealed_at timestamptz NULL,
        collected_at timestamptz NULL,
        PRIMARY KEY (owner_id, overlay_id),
        {_PACK_FK}
    )""",
    f"""CREATE TABLE pack_profiles (
        {_OWNER},
        overlay_id text NOT NULL,
        role text NULL,
        reliability text NULL CHECK (reliability IN ('A', 'B', 'C', 'D', 'E', 'F')),
        as_of date NULL,
        license text NULL,
        language text NULL,
        set_by text NOT NULL CHECK (set_by IN ('rule', 'owner')),
        PRIMARY KEY (owner_id, overlay_id),
        {_OVERLAY_FK}
    )""",
    f"""CREATE TABLE span_tags (
        {_OWNER},
        overlay_id text NOT NULL,
        pack_id text NOT NULL,
        locator text NOT NULL,
        facet text NOT NULL,
        value text NOT NULL,
        by text NOT NULL CHECK (by IN ('rule', 'model', 'owner')),
        confidence real NOT NULL,
        tagger_version text NOT NULL,
        PRIMARY KEY (owner_id, overlay_id, locator, facet, value),
        {_OVERLAY_FK}
    )""",
    f"""CREATE TABLE manifest_entries (
        {_OWNER},
        seq bigint NOT NULL CHECK (seq >= 1),
        parent_seq bigint NOT NULL CHECK (parent_seq = seq - 1),
        entry jsonb NOT NULL,
        brain_version text NOT NULL CHECK (brain_version {_HEX}),
        reason text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (owner_id, seq),
        UNIQUE (owner_id, parent_seq)
    )""",
    f"""CREATE TABLE pending_activations (
        {_OWNER},
        pack_id text NOT NULL,
        material_sha256 text NOT NULL,
        overlays jsonb NOT NULL,
        reason text NULL,
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (owner_id, pack_id),
        {_PACK_FK}
    )""",
    f"""CREATE TABLE seq_maps (
        {_OWNER},
        from_ingest_id uuid NOT NULL REFERENCES ingests (id) ON DELETE RESTRICT,
        to_pack_id text NOT NULL,
        state text NOT NULL CHECK (state IN ('accepted', 'refused')),
        reason text NULL,
        span_count integer NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (owner_id, from_ingest_id, to_pack_id),
        FOREIGN KEY (owner_id, to_pack_id) REFERENCES base_packs (owner_id, pack_id) ON DELETE RESTRICT
    )""",
    "CREATE UNIQUE INDEX seq_maps_one_accepted ON seq_maps (owner_id, from_ingest_id)"
    " WHERE state = 'accepted'",
    f"""CREATE TABLE span_seq_map (
        {_OWNER},
        from_ingest_id uuid NOT NULL,
        to_pack_id text NOT NULL,
        seq integer NOT NULL,
        locator text NOT NULL,
        quote_sha256 text NOT NULL,
        PRIMARY KEY (owner_id, from_ingest_id, seq),
        FOREIGN KEY (owner_id, from_ingest_id, to_pack_id)
            REFERENCES seq_maps (owner_id, from_ingest_id, to_pack_id) ON DELETE RESTRICT
    )""",
    f"""CREATE TABLE anchor_maps (
        {_OWNER},
        from_pack_id text NOT NULL,
        to_pack_id text NOT NULL,
        from_locator text NOT NULL,
        quote_sha256 text NOT NULL,
        to_locator text NULL,
        state text NOT NULL CHECK (state IN ('mapped', 'orphaned')),
        PRIMARY KEY (owner_id, from_pack_id, to_pack_id, from_locator)
    )""",
    f"""CREATE TABLE anchor_runs (
        {_OWNER},
        from_pack_id text NOT NULL,
        to_pack_id text NOT NULL,
        mapped integer NOT NULL,
        orphaned integer NOT NULL,
        finished_at timestamptz NOT NULL,
        PRIMARY KEY (owner_id, from_pack_id, to_pack_id)
    )""",
    f"""CREATE TABLE citation_migrations (
        {_OWNER},
        target text NOT NULL,
        rows integer NOT NULL,
        finished_at timestamptz NOT NULL DEFAULT now()
    )""",
)

# Triggers (FR-INGEST-35, -36, -38): each refusal raises; DELETE of pack data only with reader.pack_delete on.
TRIGGERS = (
    """CREATE FUNCTION ingest_base_packs_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'base_packs rows are tombstones'; END IF;
        IF (OLD.owner_id, OLD.pack_id, OLD.material_sha256, OLD.ingest_id, OLD.parse_config, OLD.created_at)
           IS DISTINCT FROM
           (NEW.owner_id, NEW.pack_id, NEW.material_sha256, NEW.ingest_id, NEW.parse_config, NEW.created_at)
        THEN RAISE EXCEPTION 'base_packs identity is immutable'; END IF;
        IF OLD.state = 'building' AND NEW.state = 'sealed' AND NEW.sealed_at IS NOT NULL
           AND NEW.collected_at IS NULL AND NEW.purged_at IS NULL THEN
            IF EXISTS (SELECT 1 FROM material_spans WHERE ingest_id = NEW.ingest_id
                       AND (locator IS NULL OR quote_sha256 IS NULL))
            THEN RAISE EXCEPTION 'a span of the pack has no locator'; END IF;
            RETURN NEW;
        END IF;
        IF OLD.state = 'sealed' AND NEW.state = 'sealed' AND OLD.sealed_at = NEW.sealed_at
           AND ((OLD.collected_at IS NULL AND NEW.collected_at IS NOT NULL
                 AND OLD.purged_at IS NOT DISTINCT FROM NEW.purged_at)
             OR (OLD.purged_at IS NULL AND NEW.purged_at IS NOT NULL
                 AND OLD.collected_at IS NOT DISTINCT FROM NEW.collected_at))
        THEN RETURN NEW; END IF;
        RAISE EXCEPTION 'base_packs update refused';
    END $$""",
    "CREATE TRIGGER base_packs_guard BEFORE UPDATE OR DELETE ON base_packs"
    " FOR EACH ROW EXECUTE FUNCTION ingest_base_packs_guard()",
    """CREATE FUNCTION ingest_ingests_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            IF EXISTS (SELECT 1 FROM base_packs WHERE ingest_id = OLD.id)
            THEN RAISE EXCEPTION 'ingests rows of packs are tombstones'; END IF;
            RETURN OLD;  -- a bare ingest (no pack): other modules' fixtures only
        END IF;
        IF EXISTS (SELECT 1 FROM base_packs WHERE ingest_id = OLD.id AND state = 'sealed')
        THEN RAISE EXCEPTION 'the ingest of a sealed pack is immutable'; END IF;
        RETURN NEW;
    END $$""",
    "CREATE TRIGGER ingests_guard BEFORE UPDATE OR DELETE ON ingests"
    " FOR EACH ROW EXECUTE FUNCTION ingest_ingests_guard()",
    """CREATE FUNCTION ingest_pack_rows_guard() RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE ingest uuid; dest uuid;
    BEGIN
        IF TG_OP = 'INSERT' THEN ingest := NEW.ingest_id; ELSE ingest := OLD.ingest_id; END IF;
        IF TG_OP = 'UPDATE' THEN dest := NEW.ingest_id; ELSE dest := ingest; END IF;
        IF EXISTS (SELECT 1 FROM base_packs WHERE ingest_id IN (ingest, dest) AND state = 'sealed') THEN
            IF TG_OP = 'DELETE' AND coalesce(current_setting('reader.pack_delete', true), '') = 'on'
            THEN RETURN OLD; END IF;
            RAISE EXCEPTION 'rows of a sealed pack are immutable';
        END IF;
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END $$""",
    "CREATE TRIGGER material_spans_guard BEFORE INSERT OR UPDATE OR DELETE ON material_spans"
    " FOR EACH ROW EXECUTE FUNCTION ingest_pack_rows_guard()",
    "CREATE TRIGGER material_outline_guard BEFORE INSERT OR UPDATE OR DELETE ON material_outline"
    " FOR EACH ROW EXECUTE FUNCTION ingest_pack_rows_guard()",
    """CREATE FUNCTION ingest_overlays_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'overlays rows are tombstones'; END IF;
        IF (OLD.owner_id, OLD.overlay_id, OLD.pack_id, OLD.kind, OLD.builder_version, OLD.inputs,
            OLD.created_at) IS DISTINCT FROM
           (NEW.owner_id, NEW.overlay_id, NEW.pack_id, NEW.kind, NEW.builder_version, NEW.inputs,
            NEW.created_at)
        THEN RAISE EXCEPTION 'overlays identity is immutable'; END IF;
        IF OLD.collected_at IS NULL AND NEW.collected_at IS NOT NULL AND OLD.state = NEW.state
           AND OLD.sealed_at IS NOT DISTINCT FROM NEW.sealed_at THEN RETURN NEW; END IF;
        IF OLD.state = 'building' AND NEW.state = 'sealed' AND NEW.sealed_at IS NOT NULL
           AND OLD.collected_at IS NULL AND NEW.collected_at IS NULL THEN RETURN NEW; END IF;
        RAISE EXCEPTION 'overlays update refused';
    END $$""",
    "CREATE TRIGGER overlays_guard BEFORE UPDATE OR DELETE ON overlays"
    " FOR EACH ROW EXECUTE FUNCTION ingest_overlays_guard()",
    """CREATE FUNCTION ingest_overlay_rows_guard() RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE oid text; owner uuid; new_oid text; new_owner uuid;
    BEGIN
        IF TG_OP = 'INSERT' THEN oid := NEW.overlay_id; owner := NEW.owner_id;
        ELSE oid := OLD.overlay_id; owner := OLD.owner_id; END IF;
        IF TG_OP = 'UPDATE' THEN new_oid := NEW.overlay_id; new_owner := NEW.owner_id;
        ELSE new_oid := oid; new_owner := owner; END IF;
        IF EXISTS (SELECT 1 FROM overlays WHERE state = 'sealed'
                   AND ((owner_id = owner AND overlay_id = oid)
                        OR (owner_id = new_owner AND overlay_id = new_oid))) THEN
            IF TG_OP = 'DELETE' AND coalesce(current_setting('reader.pack_delete', true), '') = 'on'
            THEN RETURN OLD; END IF;
            RAISE EXCEPTION 'rows of a sealed overlay are immutable';
        END IF;
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END $$""",
    "CREATE TRIGGER pack_profiles_guard BEFORE INSERT OR UPDATE OR DELETE ON pack_profiles"
    " FOR EACH ROW EXECUTE FUNCTION ingest_overlay_rows_guard()",
    "CREATE TRIGGER span_tags_guard BEFORE INSERT OR UPDATE OR DELETE ON span_tags"
    " FOR EACH ROW EXECUTE FUNCTION ingest_overlay_rows_guard()",
    """CREATE FUNCTION ingest_manifest_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'manifest_entries is append-only'; END $$""",
    "CREATE TRIGGER manifest_entries_guard BEFORE UPDATE OR DELETE ON manifest_entries"
    " FOR EACH ROW EXECUTE FUNCTION ingest_manifest_guard()",
    "CREATE TRIGGER manifest_entries_truncate BEFORE TRUNCATE ON manifest_entries"
    " FOR EACH STATEMENT EXECUTE FUNCTION ingest_manifest_guard()",
)

# Not NOT NULL: other modules' [db] fixtures write spans of a bare ingest; the seal trigger checks instead.
AFTER_BACKFILL = (
    "ALTER TABLE material_spans ADD CONSTRAINT material_spans_locator_unique UNIQUE (ingest_id, locator)",
)

DOWNGRADE = (
    "DROP TABLE citation_migrations, anchor_runs, anchor_maps, span_seq_map, seq_maps, pending_activations,"
    " manifest_entries, span_tags, pack_profiles, overlays, base_packs",
    "DROP TRIGGER material_spans_guard ON material_spans",
    "DROP TRIGGER material_outline_guard ON material_outline",
    "DROP TRIGGER ingests_guard ON ingests",
    "DROP FUNCTION ingest_base_packs_guard, ingest_ingests_guard, ingest_pack_rows_guard,"
    " ingest_overlays_guard, ingest_overlay_rows_guard, ingest_manifest_guard",
    "ALTER TABLE material_spans DROP CONSTRAINT material_spans_locator_unique,"
    " DROP COLUMN locator, DROP COLUMN quote_sha256",
    "DROP INDEX ingests_one_per_version",
    "CREATE UNIQUE INDEX ingests_one_per_material ON ingests (material_id)",
)


def _backfill(bind: Any) -> None:
    ingests = bind.execute(text(
        "SELECT i.id, i.owner_id, i.converter_version, m.sha256, m.config_id, m.deleted_at IS NOT NULL"
        " FROM ingests i JOIN materials m ON m.id = i.material_id ORDER BY i.owner_id, m.sha256")).all()
    heads: dict[Any, dict[str, dict[str, str]]] = {}
    for ingest_id, owner, version, sha, config_id, deleted in ingests:
        spans = bind.execute(text(
            "SELECT seq, page, text FROM material_spans WHERE ingest_id = :i ORDER BY page, seq"),
            {"i": ingest_id}).all()
        pages: dict[int, list[Any]] = {}
        for row in spans:
            pages.setdefault(int(row[1]), []).append(row)
        for page, rows in pages.items():
            ids = locator.page_locators(sha, page, [str(r[2]) for r in rows])
            for row, loc in zip(rows, ids, strict=True):
                bind.execute(text("UPDATE material_spans SET locator = :l, quote_sha256 = :q"
                                  " WHERE ingest_id = :i AND seq = :s"),
                             {"l": loc, "q": locator.quote_sha256(str(row[2])), "i": ingest_id, "s": row[0]})
        pid = packs.pack_id(sha, version)
        bind.execute(text(
            "INSERT INTO base_packs (owner_id, pack_id, material_sha256, ingest_id, parse_config, state,"
            " sealed_at) VALUES (:o, :p, :s, :i, CAST(:c AS jsonb), 'sealed', now())"),
            {"o": owner, "p": pid, "s": sha, "i": ingest_id,
             "c": json.dumps(dict(PARSE_CONFIGS[version]))})
        language = None
        if str(config_id).startswith("up-"):
            language = bind.execute(
                text("SELECT language FROM uploads WHERE sha256 = :s"), {"s": sha}
            ).scalar()
        values = {"license": packs.UPLOAD_LICENSE if language is not None else None, "language": language}
        inputs = overlays.profile_inputs(values)
        oid = overlays.overlay_id("profile", pid, inputs, overlays.PROFILE_BUILDER)
        bind.execute(text(
            "INSERT INTO overlays (owner_id, overlay_id, pack_id, kind, builder_version, inputs, state,"
            " sealed_at) VALUES (:o, :i, :p, 'profile', :b, CAST(:inp AS jsonb), 'sealed', now())"),
            {"o": owner, "i": oid, "p": pid, "b": overlays.PROFILE_BUILDER, "inp": json.dumps(inputs)})
        bind.execute(text(
            "INSERT INTO pack_profiles (owner_id, overlay_id, license, language, set_by)"
            " VALUES (:o, :i, :l, :g, 'rule')"),
            {"o": owner, "i": oid, "l": inputs["license"], "g": language})
        if deleted:  # out of the first entry; a pending {profile} row lets the restore activate it
            bind.execute(text(
                "INSERT INTO pending_activations (owner_id, pack_id, material_sha256, overlays)"
                " VALUES (:o, :p, :s, CAST(:v AS jsonb))"),
                {"o": owner, "p": pid, "s": sha, "v": json.dumps({"profile": oid})})
            continue
        heads.setdefault(owner, {})[pid] = {"profile": oid}
    for owner, elements in heads.items():
        entry = manifest.make_entry(elements)
        bind.execute(text(
            "INSERT INTO manifest_entries (owner_id, seq, parent_seq, entry, brain_version, reason)"
            " VALUES (:o, 1, 0, CAST(:e AS jsonb), :b, 'ingest_0004_packs')"),
            {"o": owner, "e": json.dumps(entry), "b": manifest.brain_version(entry)})


def upgrade() -> None:
    for statement in TABLES:
        op.execute(statement)
    _backfill(op.get_bind())
    for statement in (*AFTER_BACKFILL, *TRIGGERS):
        op.execute(statement)


def downgrade() -> None:
    bind = op.get_bind()
    twice = bind.execute(text(
        "SELECT count(*) FROM (SELECT material_id FROM ingests GROUP BY material_id HAVING count(*) > 1) t"
    )).scalar()
    if twice:
        raise RuntimeError("ingest_0004_packs downgrade refused: a material holds more than one ingest")
    for statement in DOWNGRADE:
        op.execute(statement)
