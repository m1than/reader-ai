"""SRV-AUTH first migration: users, credentials, sessions, ceremonies, tokens, failures, invitations.

Revision ID: 0001_auth
Revises:
"""

from __future__ import annotations

from alembic import op

revision = "0001_auth"
down_revision = None
branch_labels = ("reader",)
depends_on = None

# One statement per op.execute: asyncpg runs prepared statements, which hold a single command.
UPGRADE = (
    """CREATE TABLE users (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        handle text NOT NULL UNIQUE,
        role text NOT NULL CHECK (role IN ('owner', 'learner')),
        webauthn_user_id bytea NOT NULL UNIQUE CHECK (octet_length(webauthn_user_id) = 64),
        display_name text NOT NULL,
        consent_evaluation boolean NOT NULL,
        consent_tuning boolean NOT NULL DEFAULT false,
        disabled_at timestamptz NULL,
        created_at timestamptz NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE webauthn_credentials (
        credential_id bytea PRIMARY KEY,
        owner_id uuid NOT NULL REFERENCES users (id),
        public_key bytea NOT NULL,
        sign_count bigint NOT NULL CHECK (sign_count >= 0),
        transports text[] NOT NULL DEFAULT '{}',
        aaguid uuid NOT NULL,
        backed_up boolean NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        last_used_at timestamptz NULL
    )""",
    "CREATE INDEX webauthn_credentials_owner ON webauthn_credentials (owner_id)",
    """CREATE TABLE sessions (
        id_sha256 bytea PRIMARY KEY,
        owner_id uuid NOT NULL REFERENCES users (id),
        created_at timestamptz NOT NULL DEFAULT now(),
        expires_at timestamptz NOT NULL,
        revoked_at timestamptz NULL
    )""",
    "CREATE INDEX sessions_owner ON sessions (owner_id)",
    """CREATE TABLE auth_challenges (
        ceremony_sha256 bytea PRIMARY KEY,
        kind text NOT NULL CHECK (kind IN ('register', 'authenticate', 'bootstrap')),
        challenge bytea NOT NULL,
        user_id uuid NULL REFERENCES users (id),
        bootstrap_token_sha256 bytea NULL,
        expires_at timestamptz NOT NULL,
        consumed_at timestamptz NULL
    )""",
    """CREATE TABLE bootstrap_tokens (
        token_sha256 bytea PRIMARY KEY,
        user_id uuid NOT NULL REFERENCES users (id),
        expires_at timestamptz NOT NULL,
        consumed_at timestamptz NULL,
        revoked_at timestamptz NULL
    )""",
    """CREATE TABLE auth_failures (
        id bigserial PRIMARY KEY,
        reason text NOT NULL,
        at timestamptz NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX auth_failures_at ON auth_failures (at)",
    """CREATE TABLE invitations (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        owner_id uuid NOT NULL REFERENCES users (id),
        created_at timestamptz NOT NULL DEFAULT now()
    )""",
)

DOWNGRADE = (
    "DROP TABLE invitations",
    "DROP TABLE auth_failures",
    "DROP TABLE bootstrap_tokens",
    "DROP TABLE auth_challenges",
    "DROP TABLE sessions",
    "DROP TABLE webauthn_credentials",
    "DROP TABLE users",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
