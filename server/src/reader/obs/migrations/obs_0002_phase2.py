"""SRV-OBS phase 2 (ADR-0038): the Postgres records the api reads (it never reads MLflow).

- `obs_bench_results` (FR-OBS-23): one row per owner upload, the recomputed scores only (no file
  aggregate), the per-question ranks (search) used for the stepwise metric, and the pinned dataset records
  of the set (committed repository data), so `obs.bench` never needs the uploaded file again.
- `owner_labels` (FR-OBS-24): the owner's labels; MLflow holds a HUMAN-assessment mirror.
  `lesson_revision_sha256` is the lesson revision the owner saw (the published revision for a lesson, the
  question's revision for an answer); '' when the lesson has no revision yet, so the unique key still
  deduplicates.
- `obs_stack_status` (FR-OBS-32): the newest probe per member, written by `obs.health`.
"""

from __future__ import annotations

from alembic import op

revision = "obs_0002"
down_revision: str | None = "obs_0001"
branch_labels = None
depends_on = "0001_auth"  # the owner foreign keys

_SHA = "~ '^[0-9a-f]{64}$'"
REASONS = ("wrong_fact", "unclear", "off_topic", "broken_lab", "bad_citation", "other")
MEMBERS = ("mlflow", "rotel")
DETAILS = ("healthy", "unhealthy", "unreachable", "listening")


def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.execute(
        f"""CREATE TABLE obs_bench_results (
        id uuid PRIMARY KEY,
        owner_id uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        digest char(64) NOT NULL UNIQUE CHECK (digest {_SHA}),
        kind text NOT NULL CHECK (kind IN ('search', 'tutor')),
        set_name text NOT NULL CHECK (set_name ~ '^[a-z0-9-]{{1,64}}$'),
        set_sha256 char(64) NOT NULL CHECK (set_sha256 {_SHA}),
        model text NULL CHECK (model ~ '^[A-Za-z0-9_./-]{{1,161}}$'),
        "commit" text NULL CHECK ("commit" ~ '^[0-9a-f]{{7,40}}$'),
        params jsonb NOT NULL DEFAULT '{{}}'::jsonb,
        scores jsonb NOT NULL,
        per_question jsonb NOT NULL DEFAULT '[]'::jsonb,
        records jsonb NOT NULL DEFAULT '[]'::jsonb,
        created_at timestamptz NOT NULL DEFAULT now()
    )"""
    )
    op.execute(
        "CREATE INDEX obs_bench_results_series ON obs_bench_results (kind, set_name, model, created_at)"
    )
    op.execute(
        f"""CREATE TABLE owner_labels (
        id uuid PRIMARY KEY,
        owner_id uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        target text NOT NULL CHECK (target IN ('lesson', 'answer')),
        target_id uuid NOT NULL,
        lesson_revision_sha256 text NOT NULL DEFAULT '' CHECK (lesson_revision_sha256 = ''
            OR lesson_revision_sha256 {_SHA}),
        ok boolean NOT NULL,
        reason text NULL CHECK (reason IN {_in(REASONS)}),
        revision integer NOT NULL CHECK (revision >= 1),
        assessment_id text NULL CHECK (assessment_id ~ '^a-[0-9a-f]{{1,64}}$'),
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (owner_id, target, target_id, lesson_revision_sha256)
    )"""
    )
    op.execute(
        f"""CREATE TABLE obs_stack_status (
        member text PRIMARY KEY CHECK (member IN {_in(MEMBERS)}),
        ok boolean NOT NULL,
        detail text NOT NULL CHECK (detail IN {_in(DETAILS)}),
        checked_at timestamptz NOT NULL
    )"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE obs_stack_status")
    op.execute("DROP TABLE owner_labels")
    op.execute("DROP TABLE obs_bench_results")
