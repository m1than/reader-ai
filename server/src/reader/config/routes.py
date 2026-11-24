"""The owner's settings view and overlay routes (SRV-CONFIG FR-CONFIG-25, FR-CONFIG-26), declared as
data for SRV-AUTH's ROUTERS.

The view is an allow-listed projection (`SETTINGS_VIEW`): no user, provider, secret reference, material,
kernel or instance URL ever reaches the body, and free text from the file is shown only when it is a plain
name (and, for a model, a priced one). The overlay writes `settings_overrides` with a version check and an
audit row in the same transaction; a no-op writes nothing.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from sqlalchemy import text
from starlette.responses import JSONResponse, Response

from reader.api.http import Ctx, Handler, Route, error
from reader.config import ConfigError, ReaderConfig, effective, load_config
from reader.jobs import audit

DEFAULT_CONFIG: Final = "/etc/reader/reader.yaml"
HIDDEN: Final = "(hidden)"
_NAME: Final = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
START, WORKER, EACH_JOB, FIXED = "start", "worker start and the daily job", "each job", "fixed"
# (name, file path or constant, read_when): the declared projection, in body order; `tasks.*` expands per
# task in file order.
SETTINGS_VIEW: Final = (
    ("signup", "sharing.signup", START),
    ("observability.enabled", "observability.enabled", WORKER),
    ("observability.retention_days", "observability.retention_days", WORKER),
    ("budgets.monthly_usd", "budgets.monthly_usd", EACH_JOB),
    ("budgets.warn_at", "budgets.warn_at", EACH_JOB),
    ("tasks.*.model", "tasks.*.model", EACH_JOB),
    ("tasks.*.budget_usd_per_lesson", "tasks.*.budget_usd_per_lesson", EACH_JOB),
    ("pipeline.run_cap_usd", "SRV-PIPELINE bounds.run_cap_usd", FIXED),
    ("tutor.question_cap_usd", "SRV-TUTOR question_cap_usd", FIXED),
    ("tutor.daily_cap_usd", "SRV-TUTOR daily_cap_usd", FIXED),
)
_UPSERT: Final = text(
    "INSERT INTO settings_overrides (key, value, updated_by, version) VALUES (:k, CAST(:v AS jsonb), :u, 1)"
    " ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_by = excluded.updated_by,"
    " updated_at = now(), version = settings_overrides.version + 1"
)


def _fixed() -> dict[str, Decimal]:
    from reader.pipeline.routes import RUN_CAP_USD
    from reader.tutor.params import DAILY_CAP_USD, QUESTION_CAP_USD

    return {
        "pipeline.run_cap_usd": RUN_CAP_USD,
        "tutor.question_cap_usd": QUESTION_CAP_USD,
        "tutor.daily_cap_usd": DAILY_CAP_USD,
    }


def _plain(value: str) -> bool:
    if _NAME.fullmatch(value) is None or "://" in value:
        return False
    return not value.startswith(("secret:", "sk-"))


def _model(value: str) -> str:
    from reader.agent.pricing import ALIASES, load_pricing

    known = ALIASES.get(value, value) in load_pricing().models
    return value if _plain(value) and known else HIDDEN


def file_of(config: ReaderConfig, signup: effective.SignupMode) -> effective.Settings:
    """The file's values with the sign-up switch the api read at its start (FR-CONFIG-25)."""
    f = effective.file_settings(config)
    return effective.Settings(signup, f.retention_days, f.monthly_usd, f.warn_at, f.task_budgets)


def _overrides(stored: Mapping[str, tuple[Any, int]]) -> dict[str, Any]:
    return {key: value for key, (value, _) in stored.items()}


def project(
    config: ReaderConfig, signup: effective.SignupMode, stored: Mapping[str, tuple[Any, int]]
) -> list[dict[str, Any]]:
    """The view's rows (SETTINGS_VIEW), with the stored overrides applied or marked ignored."""
    file = file_of(config, signup)
    result = effective.apply(file, _overrides(stored))
    obs_set = config.observability.model_fields_set

    def row(name: str, key: str | None, value: Any, at_file: Any, in_file: bool, when: str) -> dict[str, Any]:
        source = FIXED if when == FIXED else "reader.yaml" if in_file else "default"
        return {
            "name": name,
            "value": value,
            "file_value": at_file,
            "source": "override" if key in result.applied else source,
            "key": key,
            "read_when": when,
            "version": stored[key][1] if key is not None and key in stored else 0,
            "ignored": result.ignored.get(key) if key is not None else None,
        }

    def setting(name: str, key: str, in_file: bool, when: str) -> dict[str, Any]:
        value = effective.file_value(result, key)
        return row(name, key, value, effective.file_value(file, key), in_file, when)

    enabled = config.observability.enabled
    rows = [
        setting("signup", effective.SIGNUP, effective.signup_in_file(config), START),
        row("observability.enabled", None, enabled, enabled, "enabled" in obs_set, WORKER),
        setting("observability.retention_days", effective.RETENTION, "retention_days" in obs_set, WORKER),
        setting("budgets.monthly_usd", effective.MONTHLY, file.monthly_usd is not None, EACH_JOB),
        setting("budgets.warn_at", effective.WARN, file.warn_at is not None, EACH_JOB),
    ]
    for name, task in config.tasks.items():
        shown = name if _plain(name) else HIDDEN
        model = _model(task.model)
        rows.append(row(f"tasks.{shown}.model", None, model, model, True, EACH_JOB))
        key = f"tasks.{name}.budget_usd_per_lesson"
        budget = file.task_budgets.get(name)
        if shown != HIDDEN and effective.TASK_KEY.fullmatch(key):
            rows.append(setting(key, key, budget is not None, EACH_JOB))
        else:  # not overridable: its value only
            value = effective.as_json(budget)
            name_shown = f"tasks.{shown}.budget_usd_per_lesson"
            rows.append(row(name_shown, None, value, value, budget is not None, EACH_JOB))
    for name, amount in _fixed().items():
        value = effective.as_json(amount)
        rows.append(row(name, None, value, value, True, FIXED))
    return rows


