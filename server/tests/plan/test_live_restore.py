# @trace FR-PLAN-9
"""The live check (e2e/live/lib/plan_live.py) restores the owner's own stored plan after its mutating steps, also
when a step fails; without a stored plan it leaves its confirmed plan. A fake HTTP client stands in for the stack."""

from __future__ import annotations

from types import ModuleType
from typing import Any

import pytest

from .test_live_session_db import helper

SHA = "61e8d4c390ba" + "0" * 52


class Resp:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self._body = body
        self.text = str(body)

        class _E:
            @staticmethod
            def total_seconds() -> float:
                return 0.01

        self.elapsed = _E()

    def json(self) -> Any:
        return self._body


def preview(body: dict[str, Any]) -> dict[str, Any]:
    w = body["total_weeks"]
    return {
        "plan_sha256": f"{w:064d}",
        "inputs": {**body, "start_date": "2026-01-01"},
        "lessons": [{"id": "p1-2", "pages": [1, 2], "minutes": 5, "fixed": False, "title": "t"}],
        "summary": {"lesson_count": 1, "fixed_count": 0, "new_count": 1, "study_days": w * 7, "days_left": w * 7,
                    "minutes_mean": 5, "minutes_max": 5, "required_minutes": 5, "fits": w != 1,
                    "reasons": [] if w != 1 else ["days"], "suggest": None},
        "estimate": {"course_low_usd": "0.10", "course_high_usd": "0.20", "mean_lesson_kchars": "1.000",
                     "extrapolated": True, "first_lesson": None},
    }


class FakeClient:
    def __init__(self, stored: dict[str, Any] | None, fail_replan: bool = False) -> None:
        self.stored = stored
        self.confirms: list[dict[str, Any]] = []
        self.fail_replan = fail_replan

    def get(self, path: str, params: Any = None) -> Resp:
        if path == "/api/materials":
            return Resp(200, {"materials": [{"id": "pilot", "sha256": SHA, "status": "imported"}]})
        if self.stored is None:
            return Resp(404, {"error": "not_found"})
        return Resp(200, {"inputs": self.stored, "plan_sha256": "x"})

    def post(self, path: str, json: dict[str, Any]) -> Resp:
        if path == "/api/plans/preview":
            return Resp(200, preview(json))
        if self.fail_replan and json["total_weeks"] == 16:
            raise RuntimeError("stack went away")
        self.confirms.append(json)
        self.stored = {k: json[k] for k in ("total_weeks", "minutes_per_day")} | {
            "rate": json.get("rate", "2.64"), "start_date": "2026-01-01"}
        return Resp(200, {"course_id": "c", "plan_course_id": "plan-x", "created": 1, "removed": 0,
                          "replayed": False, "compile": None,
                          "lessons": [{"plan_lesson_id": "p1-2", "lesson_id": "l", "ordinal": 1}]})


@pytest.fixture
def live() -> ModuleType:
    return helper()


def test_restores_the_owners_plan(live: ModuleType, tmp_path: Any) -> None:
    owner = {"total_weeks": 20, "minutes_per_day": 45, "rate": "3.10", "start_date": "2026-01-01"}
    c = FakeClient(dict(owner))
    live.check_api(c, tmp_path)
    last = c.confirms[-1]
    assert (last["total_weeks"], last["minutes_per_day"], last["rate"]) == (20, 45, "3.10")
    assert all(x["compile_first"] is False for x in c.confirms)


def test_restores_even_when_a_step_fails(live: ModuleType, tmp_path: Any) -> None:
    owner = {"total_weeks": 20, "minutes_per_day": 45, "rate": "3.10", "start_date": "2026-01-01"}
    c = FakeClient(dict(owner), fail_replan=True)
    with pytest.raises(RuntimeError):
        live.check_api(c, tmp_path)
    assert (c.confirms[-1]["total_weeks"], c.confirms[-1]["minutes_per_day"]) == (20, 45)


def test_skips_the_mutating_steps_when_the_owners_plan_would_not_fit(live: ModuleType, tmp_path: Any) -> None:
    owner = {"total_weeks": 1, "minutes_per_day": 45, "rate": "3.10", "start_date": "2026-01-01"}
    c = FakeClient(dict(owner))
    results = live.check_api(c, tmp_path)
    assert c.confirms == []
    assert results["mutating_steps"].startswith("skipped")


def test_without_a_stored_plan_it_leaves_its_own(live: ModuleType, tmp_path: Any) -> None:
    c = FakeClient(None)
    live.check_api(c, tmp_path)
    assert c.confirms[-1]["total_weeks"] == 12
