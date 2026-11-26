"""SRV-OBS ADR-0039 revision (V.kl-obs-spans): the judge stores.

- `judge_inputs` (FR-OBS-38): one row per recorded `judge.find` output, ids, hashes and ordinals; the query only
  for a consenting caller.
- `obs_eval_runs`, `obs_eval_records`, `obs_eval_scores` (FR-OBS-39): the record of a local judge evaluation.
- `judge_prompt_proposals` (FR-OBS-41): alignment proposals; text only while running (guarded candidates) or
  once proposed.
- `owner_labels` (FR-OBS-24): target `judge`, with `slot` and `note`.
"""

from __future__ import annotations

from alembic import op

revision = "obs_0003"
down_revision: str | None = "obs_0002"
branch_labels = None
depends_on = None

_SHA = "~ '^[0-9a-f]{64}$'"
EVAL_STATES = ("queued", "running", "done", "capped", "failed")
RECORD_STATES = ("queued", "done", "failed", "capped")
PROPOSAL_STATES = ("running", "proposed", "refused", "exported", "discarded")
REFUSALS = (
    "verbatim_8", "whole_input", "demonstration", "no_change", "guard_inputs_missing", "too_few_labels",
    "slot_unsupported", "eval_cap", "provider_error", "output_invalid", "step_lost", "section_delimiter",
    "frame_changed", "base_changed", "all_guard_refused", "no_training_errors", "threshold_missing",
    "policy_changed",
)
JUDGE_SLOTS = (
    "judge.find", "judge.eval.relevance", "judge.eval.sufficiency", "judge.eval.groundedness",
    "judge.eval.self_contained", "judge.eval.citation_supports_claim",
)


