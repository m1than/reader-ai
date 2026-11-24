"""SRV-AUTH sign-up migration (ADR-0031, FR-AUTH-24): user source, state and skeleton, the signup ceremony,
failure scopes and the sign-up attempt rows.

Revision ID: 0002_signup
Revises: 0001_auth
"""

from __future__ import annotations

from alembic import op

revision = "0002_signup"
down_revision = "0001_auth"
branch_labels = None
depends_on = None

# The SQL backfill equals reader.auth.handles.skeleton for ASCII handles (every sign-up handle); `sync_users`
# rewrites the skeleton of every configured row with that function at each api start.
UPGRADE = (
    "ALTER TABLE users ADD COLUMN source text NOT NULL DEFAULT 'config' "
    "CHECK (source IN ('config', 'signup'))",
    "ALTER TABLE users ADD COLUMN state text NOT NULL DEFAULT 'active' "
    "CHECK (state IN ('active', 'pending'))",
    "ALTER TABLE users ADD COLUMN handle_skeleton text NULL",
    "UPDATE users SET handle_skeleton = translate("
    "replace(replace(replace(lower(handle), '-', ''), 'rn', 'm'), 'vv', 'w'), '01i', 'oll')",
    "ALTER TABLE users ALTER COLUMN handle_skeleton SET NOT NULL",
    "CREATE UNIQUE INDEX users_signup_skeleton ON users (handle_skeleton) WHERE source = 'signup'",
    "ALTER TABLE auth_challenges DROP CONSTRAINT auth_challenges_kind_check",
    "ALTER TABLE auth_challenges ADD CONSTRAINT auth_challenges_kind_check "
    "CHECK (kind IN ('register', 'authenticate', 'bootstrap', 'signup'))",
    "ALTER TABLE auth_challenges ADD COLUMN signup_handle text NULL, "
    "ADD COLUMN signup_display_name text NULL, ADD COLUMN signup_webauthn_user_id bytea NULL",
    "ALTER TABLE auth_challenges ADD CONSTRAINT auth_challenges_signup_fields CHECK ((kind = 'signup') = "
    "(signup_handle IS NOT NULL AND signup_display_name IS NOT NULL "
    "AND signup_webauthn_user_id IS NOT NULL))",
    "ALTER TABLE auth_failures ADD COLUMN scope text NOT NULL DEFAULT 'auth' "
    "CHECK (scope IN ('auth', 'signup'))",
    "CREATE INDEX auth_failures_scope_at ON auth_failures (scope, at)",
    """CREATE TABLE signup_attempts (
        id bigserial PRIMARY KEY,
        at timestamptz NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX signup_attempts_at ON signup_attempts (at)",
)

DOWNGRADE = (
    "DROP TABLE signup_attempts",
    "DROP INDEX auth_failures_scope_at",
    "ALTER TABLE auth_failures DROP COLUMN scope",
    "ALTER TABLE auth_challenges DROP CONSTRAINT auth_challenges_signup_fields",
    "DELETE FROM auth_challenges WHERE kind = 'signup'",
    "ALTER TABLE auth_challenges DROP COLUMN signup_handle, DROP COLUMN signup_display_name, "
    "DROP COLUMN signup_webauthn_user_id",
    "ALTER TABLE auth_challenges DROP CONSTRAINT auth_challenges_kind_check",
    "ALTER TABLE auth_challenges ADD CONSTRAINT auth_challenges_kind_check "
    "CHECK (kind IN ('register', 'authenticate', 'bootstrap'))",
    "DROP INDEX users_signup_skeleton",
    "ALTER TABLE users DROP COLUMN handle_skeleton",
    "ALTER TABLE users DROP COLUMN state",
    "ALTER TABLE users DROP COLUMN source",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
