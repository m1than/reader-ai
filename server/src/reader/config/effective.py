"""The settings overlay (SRV-CONFIG FR-CONFIG-26, ADR-0036): the only module that reads the overridable
keys of a loaded reader.yaml.

`file_settings(config)` takes the file's values (defaults included); `apply(file, overrides)` lays the stored
overrides on top, each validated with the file's own models, narrowed for sign-up, or ignored with a reason.
Both are pure and shared with the CLI (`status_lines`). `effective_settings(conn, config)` and
`effective_from(conn, file)` add the one database read. Log lines carry keys and reasons only.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal
from urllib.parse import urlsplit

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from reader.config.model import Budgets, Observability, ReaderConfig, Task

log = logging.getLogger("reader.config")

SignupMode = Literal["open", "off"]
Reason = Literal["not_in_file", "invalid", "narrowed"]
SIGNUP: Final = "sharing.signup"
RETENTION: Final = "observability.retention_days"
MONTHLY: Final = "budgets.monthly_usd"
WARN: Final = "budgets.warn_at"
FIXED_KEYS: Final = (SIGNUP, RETENTION, MONTHLY, WARN)
# `tasks.<name>.budget_usd_per_lesson`; the name follows SRV-EVENTS's audit pattern for a setting key.
TASK_KEY: Final = re.compile(r"tasks\.([a-z0-9][a-z0-9_-]{0,63})\.budget_usd_per_lesson")
# The overlay's own bound on a number (FR-CONFIG-26): below 2^53, so the settings page reads it exactly.
OVERRIDE_MAX: Final = Decimal(10) ** 15
_ABSENT: Final = object()


@dataclass(frozen=True)
class Settings:
    """The values of the overridable keys: the file's (defaults included), or the file's with overrides."""

    signup: SignupMode
    retention_days: int
    monthly_usd: Decimal | None
    warn_at: Decimal | None
    task_budgets: Mapping[str, Decimal | None]  # every task in reader.yaml
    applied: Mapping[str, Any] = field(default_factory=dict)  # key → the stored JSON value in effect
    ignored: Mapping[str, Reason] = field(default_factory=dict)


class Refused(Exception):
    """A set request the overlay refuses: `not_overridable`, `value_invalid` (with `field`) or
    `file_narrower`."""

    def __init__(self, code: str, field_name: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.field = field_name


# The file ---------------------------------------------------------------------------------------------------


def file_signup(config: ReaderConfig) -> tuple[SignupMode | None, bool]:
    """FR-AUTH-23's rule: (mode, absent). The mode is None for a value SRV-AUTH refuses at startup."""
    value = config.sharing.get("signup", _ABSENT)
    if value is _ABSENT:
        host = (urlsplit(config.instance.url).hostname or "").lower()
        return ("open" if host == "localhost" else "off"), True
    if value is False or value == "off":
        return "off", False
    if value == "open":
        return "open", False
    return None, False


def signup_in_file(config: ReaderConfig) -> bool:
    return "signup" in config.sharing


def file_settings(config: ReaderConfig) -> Settings:
    mode, _ = file_signup(config)
    budgets = config.budgets
    return Settings(
        signup=mode or "off",
        retention_days=int(config.observability.retention_days),
        monthly_usd=budgets.monthly_usd if budgets else None,
        warn_at=budgets.warn_at if budgets else None,
        task_budgets={name: task.budget_usd_per_lesson for name, task in config.tasks.items()},
    )


def file_value(file: Settings, key: str) -> Any:
    """The file's value of an overridable key as JSON (None for a task not in the file)."""
    if key == SIGNUP:
        return file.signup
    if key == RETENTION:
        return file.retention_days
    if key == MONTHLY:
        return as_json(file.monthly_usd)
    if key == WARN:
        return as_json(file.warn_at)
    match = TASK_KEY.fullmatch(key)
    return as_json(file.task_budgets.get(match.group(1))) if match else None


def as_json(value: Decimal | None) -> int | float | None:
    """A Decimal as a JSON number: an int when integral, else a float."""
    if value is None:
        return None
    return int(value) if value == value.to_integral_value() else float(value)


# Validation -------------------------------------------------------------------------------------------------


def overridable(file: Settings, key: str) -> bool:
    if key in FIXED_KEYS:
        return True
    match = TASK_KEY.fullmatch(key)
    return match is not None and match.group(1) in file.task_budgets


def _decimal(value: Any) -> Decimal | None:
    """A JSON number (never a boolean or a string) through its shortest decimal text."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _valid(file: Settings, key: str, value: Any) -> bool:
    """`value` validates with the file's own models for `key` (the other budget value from `file`)."""
    try:
        if key == SIGNUP:
            return value in ("open", "off")
        if key == RETENTION:
            return _valid_retention(value)
        number = _decimal(value)
        if number is None or not number.is_finite() or number >= OVERRIDE_MAX:
            return False
        if key == MONTHLY:
            Budgets.model_validate({"monthly_usd": number, "warn_at": file.warn_at})
        elif key == WARN:
            if file.monthly_usd is None:
                return False  # a `budgets` section needs its monthly_usd
            Budgets.model_validate({"monthly_usd": file.monthly_usd, "warn_at": number})
        else:
            Task.model_validate({"model": "x", "budget_usd_per_lesson": number})
    except ValidationError:
        return False
    return True