def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.execute(
        f"""CREATE TABLE judge_inputs (
        id uuid PRIMARY KEY,
        owner_id uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        slot text NOT NULL CHECK (slot = 'judge.find'),
        input_sha256 char(64) NOT NULL CHECK (input_sha256 {_SHA}),
        judge text NOT NULL CHECK (judge ~ '^judge\\.find@[0-9a-f]{{12,64}}$'),
        query text NULL,
        card_ids text[] NOT NULL CHECK (cardinality(card_ids) BETWEEN 1 AND 50),
        pack_ids text[] NOT NULL DEFAULT '{{}}',
        output jsonb NOT NULL,
        find_trace_id char(32) NULL,
        judge_trace_id char(32) NULL,
        channel text NULL,
        route text NULL,
        labelable boolean NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        CHECK (labelable = (query IS NOT NULL)),
        UNIQUE (owner_id, input_sha256, judge)
    )"""
    )
    op.execute("CREATE INDEX judge_inputs_created ON judge_inputs (created_at)")
    op.execute("CREATE INDEX judge_inputs_packs ON judge_inputs USING gin (pack_ids)")
    op.execute(
        f"""CREATE TABLE obs_eval_runs (
        id uuid PRIMARY KEY,
        owner_id uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        mode text NOT NULL CHECK (mode IN ('dataset', 'sample')),
        set_name text NULL,
        set_sha256 char(64) NULL CHECK (set_sha256 {_SHA}),
        set_doc jsonb NULL,
        caller text NOT NULL DEFAULT 'none' CHECK (caller IN ('none')),
        budget_profile text NOT NULL CHECK (budget_profile IN ('mcp', 'tutor')),
        judges text[] NOT NULL,
        brain_version char(64) NULL,
        policy_version char(64) NULL,
        version_key char(12) NULL,
        records integer NOT NULL CHECK (records >= 0),
        deadline timestamptz NOT NULL,
        state text NOT NULL CHECK (state IN {_in(EVAL_STATES)}),
        created_at timestamptz NOT NULL DEFAULT now(),
        finished_at timestamptz NULL
    )"""
    )
    op.execute(
        f"""CREATE TABLE obs_eval_records (
        eval_id uuid NOT NULL REFERENCES obs_eval_runs (id) ON DELETE CASCADE,
        record_id text NOT NULL,
        owner_id uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        judge_input_id uuid NULL REFERENCES judge_inputs (id) ON DELETE SET NULL,
        card_ids text[] NOT NULL DEFAULT '{{}}',
        pack_ids text[] NOT NULL DEFAULT '{{}}',
        find_ms integer NULL,
        cards integer NOT NULL DEFAULT 0,
        tokens integer NOT NULL DEFAULT 0,
        judged_tokens integer NOT NULL DEFAULT 0,
        unjudged_tokens integer NOT NULL DEFAULT 0,
        judge_ran boolean NULL,
        judge_skip text NULL,
        gate text NULL,
        skipped_not_entitled integer NOT NULL DEFAULT 0,
        invalid_outputs integer NOT NULL DEFAULT 0,
        trace_id char(32) NULL,
        state text NOT NULL CHECK (state IN {_in(RECORD_STATES)}),
        PRIMARY KEY (eval_id, record_id)
    )"""
    )
    op.execute(
        f"""CREATE TABLE obs_eval_scores (
        id uuid PRIMARY KEY,
        owner_id uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        eval_id uuid NULL REFERENCES obs_eval_runs (id) ON DELETE CASCADE,
        judge_input_id uuid NULL REFERENCES judge_inputs (id) ON DELETE CASCADE,
        record_id text NULL,
        slot text NOT NULL CHECK (slot IN {_in(JUDGE_SLOTS)}),
        judge text NOT NULL,
        value double precision NOT NULL CHECK (value >= 0 AND value <= 1),
        trace_id char(32) NULL,
        assessment_id text NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (eval_id, record_id, slot, judge),
        UNIQUE (judge_input_id, slot, judge)
    )"""
    )
    op.execute(
        f"""CREATE TABLE judge_prompt_proposals (
        id uuid PRIMARY KEY,
        owner_id uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        slot text NOT NULL CHECK (slot IN {_in(JUDGE_SLOTS)}),
        at_labels integer NOT NULL CHECK (at_labels >= 1),
        step integer NOT NULL DEFAULT 0,
        base_prompt_sha256 char(64) NOT NULL CHECK (base_prompt_sha256 {_SHA}),
        frame_sha256 char(64) NOT NULL CHECK (frame_sha256 {_SHA}),
        policy_version char(64) NULL,
        state text NOT NULL CHECK (state IN {_in(PROPOSAL_STATES)}),
        refusal text NULL CHECK (refusal IN {_in(REFUSALS)}),
        work jsonb NOT NULL DEFAULT '{{}}'::jsonb,
        candidate_text text NULL,
        candidates_under_test text[] NULL CHECK (cardinality(candidates_under_test) <= 2),
        proposed_text text NULL,
        proposed_sha256 char(64) NULL,
        label_ids uuid[] NOT NULL DEFAULT '{{}}',
        scores jsonb NOT NULL DEFAULT '{{}}'::jsonb,
        guard_version text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        exported_at timestamptz NULL,
        CHECK (state = 'running' OR (candidate_text IS NULL AND candidates_under_test IS NULL)),
        CHECK ((proposed_text IS NOT NULL) = (state IN ('proposed', 'exported'))),
        CHECK ((refusal IS NOT NULL) = (state = 'refused')),
        UNIQUE (owner_id, slot, at_labels)
    )"""
    )
    op.execute("ALTER TABLE owner_labels DROP CONSTRAINT IF EXISTS owner_labels_target_check")
    op.execute("ALTER TABLE owner_labels ADD CONSTRAINT owner_labels_target_check"
               " CHECK (target IN ('lesson', 'answer', 'judge'))")
    op.execute(f"ALTER TABLE owner_labels ADD COLUMN slot text NULL CHECK (slot IN {_in(JUDGE_SLOTS)})")
    op.execute("ALTER TABLE owner_labels ADD COLUMN note text NULL"
               " CHECK (note IS NULL OR (char_length(note) <= 200 AND position(E'\\n' IN note) = 0))")
    op.execute("ALTER TABLE owner_labels ADD CONSTRAINT owner_labels_slot_target"
               " CHECK ((slot IS NULL) = (target <> 'judge') AND (note IS NULL OR target = 'judge'))")


def downgrade() -> None:
    op.execute("ALTER TABLE owner_labels DROP CONSTRAINT owner_labels_slot_target")
    op.execute("DELETE FROM owner_labels WHERE target = 'judge'")
    op.execute("ALTER TABLE owner_labels DROP COLUMN note")
    op.execute("ALTER TABLE owner_labels DROP COLUMN slot")
    op.execute("ALTER TABLE owner_labels DROP CONSTRAINT owner_labels_target_check")
    op.execute("ALTER TABLE owner_labels ADD CONSTRAINT owner_labels_target_check"
               " CHECK (target IN ('lesson', 'answer'))")
    op.execute("DROP TABLE judge_prompt_proposals")
    op.execute("DROP TABLE obs_eval_scores")
    op.execute("DROP TABLE obs_eval_records")
    op.execute("DROP TABLE obs_eval_runs")
    op.execute("DROP TABLE judge_inputs")
