"""The compile steps as data and the step decision (SRV-PIPELINE FR-PIPELINE-5, -23, VEC-PIPELINE-1, -2).

After `draft` the checks run cheapest first (rules, lab verification, fact-check, adversarial review). The
handler of each check records its decision on its report as `next` (the next check, `revise` or `findings`)
with a `stop` when it is `findings`; `next_stage` only reads what was recorded, so `ensure` never re-decides a
loop. A budget refusal wins over any recorded `next`: the next step is then `findings` (none before any
notebook exists).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from ruamel.yaml import YAML

STAGES_FILE: Final = Path(__file__).resolve().parent / "stages.yaml"
NOTEBOOK_MODEL_STAGES: Final = frozenset({"draft", "revise"})
FACT_SOURCES: Final = frozenset({"section"})  # `route` joins with SRV-SEARCH's route (FR-PIPELINE-26)


class StagesInvalid(ValueError):
    """The stages file does not describe a runnable pipeline."""


@dataclass(frozen=True)
class StageDef:
    name: str
    task: str | None
    max_output_tokens: int
    floor_reserve_usd: Decimal
    output: str


@dataclass(frozen=True)
class Pipeline:
    prompt_version: str
    max_loops: int
    stages: Mapping[str, StageDef]
    first: tuple[str, ...]
    checks: tuple[str, ...]
    loop: tuple[str, ...]
    final: tuple[str, ...]
    fact_source: str = "section"
    rules: tuple[str, ...] = ()
    reference_libraries: tuple[str, ...] = ()

    def model_stage(self, name: str) -> bool:
        return self.stages[name].task is not None


@dataclass(frozen=True)
class Done:
    """A completed step: its stage, whether its check failed, and the decision recorded on its report."""

    stage: str
    problems: bool = False
    next: str | None = None
    stop: str | None = None


def load(path: Path = STAGES_FILE, *, rule_names: frozenset[str] | None = None) -> Pipeline:
    data: Any = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    stages = {
        str(s["name"]): StageDef(
            str(s["name"]),
            None if s["task"] == "none" else str(s["task"]),
            int(s["max_output_tokens"]),
            Decimal(str(s["floor_reserve_usd"])),
            str(s["output"]),
        )
        for s in data["stages"]
    }
    order = data["order"]
    pipeline = Pipeline(
        str(data["prompt_version"]),
        int(data["max_loops"]),
        stages,
        tuple(order["first"]),
        tuple(order["checks"]),
        tuple(order["loop"]),
        tuple(order["final"]),
        str((data.get("fact_check") or {}).get("source")),
        tuple(str(r) for r in data.get("rules", [])),
        tuple(str(m) for m in data.get("reference_libraries", [])),
    )
    if pipeline.fact_source not in FACT_SOURCES:
        raise StagesInvalid(f"fact_check.source {pipeline.fact_source!r}")
    named = (*pipeline.first, *pipeline.checks, *pipeline.loop, *pipeline.final)
    if any(name not in stages for name in named):
        raise StagesInvalid("a step of `order` is not a declared stage")
    if rule_names is None:
        from reader.pipeline.rules import RULES

        rule_names = frozenset(RULES)
    if any(rule not in rule_names for rule in pipeline.rules):
        raise StagesInvalid("a rule without a function")
    return pipeline


def with_floors(pipeline: Pipeline, floors: Mapping[str, Decimal]) -> Pipeline:
    stages = {
        name: dataclasses.replace(stage, floor_reserve_usd=floors.get(name, stage.floor_reserve_usd))
        for name, stage in pipeline.stages.items()
    }
    return dataclasses.replace(pipeline, stages=stages)


def decide(
    pipeline: Pipeline, stage: str, failed: bool, loops: int, fits: Callable[[], str | None]
) -> tuple[str, str | None, str | None]:
    """A check's decision: (next, stop, refusing limit). `fits` is asked only when a loop is due and allowed;
    it returns None (the loop fits) or the limit that refuses it (`run`, `task`, `month`)."""
    if not failed:
        position = pipeline.checks.index(stage)
        if position + 1 < len(pipeline.checks):
            return pipeline.checks[position + 1], None, None
        return pipeline.final[0], "passed", None
    if loops >= pipeline.max_loops:
        return pipeline.final[0], "loop_limit", None  # the fourth failure stops the lesson
    limit = fits()
    if limit is not None:
        return pipeline.final[0], "loop_cap", limit
    return pipeline.loop[0], None, None


def next_stage(pipeline: Pipeline, done: Sequence[Done], *, refused: bool) -> str | None:
    """The stage of the next step, or None when the run is finished (VEC-PIPELINE-2).

    Only recorded decisions are read; a refusal wins over a recorded `next` (FR-PIPELINE-5)."""
    names = [d.stage for d in done]
    final = pipeline.final[0]
    if final in names:
        return None
    if refused:
        return final if any(n in NOTEBOOK_MODEL_STAGES for n in names) else None
    if not done:
        return pipeline.first[0]
    last = done[-1]
    if last.stage in NOTEBOOK_MODEL_STAGES:
        return pipeline.checks[0]
    if last.stage in pipeline.checks:
        return last.next
    return None
