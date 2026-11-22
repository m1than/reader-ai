"""SRV-PIPELINE migration: course_plans, compile_runs, compile_steps (SRV-PIPELINE `tables`).

Revision ID: pipeline_0001
Revises: 0001_auth (its own branch, like SRV-LESSON's), depends on lessons_0001 (it references lessons), so a
downgrade of the lessons branch takes this one first and stays a branch of its own.
"""

from __future__ import annotations

from alembic import op

revision = "pipeline_0001"
down_revision = "0001_auth"
branch_labels = ("pipeline",)
depends_on = ("lessons_0001",)

_SHA = "~ '^[0-9a-f]{64}$'"

UPGRADE = (
    f"""CREATE TABLE course_plans (
        owner_id uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
        plan_course_id text NOT NULL,
        plan_lesson_id text NOT NULL,
        lesson_id uuid NOT NULL,
        material_sha256 text NOT NULL CHECK (material_sha256 {_SHA}),
        pdf_first integer NOT NULL CHECK (pdf_first >= 1),
        pdf_last integer NOT NULL CHECK (pdf_last >= pdf_first),
        g1_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (owner_id, plan_course_id, plan_lesson_id),
        CONSTRAINT course_plans_lesson FOREIGN KEY (lesson_id, owner_id) REFERENCES lessons (id, owner_id)
    )""",
    """CREATE TABLE compile_runs (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        owner_id uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
        lesson_id uuid NOT NULL,
        cap_usd numeric(8, 2) NOT NULL CHECK (cap_usd > 0),
        task_budgets jsonb NOT NULL,
        budget_refused text NULL,
        loops integer NOT NULL DEFAULT 0,
        base_sha256 text NULL,
        last_model_sha256 text NULL,
        final_sha256 text NULL,
        g2 jsonb NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        finished_at timestamptz NULL,
        CONSTRAINT compile_runs_lesson FOREIGN KEY (lesson_id, owner_id) REFERENCES lessons (id, owner_id)
    )""",
    "CREATE INDEX compile_runs_lesson_idx ON compile_runs (lesson_id, created_at)",
    """CREATE TABLE compile_steps (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        owner_id uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
        run_id uuid NOT NULL REFERENCES compile_runs (id) ON DELETE RESTRICT,
        step integer NOT NULL CHECK (step >= 1),
        stage text NOT NULL,
        input_sha256 text NOT NULL,
        output_sha256 text NULL,
        report jsonb NULL,
        reused_from bigint NULL REFERENCES compile_steps (id),
        model text NULL,
        cost_usd numeric(12, 6) NOT NULL DEFAULT 0,
        duration_s numeric NOT NULL DEFAULT 0,
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (run_id, step)
    )""",
    "CREATE INDEX compile_steps_reuse ON compile_steps (owner_id, stage, input_sha256)",
)

DOWNGRADE = ("DROP TABLE compile_steps, compile_runs, course_plans",)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
