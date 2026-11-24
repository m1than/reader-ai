"""Audit rows of the management layer (SRV-EVENTS FR-EVENTS-30).

`AUDIT_ACTIONS` is the spec's `audit_actions` table as data: per action the object kind, the shape of its
object id and the only keys `detail` may hold. `check` refuses anything else before a row is built; `record`
inserts the row on the caller's connection, so it commits or rolls back with the change it describes.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

DETAIL_MAX_BYTES: Final = 2048
_UUID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_HEX16: Final = re.compile(r"[0-9a-f]{16}")
_SHA: Final = re.compile(r"[0-9a-f]{64}")
_MATERIAL: Final = re.compile(r"up-[0-9a-f]{12}|[a-z0-9][a-z0-9-]{0,62}")
# SRV-CONFIG FR-CONFIG-26's OVERRIDABLE families; a task name follows reader.yaml's task keys.
_SETTING: Final = re.compile(
    r"sharing\.signup|observability\.retention_days|budgets\.monthly_usd|budgets\.warn_at"
    r"|tasks\.[a-z0-9][a-z0-9_-]{0,63}\.budget_usd_per_lesson"
)


class AuditRefused(ValueError):
    """A row the audit table does not allow; `reason` names the rule."""

    def __init__(self, reason: str, action: str) -> None:
        super().__init__(f"audit refused: {reason} ({action})")
        self.reason = reason


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2**53


def _flag(value: Any) -> bool:
    return isinstance(value, bool)


def _setting_value(value: Any) -> bool:
    """A setting's value: null, a boolean, a bounded number or one of the sign-up enums (never free text)."""
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, int):
        return abs(value) < 2**53
    if isinstance(value, float):
        return value == value and abs(value) < 1e15
    return value in ("open", "off")


def _fullmatch(pattern: re.Pattern[str]) -> Callable[[Any], bool]:
    return lambda value: isinstance(value, str) and pattern.fullmatch(value) is not None


_VALUE_RULES: Final[dict[str, Callable[[Any], bool]]] = {
    "version": _count,
    "count": _count,
    "lessons": _count,
    "revisions": _count,
    "tutor_questions": _count,
    "compile_runs": _count,
    "spend_rows": _count,
    "courses": _count,
    "revoked_sessions": _count,
    "by_material": _flag,
    "from_sha256": _fullmatch(_SHA),
    "to_sha256": _fullmatch(_SHA),
    "lesson_id": _fullmatch(_UUID),
    "pid": _fullmatch(_HEX16),
    "sid": _fullmatch(_HEX16),
    "old": _setting_value,
    "new": _setting_value,
    "file": _setting_value,
}

_ID_RULES: Final[dict[str, re.Pattern[str]]] = {"uuid": _UUID, "material": _MATERIAL, "setting": _SETTING}


@dataclass(frozen=True)
class Action:
    kind: str
    id: str  # a key of _ID_RULES
    detail: tuple[str, ...]


def _a(kind: str, id_rule: str, *detail: str) -> Action:
    return Action(kind, id_rule, detail)


AUDIT_ACTIONS: Final[Mapping[str, Action]] = {
    "course.rename": _a("course", "uuid", "version"),
    "course.describe": _a("course", "uuid", "version"),
    "course.order": _a("owner", "uuid", "count"),
    "course.archive": _a("course", "uuid"),
    "course.unarchive": _a("course", "uuid"),
    "course.delete": _a(
        "course", "uuid", "lessons", "revisions", "tutor_questions", "compile_runs", "spend_rows",
        "by_material",
    ),
    "course.restore": _a("course", "uuid", "by_material"),
    "lesson.rename": _a("lesson", "uuid", "version"),
    "lesson.order": _a("course", "uuid", "count"),
    "lesson.skip": _a("lesson", "uuid"),
    "lesson.unskip": _a("lesson", "uuid"),
    "lesson.delete": _a("lesson", "uuid"),
    "lesson.restore": _a("lesson", "uuid"),
    "lesson.revert": _a("lesson", "uuid", "from_sha256", "to_sha256"),
    "material.rename": _a("material", "material", "version"),
    "material.delete": _a("material", "material", "courses"),
    "material.restore": _a("material", "material", "courses"),
    "compile.cancel": _a("run", "uuid", "lesson_id"),
    "tutor.clear": _a("lesson", "uuid", "count"),
    "user.sessions_revoke": _a("user", "uuid", "count"),
    "user.credential_remove": _a("user", "uuid", "pid"),
    "user.delete": _a("user", "uuid"),
    "user.enable": _a("user", "uuid"),
    "self.session_revoke": _a("user", "uuid", "sid", "count"),
    "self.credential_label": _a("user", "uuid", "pid"),
    "self.credential_remove": _a("user", "uuid", "pid", "revoked_sessions"),
    "settings.set": _a("setting", "setting", "old", "new"),
    "settings.reset": _a("setting", "setting", "old", "file"),
}


def check(action: str, object_id: str, detail: Mapping[str, Any]) -> str:
    """Validate one row; returns its compact JSON detail. Raises AuditRefused before anything is written."""
    declared = AUDIT_ACTIONS.get(action)
    if declared is None:
        raise AuditRefused("unknown_action", action)
    if not isinstance(object_id, str) or _ID_RULES[declared.id].fullmatch(object_id) is None:
        raise AuditRefused("object_id", action)
    for key, value in detail.items():
        if key not in declared.detail:
            raise AuditRefused("detail_key", action)
        if not _VALUE_RULES[key](value):
            raise AuditRefused("detail_value", action)
    encoded = json.dumps(dict(detail), separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) > DETAIL_MAX_BYTES:
        raise AuditRefused("detail_size", action)
    return encoded


async def record(
    conn: AsyncConnection, actor_id: uuid.UUID, action: str, object_id: str, detail: Mapping[str, Any]
) -> None:
    """Insert one audit row on the caller's connection (same transaction as the change)."""
    encoded = check(action, object_id, detail)
    await conn.execute(
        text(
            "INSERT INTO audit_log (actor_id, action, object_kind, object_id, detail)"
            " VALUES (:a, :act, :k, :o, CAST(:d AS jsonb))"
        ),
        {"a": actor_id, "act": action, "k": AUDIT_ACTIONS[action].kind, "o": object_id, "d": encoded},
    )
