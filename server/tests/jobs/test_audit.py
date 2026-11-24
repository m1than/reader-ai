"""SRV-EVENTS FR-EVENTS-30 (audit rows) and FR-EVENTS-31 (reading the audit). [unit, db]

@trace FR-EVENTS-30 FR-EVENTS-31
"""

from __future__ import annotations

import importlib
import textwrap
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from .conftest import SPEC, Pg, load_yaml, target

pytestmark = pytest.mark.anyio
ORIGIN = "https://localhost"
CONFIG = """\
version: 1
instance: { url: "https://localhost", data_dir: ./data }
users:
  - { handle: ivan, role: owner }
  - { handle: anna, role: learner }
sharing: {}
"""


@dataclass
class World:
    engine: Any
    settings: Any
    users: dict[str, uuid.UUID]


async def world(pg: Pg, tmp: Path) -> World:
    """A migrated database with users ivan (owner) and anna (learner) (as SRV-LESSON's tests build it)."""
    from sqlalchemy import text

    from reader.auth.policy import settings_from_config
    from reader.auth.store import prepare
    from reader.config import load_config
    from reader.db import make_engine, migrate

    path = tmp / "reader.yaml"
    path.write_text(textwrap.dedent(CONFIG), encoding="utf-8")
    settings = settings_from_config(load_config(path))
    engine = make_engine(target(pg))
    await migrate(engine)
    await prepare(engine, settings)
    async with engine.connect() as conn:
        users = {r[0]: r[1] for r in await conn.execute(text("SELECT handle, id FROM users"))}
    return World(engine, settings, users)


@asynccontextmanager
async def client(w: World, handle: str | None) -> AsyncIterator[httpx.AsyncClient]:
    from reader.api.app import create_app
    from reader.auth.policy import SESSION_COOKIE
    from reader.auth.store import _start_session

    app = create_app(w.settings, w.engine, run_cleanup_task=False)
    cookies: dict[str, str] = {}
    if handle:
        async with w.engine.begin() as conn:
            cookies[SESSION_COOKIE] = await _start_session(conn, w.users[handle], None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url=ORIGIN, headers={"Origin": ORIGIN}, cookies=cookies
    ) as c:
        yield c
OWNER_ID = "0b5c1f3e-6d2a-4f7e-9a41-2c8d5e7f9b10"
COURSE_ID = "5f0c1f3e-6d2a-4f7e-9a41-2c8d5e7f9b11"


def _audit() -> Any:
    try:
        return importlib.import_module("reader.jobs.audit")
    except ModuleNotFoundError as error:
        raise AssertionError(f"reader.jobs.audit is not implemented ({error.name} missing)") from None


# --- [unit] the declared table equals the spec's, and every refusal happens before any insert ---


def test_actions_table_equals_the_spec() -> None:
    audit = _audit()
    spec = load_yaml(SPEC)["audit_actions"]
    assert set(audit.AUDIT_ACTIONS) == set(spec)
    for action, row in spec.items():
        declared = audit.AUDIT_ACTIONS[action]
        assert declared.kind == row["kind"], action
        assert list(declared.detail) == list(row["detail"]), action


@pytest.mark.parametrize(
    ("action", "object_id", "detail", "reason"),
    [
        ("course.explode", COURSE_ID, {}, "unknown_action"),
        ("course.archive", "not-a-uuid", {}, "object_id"),
        ("course.archive", COURSE_ID.upper(), {}, "object_id"),
        ("course.archive", COURSE_ID, {"title": "Secret book"}, "detail_key"),
        ("course.rename", COURSE_ID, {"version": "two"}, "detail_value"),
        ("lesson.revert", COURSE_ID, {"from_sha256": "x", "to_sha256": "a" * 64}, "detail_value"),
        ("user.credential_remove", OWNER_ID, {"pid": "ABCDEF0123456789"}, "detail_value"),
        ("settings.set", "budgets.monthly_usd", {"old": "free text here", "new": 5}, "detail_value"),
        ("settings.set", "users", {"old": 1, "new": 2}, "object_id"),
        ("material.rename", "../etc", {"version": 2}, "object_id"),
        ("course.order", OWNER_ID, {"count": 1, "pad": 0}, "detail_key"),
    ],
)
def test_refusals(action: str, object_id: str, detail: dict[str, Any], reason: str) -> None:
    audit = _audit()
    with pytest.raises(audit.AuditRefused) as caught:
        audit.check(action, object_id, detail)
    assert caught.value.reason == reason


def test_detail_is_bounded() -> None:
    audit = _audit()
    with pytest.raises(audit.AuditRefused) as caught:
        audit.check("settings.set", "budgets.monthly_usd", {"old": 1, "new": 10**3000})
    assert caught.value.reason in {"detail_value", "detail_size"}


def test_allowed_value_is_the_detail_rule() -> None:
    audit = _audit()
    assert audit.allowed_value("file", 200) is True
    assert audit.allowed_value("file", None) is True
    for value in (10**20, 1e15, float("inf"), float("nan"), "free text"):
        assert audit.allowed_value("file", value) is False, value
    assert audit.allowed_value("no_such_key", 1) is False


