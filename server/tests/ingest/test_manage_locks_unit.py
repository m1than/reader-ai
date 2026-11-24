"""The material request's lock walk (SRV-INGEST FR-INGEST-31, SRV-LESSON INV-LESSON-23's order). [unit]

@trace FR-INGEST-31

`affected` is replaced by a scripted sequence of sets and the lock calls are recorded, so the walk's order is
checked without a database; the [db] oracle over real `pg_locks` is
tests/lessons/test_manage_lock_order_db.py.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

import pytest

from .conftest import need

pytestmark = pytest.mark.anyio
OWNER = uuid.UUID("00000000-0000-4000-8000-000000000001")
L1, L2, L3 = (uuid.UUID(f"00000000-0000-4000-8000-00000000001{n}") for n in (1, 2, 3))


def _walk(monkeypatch: pytest.MonkeyPatch, sets: Sequence[Any]) -> tuple[Any, list[tuple[str, str]]]:
    manage = need("reader.ingest.manage")
    taken: list[tuple[str, str]] = []
    script = list(sets)

    async def scripted(*_: Any) -> Any:
        return script.pop(0) if len(script) > 1 else script[0]

    async def take(_conn: Any, owner: uuid.UUID, kind: str, key: str) -> None:
        assert owner == OWNER
        taken.append((kind, key))

    async def advisory(_conn: Any, namespace: int, key: str) -> None:
        taken.append(("position", f"{namespace:#x}:{key}"))

    monkeypatch.setattr(manage, "affected", scripted)
    monkeypatch.setattr(manage, "_take", take)
    monkeypatch.setattr(manage, "_advisory", advisory)
    return manage, taken


def _set(manage: Any, plans: set[str], lessons: set[uuid.UUID]) -> Any:
    return manage.Affected(frozenset(plans), frozenset(lessons))


def test_lock_sequence_is_plans_then_lessons_each_ascending() -> None:
    manage = need("reader.ingest.manage")
    found = _set(manage, {"b-course", "a-course"}, {L3, L1, L2})
    assert manage.lock_sequence(found) == [
        ("plan", "a-course"), ("plan", "b-course"),
        ("lesson", str(L1)), ("lesson", str(L2)), ("lesson", str(L3)),
    ]


async def test_a_stable_set_is_walked_once_then_the_position_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    found = _set(need("reader.ingest.manage"), {"a-course"}, {L1, L3})
    manage, taken = _walk(monkeypatch, [found, found])
    await manage.lock_affected(object(), OWNER, "s" * 64, "synthetic")
    assert taken == [*manage.lock_sequence(found), ("position", f"0x5243504f:{OWNER}")]


async def test_a_set_that_grew_is_walked_again_whole_in_global_order(monkeypatch: pytest.MonkeyPatch) -> None:
    manage = need("reader.ingest.manage")
    first = _set(manage, {"b-course"}, {L1, L3})
    grown = _set(manage, {"a-course", "b-course"}, {L1, L2, L3})  # a lower plan course and a middle lesson
    manage, taken = _walk(monkeypatch, [first, grown, grown])
    await manage.lock_affected(object(), OWNER, "s" * 64, "synthetic")
    first_walk, second_walk = manage.lock_sequence(first), manage.lock_sequence(grown)
    assert taken == [*first_walk, *second_walk, ("position", f"0x5243504f:{OWNER}")]
    assert second_walk == sorted(second_walk, key=lambda x: (x[0] != "plan", x[1]))  # the whole set, in order


async def test_a_set_that_keeps_changing_is_refused_before_the_position_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manage = need("reader.ingest.manage")
    sets = [_set(manage, {"a-course"}, set(lessons)) for lessons in ([L1], [L1, L2], [L1, L2, L3])]
    sets.append(_set(manage, {"a-course", "b-course"}, {L1, L2, L3}))
    manage, taken = _walk(monkeypatch, sets)
    with pytest.raises(manage.Refused) as caught:
        await manage.lock_affected(object(), OWNER, "s" * 64, "synthetic")
    assert (caught.value.status, caught.value.body) == (409, {"error": "material_busy"})
    assert all(kind != "position" for kind, _ in taken)
