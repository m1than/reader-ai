"""SRV-AUTH FR-AUTH-32 [live] cleanup: web_live.py deletes only the sign-up accounts a run created, by id.

An id that names a configured user deletes nothing. The `psql` the cleanup calls is replaced by one that runs
the same SQL through the test container's psql against the test database, so Postgres itself checks the
guards.

@trace FR-AUTH-32
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest
from auth_helpers import REPO, execute, ready_engine, scalar, settings_for, target
from conftest import Pg
from signup_helpers import insert_pending, user_id

SNAPSHOT = (
    "SELECT (SELECT count(*) FROM users) || ':' || (SELECT count(*) FROM webauthn_credentials) || ':' || "
    "(SELECT count(*) FROM sessions)"
)


def _web_live() -> Any:
    sys.path.insert(0, str(REPO / "e2e" / "live" / "lib"))
    return importlib.import_module("web_live")


async def _give_config_user_rows(engine: Any, config_id: Any) -> None:
    """A credential and a session for the configured learner, which the cleanup must never touch."""
    await insert_pending(engine, "lc-ffffffff")
    await execute(
        engine,
        "UPDATE webauthn_credentials SET owner_id = :c WHERE owner_id = "
        "(SELECT id FROM users WHERE handle = 'lc-ffffffff')",
        c=config_id,
    )
    await execute(engine, "DELETE FROM users WHERE handle = 'lc-ffffffff'")
    await execute(
        engine,
        "INSERT INTO sessions (id_sha256, owner_id, expires_at) "
        "VALUES ('\\x01', :c, now() + interval '1 day')",
        c=config_id,
    )


def _seed(pg: Pg, tmp_path: Path) -> dict[str, str]:
    async def run() -> dict[str, str]:
        engine = await ready_engine(pg, settings_for(tmp_path / "seed"))
        await insert_pending(engine, "lc-0a1b2c3d")
        await insert_pending(engine, "lc-9f8e7d6c", state="active")
        config_id = await user_id(engine, "live-check")
        await _give_config_user_rows(engine, config_id)
        ids = {
            "config": str(config_id),
            "owner": str(await user_id(engine, "ivan")),
            "pending": str(await user_id(engine, "lc-0a1b2c3d")),
            "active": str(await user_id(engine, "lc-9f8e7d6c")),
        }
        await engine.dispose()
        return ids

    return anyio.run(run)


def _snapshot(pg: Pg) -> str:
    async def run() -> str:
        from reader.db import make_engine

        engine = make_engine(target(pg))
        value = str(await scalar(engine, SNAPSHOT))
        await engine.dispose()
        return value

    return anyio.run(run)


@pytest.fixture
def fake_psql(pg: Pg, pg_container: tuple[Pg, Any], monkeypatch: pytest.MonkeyPatch) -> Any:
    _, container = pg_container
    web_live = _web_live()

    def psql(_project: str, sql: str) -> tuple[int, str]:
        code, out = container.exec(
            ["psql", "-U", "postgres", "-d", pg.database, "-v", "ON_ERROR_STOP=1", "-At", "-c", sql]
        )
        return code, out.decode("utf-8", "replace").strip()

    monkeypatch.setattr(web_live, "psql", psql)
    return web_live


def _created(out: Path, created: dict[str, Any]) -> None:
    out.mkdir()
    (out / "signup-created.json").write_text(json.dumps(created), "utf-8")


def test_configured_ids_delete_nothing(
    pg: Pg, tmp_path: Path, fake_psql: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    ids = _seed(pg, tmp_path)
    before = _snapshot(pg)
    out = tmp_path / "out"
    _created(out, {"handles": ["live-check", "ivan"], "ids": [ids["config"], ids["owner"]]})
    fake_psql.cleanup_signups("project", out)
    assert _snapshot(pg) == before
    assert "0 deleted by id, 0 left" in capsys.readouterr().out


def test_run_accounts_are_deleted_by_id_and_nothing_else(
    pg: Pg, tmp_path: Path, fake_psql: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    ids = _seed(pg, tmp_path)
    out = tmp_path / "out"
    # The pending id is learned by handle only; the active id is given; a configured id is mixed in; malformed
    # entries (a pattern, an upper-case handle, a non-string, a uuid with a suffix or a trailing newline,
    # which `$` alone would accept) are ignored.
    _created(
        out,
        {
            "handles": ["lc-0a1b2c3d", "lc-9f8e7d6c", "lc-%", "LC-0A1B2C3D", 7, "lc-0a1b2c3d" + chr(10)],
            "ids": [ids["active"], ids["config"], f"{ids['pending']}x", ids["pending"] + chr(10), None],
        },
    )
    fake_psql.cleanup_signups("project", out)
    printed = capsys.readouterr().out
    expected = "PASS browser: signup cleanup removed only the run's accounts\t2 deleted by id, 0 left"
    assert expected in printed
    # Left: the owner and the configured learner, with the learner's credential and session.
    assert _snapshot(pg) == "2:1:1"
    assert not (out / "signup-created.json").exists()