def test_accepted_rows() -> None:
    audit = _audit()
    audit.check("course.delete", COURSE_ID, {"lessons": 3, "revisions": 4, "tutor_questions": 0,
                                             "compile_runs": 1, "spend_rows": 7, "by_material": False})
    audit.check("lesson.revert", COURSE_ID, {"from_sha256": "a" * 64, "to_sha256": "b" * 64})
    audit.check("self.session_revoke", OWNER_ID, {"sid": "0123456789abcdef", "count": 1})
    audit.check("self.credential_add", OWNER_ID, {"pid": "0123456789abcdef"})
    audit.check("settings.set", "sharing.signup", {"old": "open", "new": "off"})
    audit.check("settings.set", "tasks.draft.budget_usd_per_lesson", {"old": None, "new": 2.5})
    audit.check("material.delete", "up-0123456789ab", {"courses": 2})
    audit.check("material.rename", "bashir", {"version": 2})


@pytest.mark.parametrize(
    "material_id",
    ["bashir", "Bashir", "bashir_4th", "Башир-2025", "a" * 64, "a" * 65, "two words", "a/b", "rtl‏mark",
     "up-abc", "up-0123456789ab", "up-0123456789AB", "", ".", "..", "...", ".hidden"],
)
def test_material_id_rule_is_the_config_rule(material_id: str) -> None:
    from reader.config.checks import material_id_problem

    audit = _audit()
    upload = audit._UPLOAD_ID.fullmatch(material_id) is not None
    expected = upload or (material_id != "" and material_id_problem(material_id) is None)
    accepted = {"bashir", "Bashir", "bashir_4th", "Башир-2025", "a" * 64, "up-0123456789ab", "...", ".hidden"}
    assert expected == (material_id in accepted)
    if expected:
        audit.check("material.rename", material_id, {"version": 2})
    else:
        with pytest.raises(audit.AuditRefused) as caught:
            audit.check("material.rename", material_id, {"version": 2})
        assert caught.value.reason == "object_id"


# --- [db] the trigger, one row per applied action, rollback, and the read route ---


async def _rows(engine: Any) -> list[Any]:
    from sqlalchemy import text

    async with engine.connect() as conn:
        return list(await conn.execute(text("SELECT action, object_id, detail FROM audit_log ORDER BY id")))


async def test_record_commits_and_rolls_back_with_the_change(pg: Pg, tmp_path: Path) -> None:
    audit = _audit()
    w = await world(pg, tmp_path)
    actor = w.users["ivan"]
    async with w.engine.begin() as conn:
        await audit.record(conn, actor, "course.archive", COURSE_ID, {})
    async def failing_change() -> None:
        async with w.engine.begin() as conn:
            await audit.record(conn, actor, "course.unarchive", COURSE_ID, {})
            raise RuntimeError("the change fails")

    with pytest.raises(RuntimeError):
        await failing_change()
    rows = await _rows(w.engine)
    assert [(r[0], r[1]) for r in rows] == [("course.archive", COURSE_ID)]
    with pytest.raises(audit.AuditRefused):
        async with w.engine.begin() as conn:
            await audit.record(conn, actor, "course.archive", COURSE_ID, {"title": "x"})
    assert len(await _rows(w.engine)) == 1
    await w.engine.dispose()


@pytest.mark.parametrize("statement", ["UPDATE audit_log SET action = 'x'", "DELETE FROM audit_log",
                                       "TRUNCATE audit_log"])
async def test_trigger_refuses_changes(pg: Pg, tmp_path: Path, statement: str) -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    audit = _audit()
    w = await world(pg, tmp_path)
    async with w.engine.begin() as conn:
        await audit.record(conn, w.users["ivan"], "course.archive", COURSE_ID, {})
    with pytest.raises(DBAPIError):
        async with w.engine.begin() as conn:
            await conn.execute(text(statement))
    assert len(await _rows(w.engine)) == 1
    await w.engine.dispose()


async def test_audit_log_has_no_foreign_key_to_users(pg: Pg, tmp_path: Path) -> None:
    from sqlalchemy import text

    audit = _audit()
    w = await world(pg, tmp_path)
    async with w.engine.begin() as conn:
        await audit.record(conn, uuid.uuid4(), "course.archive", COURSE_ID, {})
        fks = await conn.execute(
            text(
                "SELECT count(*) FROM pg_constraint"
                " WHERE conrelid = 'audit_log'::regclass AND contype = 'f'"
            )
        )
        assert fks.scalar() == 0
    await w.engine.dispose()


async def test_read_route_pages_and_refuses_a_learner(pg: Pg, tmp_path: Path) -> None:
    audit = _audit()
    w = await world(pg, tmp_path)
    async with w.engine.begin() as conn:
        for _ in range(205):
            await audit.record(conn, w.users["ivan"], "course.archive", COURSE_ID, {})
    async with client(w, "ivan") as c:
        first = await c.get("/api/audit")
        assert first.status_code == 200
        body = first.json()
        assert len(body["rows"]) == 200
        ids = [r["id"] for r in body["rows"]]
        assert ids == sorted(ids, reverse=True)
        keys = {"id", "at", "actor_id", "action", "object_kind", "object_id", "detail"}
        assert set(body["rows"][0]) == keys
        assert body["rows"][0]["object_kind"] == "course"
        second = (await c.get("/api/audit", params={"before": body["next_before"]})).json()
        assert len(second["rows"]) == 5
        assert second["next_before"] is None
        assert max(r["id"] for r in second["rows"]) < min(ids)
        for bad in ("0", "-1", "x", "1.5"):
            refused = await c.get("/api/audit", params={"before": bad})
            assert (refused.status_code, refused.json()) == (400, {"error": "before_invalid"})
    async with client(w, "anna") as c:
        refused = await c.get("/api/audit")
        assert (refused.status_code, refused.json()) == (403, {"error": "owner_only"})
    async with client(w, None) as c:
        assert (await c.get("/api/audit")).status_code == 401
    await w.engine.dispose()