def _config(ctx: Ctx) -> ReaderConfig | None:
    path = getattr(ctx.request.app.state, "reader_config_path", None)
    try:
        return load_config(Path(path or os.environ.get("READER_CONFIG", DEFAULT_CONFIG)))
    except ConfigError, OSError:
        return None


def _owner_only(handler: Handler) -> Handler:
    async def wrapped(ctx: Ctx) -> Response:
        if ctx.signed_in.role != "owner":
            return error(403, "owner_only")
        return await handler(ctx)

    return wrapped


async def view(ctx: Ctx) -> Response:
    config = _config(ctx)
    if config is None:
        return error(503, "config_unavailable")
    async with ctx.engine.connect() as conn:
        stored = await effective.read_overrides(conn)
    rows = project(config, ctx.settings.signup, stored)
    shown = {r["key"] for r in rows}
    ignored = effective.apply(file_of(config, ctx.settings.signup), _overrides(stored)).ignored
    # Overrides with no row of their own (a task removed from reader.yaml): listed, so the owner resets them.
    extra = [
        {"key": key, "value": value, "reason": ignored.get(key, "invalid"), "version": version}
        for key, (value, version) in sorted(stored.items())
        if key not in shown
    ]
    return JSONResponse({"rows": rows, "ignored": extra})


def _version(body: object) -> int | None:
    value = body.get("version") if isinstance(body, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


async def set_override(ctx: Ctx) -> Response:
    key = str(ctx.request.path_params.get("key", ""))
    body = await ctx.json()
    config = _config(ctx)
    if config is None:
        return error(503, "config_unavailable")
    file = file_of(config, ctx.settings.signup)
    if not effective.overridable(file, key):
        return error(400, "not_overridable")
    version = _version(body)
    if not isinstance(body, dict) or "value" not in body or version is None:
        return error(400, "body_invalid")
    try:
        value = effective.check(file, key, body["value"])
    except effective.Refused as refused:
        if refused.code == "value_invalid":
            return JSONResponse({"error": "value_invalid", "field": key}, status_code=400)
        return error(409, refused.code)  # file_narrower
    async with ctx.engine.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT value, version FROM settings_overrides WHERE key = :k FOR UPDATE"), {"k": key}
            )
        ).first()
        current = int(row[1]) if row else 0
        if version != current:
            return error(409, "stale")
        if row is not None and row[0] == value:
            return JSONResponse({"key": key, "value": value, "version": current})  # a no-op
        await conn.execute(_UPSERT, {"k": key, "v": json.dumps(value), "u": ctx.signed_in.id})
        detail = {"old": row[0] if row else None, "new": value}
        await audit.record(conn, ctx.signed_in.id, "settings.set", key, detail)
    return JSONResponse({"key": key, "value": value, "version": current + 1})


async def reset_override(ctx: Ctx) -> Response:
    key = str(ctx.request.path_params.get("key", ""))
    config = _config(ctx)
    if config is None:
        return error(503, "config_unavailable")
    file = file_of(config, ctx.settings.signup)
    async with ctx.engine.begin() as conn:
        removed = text("DELETE FROM settings_overrides WHERE key = :k RETURNING value")
        row = (await conn.execute(removed, {"k": key})).first()
        if row is None:  # idempotent; a key that is not overridable at all is refused
            if not effective.overridable(file, key):
                return error(400, "not_overridable")
            return Response(status_code=204)
        detail = {"old": row[0], "file": effective.file_value(file, key)}
        await audit.record(conn, ctx.signed_in.id, "settings.reset", key, detail)
    return Response(status_code=204)


ROUTES: Final[tuple[Route, ...]] = (
    Route("GET", "/api/settings", "session", _owner_only(view)),
    Route("POST", "/api/settings/{key}", "recent_session", _owner_only(set_override)),
    Route("DELETE", "/api/settings/{key}", "recent_session", _owner_only(reset_override)),
)
