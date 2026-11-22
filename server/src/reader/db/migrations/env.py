"""Alembic environment: runs on the connection `reader.db.migrate` passes in (the app's asyncpg engine)."""

from __future__ import annotations

from alembic import context
from sqlalchemy import Connection

connection = context.config.attributes.get("connection")
if not isinstance(connection, Connection):
    raise RuntimeError("reader.db.migrate passes the connection; offline mode is not supported")
context.configure(connection=connection, target_metadata=None, transaction_per_migration=True)
with context.begin_transaction():
    context.run_migrations()
