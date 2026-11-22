"""SRV-TUTOR [unit]: the bench question generator and the bench statistics (VEC-TUTOR-7, NFR-TUTOR-1 rank rule).
The generator lives with the live bench in e2e/web/tutor/; it is loaded here by path.

@trace VEC-TUTOR-7
"""

from __future__ import annotations

import importlib.util
import json
import math
from types import ModuleType
from typing import Any

import pytest

from .conftest import F01, REPO, fixture

BENCH = fixture("bench.yaml")


def _generator() -> ModuleType:
    path = REPO / "e2e" / "web" / "tutor" / "bench_questions.py"
    if not path.exists():
        pytest.fail("e2e/web/tutor/bench_questions.py is not implemented yet (SRV-TUTOR Red)")
    spec = importlib.util.spec_from_file_location("bench_questions", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _notebook(cells: list[tuple[str, str, str]]) -> dict[str, Any]:
    return {
        "cells": [
            {"id": cid, "cell_type": "markdown", "source": src, "metadata": {"reader": {"kind": kind}}}
            for cid, kind, src in cells
        ]
    }


def test_generator_follows_the_fixture() -> None:
    g = _generator()
    gen = BENCH["generator"]
    assert gen["take"] == g.TAKE == 20
    assert gen["template"] == g.TEMPLATE
    assert gen["cells"] == g.CELL_KIND == "explain"
    assert BENCH["fixed"] == [{"source": "spikes/embeddings-local/questions-v2.yaml", "id": "v03"}]


def test_questions_from_the_compiled_revision() -> None:
    g = _generator()
    assert g.questions(json.loads(F01)) == [("f01-explain", "Explain in your own words: Catching up")]
    many = _notebook(
        [(f"e{i}", "explain", f"## Title {i}\n\nBody.") for i in range(25)] + [("lab", "lab", "## Not this")]
    )
    out = g.questions(many)
    assert [cid for cid, _ in out] == [f"e{i}" for i in range(20)]
    assert out[3][1] == "Explain in your own words: Title 3"


@pytest.mark.parametrize(
    "cells",
    [
        [],
        [("r", "route", "## Route")],
        [("e1", "explain", "## Fine"), ("e2", "explain", "No heading here.")],
    ],
    ids=["empty", "no-explain-cell", "untitled-explain-cell"],
)
def test_generator_stops_before_any_question(cells: list[tuple[str, str, str]]) -> None:
    g = _generator()
    with pytest.raises(g.BenchError):
        g.questions(_notebook(cells))


@pytest.mark.parametrize(
    ("samples", "rank"),
    [([1.0], 1), (list(range(1, 21)), 19), (list(range(1, 22)), 20), (list(range(1, 3)), 2)],
)
def test_p95_is_nearest_rank(samples: list[float], rank: int) -> None:
    g = _generator()
    assert rank == math.ceil(0.95 * len(samples))
    assert g.p95(list(reversed(samples))) == sorted(samples)[rank - 1]
