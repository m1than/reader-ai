"""SRV-PLAN FR-PLAN-21: where a re-plan puts lessons in a course whose order the owner set by hand. Pure."""

from __future__ import annotations

from collections.abc import Collection, Hashable, Sequence


def placement[T: Hashable](
    current: Sequence[T], plan: Sequence[T], kept: Collection[T], deleted: Collection[T]
) -> list[T]:
    """The final lesson order after a re-plan of a manually ordered course.

    `current`: the course's lessons in their current order; `plan`: the new plan's lessons in plan order;
    `kept`: lessons present before and after; `deleted`: soft-deleted lessons. Kept visible lessons keep
    their current relative order; each new lesson goes directly after its nearest visible predecessor in plan
    order (the closest earlier lesson that is kept or new and not deleted), or first when there is none;
    deleted lessons follow the visible ones in their current order; lessons not in the plan are gone.
    """
    out = [x for x in current if x in kept and x not in deleted]
    for i, lesson in enumerate(plan):
        if lesson in kept:
            continue
        before = [x for x in plan[:i] if x not in deleted]
        out.insert(out.index(before[-1]) + 1 if before else 0, lesson)
    return out + [x for x in current if x in kept and x in deleted]
