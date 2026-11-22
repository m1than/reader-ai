"""SRV-AUTH database: migrations, schema ownership, startup user sync, cleanup.

@trace FR-AUTH-1 FR-AUTH-17 INV-AUTH-8 INV-AUTH-9 INV-AUTH-5
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

import pytest
from auth_helpers import REPO, execute, ready_engine, scalar, settings_for, target
from conftest import Pg
from ruamel.yaml import YAML

pytestmark = pytest.mark.anyio


def test_version_locations_are_a_declared_list() -> None:
    import reader.db as db

    ours, theirs = db.VERSION_LOCATIONS[:4], db.VERSION_LOCATIONS[4:]
    relative = [p.as_posix().split("/reader/", 1)[1] for p in ours]
    # SRV-LESSON, SRV-EVENTS and SRV-SEARCH paired changes (FR-AUTH-1 notes)
    assert relative == [
        "db/migrations/versions", "lessons/migrations", "jobs/migrations", "search/migrations",
    ]
    assert [p.as_posix().rsplit("/procrastinate/", 1)[1] for p in theirs] == ["contrib/alembic/versions"]
    assert all(p.is_dir() for p in db.VERSION_LOCATIONS)


def test_engine_hides_bound_parameters(pg: Pg) -> None:
    # Paired SRV-SEARCH change (FR-AUTH-1 note): SQL errors never log bound parameters.
    from reader.db import make_engine

    engine = make_engine(target(pg))
    assert engine.sync_engine.hide_parameters is True


def test_migration_lock_namespace_is_fixed() -> None:
    import reader.db as db

    assert isinstance(db.MIGRATION_LOCK_NAMESPACE, int)
    assert -(2**31) <= db.MIGRATION_LOCK_NAMESPACE < 2**31
    assert db.MIGRATION_LOCK_NAMESPACE != 0


def test_dsn_never_contains_the_password(pg: Pg) -> None:
    from reader.db import make_engine

    canary = "CANARY-db-" + "q" * 12
    engine = make_engine(target(pg, password=canary))
    assert canary not in str(engine.url)
    assert canary not in engine.url.render_as_string(hide_password=False)
    assert canary not in repr(engine)


async def test_migrate_reaches_all_heads_and_is_idempotent(pg: Pg) -> None:
    from reader.db import at_heads, make_engine, migrate

    engine = make_engine(target(pg))
    assert await at_heads(engine) is False
    await migrate(engine)
    assert await at_heads(engine) is True
    await migrate(engine)
    assert await at_heads(engine) is True
    await engine.dispose()


async def test_not_at_heads_when_one_branch_is_missing(pg: Pg) -> None:
    """Auth applied, jobs missing: readiness compares every effective head, not just one branch."""
    from reader.db import at_heads, make_engine, migrate

    engine = make_engine(target(pg))
    await migrate(engine)
    await execute(engine, "DELETE FROM alembic_version WHERE version_num = 'jobs_0001'")
    assert await at_heads(engine) is False
    await engine.dispose()


async def test_migration_lock_wait_is_bounded(pg: Pg, monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy import text

    import reader.db as db

    engine = db.make_engine(target(pg))
    monkeypatch.setattr(db, "MIGRATION_LOCK_WAIT_S", 0.5)
    lock = {"ns": db.MIGRATION_LOCK_NAMESPACE, "key": db.MIGRATION_LOCK_KEY}
    async with engine.connect() as holder:
        await holder.execute(text("SELECT pg_advisory_lock(:ns, :key)"), lock)
        await holder.commit()
        with pytest.raises(db.MigrationError) as caught:
            await db.migrate(engine)
        assert caught.value.code == "migration_lock_timeout"
        await holder.execute(text("SELECT pg_advisory_unlock(:ns, :key)"), lock)
        await holder.commit()
    await engine.dispose()


async def test_concurrent_migrations_serialize(pg: Pg) -> None:
    import anyio

    from reader.db import at_heads, make_engine, migrate

    engines = [make_engine(target(pg)) for _ in range(3)]
    async with anyio.create_task_group() as tg:
        for e in engines:
            tg.start_soon(migrate, e)
    assert await at_heads(engines[0]) is True
    for e in engines:
        await e.dispose()


def _system_patterns() -> list[str]:
    patterns: list[str] = []
    for spec in sorted((REPO / "spec" / "modules").glob("*.yaml")):
        doc = YAML(typ="safe").load(spec.read_text(encoding="utf-8")) or {}
        for name, table in (doc.get("tables") or {}).items():
            if isinstance(table, dict) and table.get("system"):
                patterns.append(name)
    return patterns


async def test_every_non_system_table_has_owner_id(pg: Pg, tmp_path: Path) -> None:
    engine = await ready_engine(pg, settings_for(tmp_path))
    from sqlalchemy import text

    patterns = _system_patterns()
    assert {"users", "auth_challenges", "bootstrap_tokens", "auth_failures"} <= set(patterns)
    async with engine.connect() as conn:
        tables = [
            r[0]
            for r in await conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
                    " AND table_type='BASE TABLE' AND table_name <> 'alembic_version'"
                )
            )
        ]
        owned = {
            r[0]
            for r in await conn.execute(
                text(
                    "SELECT c.table_name FROM information_schema.columns c WHERE c.table_schema='public'"
                    " AND c.column_name='owner_id' AND c.is_nullable='NO'"
                )
            )
        }
        fk = {
            r[0]
            for r in await conn.execute(
                text(
                    "SELECT tc.table_name FROM information_schema.table_constraints tc"
                    " JOIN information_schema.key_column_usage k ON k.constraint_name=tc.constraint_name"
                    " JOIN information_schema.constraint_column_usage u"
                    " ON u.constraint_name=tc.constraint_name"
                    " WHERE tc.constraint_type='FOREIGN KEY' AND k.column_name='owner_id'"
                    " AND u.table_name='users' AND u.column_name='id'"
                )
            )
        }
    assert {"webauthn_credentials", "sessions", "invitations"} <= set(tables)
    for table in tables:
        if any(fnmatch.fnmatchcase(table, p) for p in patterns):
            continue
        assert table in owned and table in fk, f"{table} lacks a NOT NULL owner_id FK to users.id"
    await engine.dispose()


async def test_startup_syncs_users_and_keeps_ids(pg: Pg, tmp_path: Path) -> None:
    from reader.auth.store import prepare

    settings = settings_for(tmp_path)
    engine = await ready_engine(pg, settings)
    rows = await scalar(engine, "SELECT count(*) FROM users")
    assert rows == 2
    before = await scalar(
        engine, "SELECT id::text || encode(webauthn_user_id,'hex') FROM users WHERE handle='ivan'"
    )
    assert await scalar(engine, "SELECT octet_length(webauthn_user_id) FROM users WHERE handle='ivan'") == 64
    await prepare(engine, settings)
    after = await scalar(
        engine, "SELECT id::text || encode(webauthn_user_id,'hex') FROM users WHERE handle='ivan'"
    )
    assert before == after
    assert await scalar(engine, "SELECT role FROM users WHERE handle='live-check'") == "learner"
    assert await scalar(engine, "SELECT consent_evaluation FROM users WHERE handle='ivan'") is True
    assert await scalar(engine, "SELECT consent_evaluation FROM users WHERE handle='live-check'") is False
    assert await scalar(engine, "SELECT bool_or(consent_tuning) FROM users") is False
    await engine.dispose()


async def test_removed_user_is_disabled_and_kept(pg: Pg, tmp_path: Path) -> None:
    from reader.auth.store import prepare

    engine = await ready_engine(pg, settings_for(tmp_path / "a"))
    uid = await scalar(engine, "SELECT id FROM users WHERE handle='live-check'")
    await execute(
        engine,
        "INSERT INTO sessions (id_sha256, owner_id, created_at, expires_at) "
        "VALUES (sha256('s'), :u, now(), now() + interval '1 day')",
        u=uid,
    )
    await prepare(engine, settings_for(tmp_path / "b", users=(("ivan", "owner"),)))
    assert await scalar(engine, "SELECT disabled_at IS NOT NULL FROM users WHERE handle='live-check'") is True
    assert (
        await scalar(engine, "SELECT count(*) FROM sessions WHERE owner_id=:u AND revoked_at IS NULL", u=uid)
        == 0
    )
    assert await scalar(engine, "SELECT count(*) FROM sessions WHERE owner_id=:u", u=uid) == 1
    await prepare(engine, settings_for(tmp_path / "c"))
    assert await scalar(engine, "SELECT disabled_at IS NULL FROM users WHERE handle='live-check'") is True
    await engine.dispose()


async def test_cleanup_deletes_only_expired_rows(pg: Pg, tmp_path: Path) -> None:
    from reader.auth.store import cleanup

    engine = await ready_engine(pg, settings_for(tmp_path))
    uid = await scalar(engine, "SELECT id FROM users WHERE handle='ivan'")
    await execute(
        engine,
        """
        INSERT INTO auth_challenges (ceremony_sha256, kind, challenge, expires_at, consumed_at) VALUES
          (sha256('old'), 'authenticate', '\\x00', now() - interval '1 hour', NULL),
          (sha256('used'), 'authenticate', '\\x00', now() + interval '1 min', now() - interval '10 min'),
          (sha256('live'), 'authenticate', '\\x00', now() + interval '1 min', NULL)""",
    )
    await execute(
        engine,
        """
        INSERT INTO bootstrap_tokens (token_sha256, user_id, expires_at) VALUES
          (sha256('t-old'), :u, now() - interval '1 hour'),
          (sha256('t-live'), :u, now() + interval '5 min')""",
        u=uid,
    )
    await execute(
        engine,
        """
        INSERT INTO sessions (id_sha256, owner_id, created_at, expires_at, revoked_at) VALUES
          (sha256('s-exp'), :u, now() - interval '20 day', now() - interval '1 day', NULL),
          (sha256('s-rev'), :u, now(), now() + interval '1 day', now()),
          (sha256('s-live'), :u, now(), now() + interval '1 day', NULL)""",
        u=uid,
    )
    await execute(
        engine,
        "INSERT INTO auth_failures (reason, at) VALUES ('malformed', now() - interval '2 hour'),"
        " ('malformed', now())",
    )
    await cleanup(engine)
    assert await scalar(engine, "SELECT count(*) FROM auth_challenges") == 1
    assert await scalar(engine, "SELECT count(*) FROM bootstrap_tokens") == 1
    assert await scalar(engine, "SELECT count(*) FROM sessions") == 1
    assert await scalar(engine, "SELECT count(*) FROM auth_failures") == 1
    await engine.dispose()


async def test_lifespan_runs_cleanup(pg: Pg, tmp_path: Path) -> None:
    from reader.api.app import create_app

    settings = settings_for(tmp_path)
    engine = await ready_engine(pg, settings)
    await execute(
        engine, "INSERT INTO auth_failures (reason, at) VALUES ('malformed', now() - interval '2 hour')"
    )
    app = create_app(settings, engine, run_cleanup_task=True)
    async with app.router.lifespan_context(app):
        import anyio

        with anyio.fail_after(5):
            while await scalar(engine, "SELECT count(*) FROM auth_failures") != 0:
                await anyio.sleep(0.05)
    await engine.dispose()
