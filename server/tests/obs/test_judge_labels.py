"""SRV-OBS FR-OBS-40, FR-OBS-24 [unit]: judge item ids, the labelling queue's order (B7 item 3's classes, then
round-robin over (route, first pack) groups, newest first), the label body for a judge target, and the HUMAN
names `owner.<slot>_ok` with their load-time injectivity check.

@trace FR-OBS-40 FR-OBS-24
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from .conftest import require

U = "10000000-0000-4000-8000-0000000000ab"


@pytest.mark.parametrize(("raw", "parsed"), [
    (f"ji:{U}", ("ji", uuid.UUID(U))),
    (f"es:{U}", ("es", uuid.UUID(U))),
    (U, None),
    (f"xx:{U}", None),
    (f"ji:{U.upper()}", None),
    (f"ji:{U}\n", None),
])
def test_item_ids(raw: str, parsed: Any) -> None:
    assert require("reader.obs.judge_labels").parse_item(raw) == parsed


def _item(n: int, klass: int, route: str, pack: str, age_min: int) -> Any:
    jl = require("reader.obs.judge_labels")
    at = datetime(2026, 11, 26, 12, tzinfo=UTC) - timedelta(minutes=age_min)
    return jl.Item(id=f"ji:{n}", slot="judge.find", created_at=at, route=route, channel="tutor",
                   group=(route, pack), klass=klass)


def test_queue_order_classes_then_round_robin_newest_first() -> None:
    jl = require("reader.obs.judge_labels")
    items = [
        _item(1, 4, "exact", "p1", 1), _item(2, 4, "exact", "p1", 2), _item(3, 4, "thematic", "p1", 3),
        _item(4, 2, "exact", "p2", 50), _item(5, 1, "thematic", "p1", 90), _item(6, 3, "exact", "p1", 9),
    ]
    assert [i.id for i in jl.order(items, 25)] == ["ji:5", "ji:4", "ji:6", "ji:1", "ji:3", "ji:2"]
    assert [i.id for i in jl.order(items, 2)] == ["ji:5", "ji:4"]


@pytest.mark.parametrize(("body", "ok"), [
    ({"target": "judge", "id": f"ji:{U}", "ok": False, "note": "kept an off-topic card"}, True),
    ({"target": "judge", "id": f"es:{U}", "ok": True}, True),
    ({"target": "judge", "id": U, "ok": True}, False),  # a judge target takes an item id
    ({"target": "judge", "id": f"ji:{U}", "ok": True, "reason": "other"}, False),  # no reason enum for judges
    ({"target": "judge", "id": f"ji:{U}", "ok": True, "note": "x" * 201}, False),
    ({"target": "judge", "id": f"ji:{U}", "ok": True, "note": "two\nlines"}, False),
    ({"target": "answer", "id": U, "ok": True, "note": "x"}, False),  # a note is for judge targets only
    ({"target": "answer", "id": f"ji:{U}", "ok": True}, False),
    ({"target": "lesson", "id": U, "ok": True, "reason": "unclear"}, True),
])
def test_label_body(body: dict[str, Any], ok: bool) -> None:
    from pydantic import ValidationError

    labels = require("reader.obs.labels")
    if ok:
        labels.LabelBody.model_validate(body)
    else:
        with pytest.raises(ValidationError):
            labels.LabelBody.model_validate(body)


def test_human_names_per_slot() -> None:
    labels = require("reader.obs.labels")
    assert labels.assessment_name("judge.find") == "owner.judge_find_ok"
    assert labels.assessment_name("judge.eval.sufficiency") == "owner.judge_eval_sufficiency_ok"
    import re

    for slot in labels.JUDGE_SLOTS:
        assert re.fullmatch(r"owner\.judge_[a-z_]{1,48}_ok", labels.assessment_name(slot))
    assert "judge.link_type" not in labels.JUDGE_SLOTS and "judge.align" not in labels.JUDGE_SLOTS
    labels.check_slots(labels.JUDGE_SLOTS)
    with pytest.raises(ValueError):
        labels.check_slots(("judge.eval.a_b", "judge.eval_a.b"))  # both map to owner.judge_eval_a_b_ok