def check(file: Settings, key: str, value: Any) -> Any:
    """A set request's value, validated against the file; raises Refused."""
    if not overridable(file, key):
        raise Refused("not_overridable")
    if not _valid(file, key, value):
        raise Refused("value_invalid", key)
    if key == SIGNUP and value == "open" and file.signup == "off":
        raise Refused("file_narrower")
    return value


def _reason(file: Settings, key: str, value: Any) -> Reason | None:
    if key not in FIXED_KEYS:
        match = TASK_KEY.fullmatch(key)
        if match is None:
            return "invalid"
        if match.group(1) not in file.task_budgets:
            return "not_in_file"
    if not _valid(file, key, value):
        return "invalid"
    if key == SIGNUP and value == "open" and file.signup == "off":
        return "narrowed"
    return None


def apply(file: Settings, overrides: Mapping[str, Any]) -> Settings:
    """Lay the overrides over the file's values: override ?? file ?? default, each one checked on its own."""
    applied: dict[str, Any] = {}
    ignored: dict[str, Reason] = {}
    for key in sorted(overrides):
        reason = _reason(file, key, overrides[key])
        if reason is None:
            applied[key] = overrides[key]
        else:
            ignored[key] = reason
    tasks = dict(file.task_budgets)
    for key, value in applied.items():
        match = TASK_KEY.fullmatch(key)
        if match:
            tasks[match.group(1)] = _decimal(value)
    return replace(
        file,
        signup=applied.get(SIGNUP, file.signup),
        retention_days=applied.get(RETENTION, file.retention_days),
        monthly_usd=_decimal(applied[MONTHLY]) if MONTHLY in applied else file.monthly_usd,
        warn_at=_decimal(applied[WARN]) if WARN in applied else file.warn_at,
        task_budgets=tasks,
        applied=applied,
        ignored=ignored,
    )


def _shown(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def status_lines(file: Settings, overrides: Mapping[str, Any]) -> list[str]:
    """OPS-CLI's `reader up` / `reader status` lines (FR-CONFIG-26), by key."""
    result = apply(file, overrides)
    lines = []
    for key in sorted(overrides):
        if key in result.ignored:
            lines.append(f"override {key}: ignored ({result.ignored[key]})")
        else:
            shown, in_file = _shown(overrides[key]), _shown(file_value(file, key))
            lines.append(f"override {key}: {shown} (reader.yaml: {in_file})")
    return lines


# The database -----------------------------------------------------------------------------------------------

_logged: set[tuple[str, str]] = set()


def reset_logged() -> None:
    """Forget which ignored overrides were logged (a new process start; tests)."""
    _logged.clear()


async def read_overrides(conn: AsyncConnection) -> dict[str, tuple[Any, int]]:
    rows = await conn.execute(text("SELECT key, value, version FROM settings_overrides ORDER BY key"))
    return {str(r[0]): (r[1], int(r[2])) for r in rows}


async def effective_from(conn: AsyncConnection, file: Settings) -> Settings:
    """`file` (a process's own snapshot of the file) with the stored overrides; logs each ignored key once."""
    stored = await read_overrides(conn)
    result = apply(file, {key: value for key, (value, _) in stored.items()})
    for key, reason in result.ignored.items():
        if (key, reason) not in _logged:
            _logged.add((key, reason))
            log.info("config override_ignored %s %s", key, reason)
    return result


async def effective_settings(conn: AsyncConnection, config: ReaderConfig) -> Settings:
    return await effective_from(conn, file_settings(config))


def file_retention_days(config: Any) -> int:
    """`observability.retention_days` of the file (SRV-OBS reads it at setup; tests pass config-shaped
    objects with only `observability`)."""
    return int(config.observability.retention_days)


async def retention_days(conn: AsyncConnection, file_days: int) -> int:
    """FR-OBS-11 per run and FR-OBS-1 per request (paired): `file_days` (the process's snapshot of the file)
    or a valid override; an invalid one is ignored and logged once."""
    row = (
        await conn.execute(text("SELECT value FROM settings_overrides WHERE key = :k"), {"k": RETENTION})
    ).first()
    if row is None:
        return file_days
    if _valid_retention(row[0]):
        return int(row[0])
    if (RETENTION, "invalid") not in _logged:
        _logged.add((RETENTION, "invalid"))
        log.info("config override_ignored %s invalid", RETENTION)
    return file_days


def _valid_retention(value: Any) -> bool:
    try:
        Observability.model_validate({"retention_days": value})
    except ValidationError:
        return False
    return True


async def signup_mode(conn: AsyncConnection, file_mode: SignupMode) -> SignupMode:
    """FR-AUTH-23 per request: the api's startup value of the switch, narrowed by an override."""
    row = (
        await conn.execute(text("SELECT value FROM settings_overrides WHERE key = :k"), {"k": SIGNUP})
    ).first()
    if file_mode == "off" or row is None:
        return file_mode  # an `open` override never opens a switch the file keeps off (narrowing)
    return "off" if row[0] == "off" else "open" if row[0] == "open" else file_mode
