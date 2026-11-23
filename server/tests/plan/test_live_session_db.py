# @trace FR-PLAN-9
"""The live check's owner session (e2e/live/lib/plan_live.py): created and deleted by its own id only, so the owner's
real session and passkey survive a run. Runs the helper's exact SQL against a migrated Postgres. [db]"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from .conftest import REPO, Pg, client, plan_env

pytestmark = pytest.mark.anyio


def helper() -> ModuleType:
    lib = REPO / "e2e" / "live" / "lib"
    sys.path.insert(0, str(lib))
    try:
        spec = importlib.util.spec_from_file_location("plan_live", lib / "plan_live.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(lib))


async def test_the_live_session_touches_only_its_own_row(pg: Pg, tmp_path: Path) -> None:
    from sqlalchemy import text

    live = helper()
    async with plan_env(pg, tmp_path) as env:
        owner = env.users["ivan"]
        # the owner's real passkey and session, made before the run
        async with env.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO webauthn_credentials (credential_id, owner_id, public_key, sign_count, aaguid,"
                    " backed_up) VALUES ('\\x01'::bytea, :o, '\\x02'::bytea, 0, gen_random_uuid(), true)"
                ),
                {"o": owner},
            )
            await conn.execute(
                text("INSERT INTO sessions (id_sha256, owner_id, expires_at) VALUES ('\\x03'::bytea, :o, now() + interval '1 day')"),
                {"o": owner},
            )
        token = "fake-run-token"  # noqa: S105 (a test value, never a credential)
        insert, delete = live.session_sql(token)
        assert "role = 'owner'" in insert
        assert delete.startswith("DELETE FROM sessions WHERE id_sha256 = decode(")
        assert "owner_id" not in delete and "webauthn" not in delete and "role" not in delete
        async with env.engine.begin() as conn:
            await conn.execute(text(insert))
        async with client(env, None) as c:
            me = await c.get("/api/me", cookies={"__Host-reader_session": token})
            assert me.status_code == 200 and me.json()["role"] == "owner"
        async with env.engine.begin() as conn:
            await conn.execute(text(delete))
        assert await env.scalar("SELECT count(*) FROM sessions WHERE id_sha256 = '\\x03'::bytea") == 1
        assert await env.scalar("SELECT count(*) FROM webauthn_credentials WHERE owner_id = :o", o=owner) == 1
        assert await env.scalar("SELECT count(*) FROM sessions WHERE owner_id = :o", o=owner) == 1
