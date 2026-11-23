"""SRV-PLAN FR-PLAN-11: study_plans, one row per planned course.

Revision ID: plan_0001 (branch `plan`, its own root like SRV-INGEST's and SRV-TUTOR's); depends on the
pipeline branch's head (courses, course_plans.origin) and SRV-INGEST's ingests.
"""

from __future__ import annotations

from alembic import op

revision = "plan_0001"
down_revision: str | None = None
branch_labels = ("plan",)
depends_on = ("pipeline_0002", "ingest_0001")

_SHA = "~ '^[0-9a-f]{64}$'"

UPGRADE = (
    f"""CREATE TABLE study_plans (
        course_id uuid PRIMARY KEY,
        owner_id uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
        plan_course_id text NOT NULL,
        material_sha256 text NOT NULL CHECK (material_sha256 {_SHA}),
        ingest_id uuid NOT NULL REFERENCES ingests (id) ON DELETE RESTRICT,
        total_weeks integer NOT NULL CHECK (total_weeks BETWEEN 1 AND 104),
        minutes_per_day integer NOT NULL
            CHECK (minutes_per_day BETWEEN 10 AND 240 AND minutes_per_day % 5 = 0),
        rate_centi integer NOT NULL CHECK (rate_centi BETWEEN 10 AND 2000),
        start_date date NOT NULL,
        plan_sha256 text NOT NULL CHECK (plan_sha256 {_SHA}),
        confirmed_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (owner_id, plan_course_id),
        CONSTRAINT study_plans_course FOREIGN KEY (course_id, owner_id) REFERENCES courses (id, owner_id)
    )""",
)
DOWNGRADE = ("DROP TABLE study_plans",)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
