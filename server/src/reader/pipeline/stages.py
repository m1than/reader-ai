"""The compile steps as data and the step decision (SRV-PIPELINE FR-PIPELINE-5, VEC-PIPELINE-1)."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from ruamel.yaml import YAML

STAGES_FILE: Final = Path(__file__).resolve().parent / "stages.yaml"
NOTEBOOK_MODEL_STAGES: Final = frozenset({"draft", "revise"})


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
    max_revise_loops: int
    stages: Mapping[str, StageDef]
    first_pass: tuple[str, ...]
    loop: tuple[str, ...]
    final: tuple[str, ...]

    def model_stage(self, name: str) -> bool:
        return self.stages[name].task is not None


@dataclass(frozen=True)
class Done:
    """A completed step: its stage, and whether its report has problems (fact_check, adversarial_review)."""

    stage: str
    problems: bool


def load(path: Path = STAGES_FILE) -> Pipeline:
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
    return Pipeline(
        str(data["prompt_version"]),
        int(data["max_revise_loops"]),
        stages,
        tuple(order["first_pass"]),
        tuple(order["loop"]),
        tuple(order["final"]),
    )


def with_floors(pipeline: Pipeline, floors: Mapping[str, Decimal]) -> Pipeline:
    stages = {
        name: dataclasses.replace(stage, floor_reserve_usd=floors.get(name, stage.floor_reserve_usd))
        for name, stage in pipeline.stages.items()
    }
    return dataclasses.replace(pipeline, stages=stages)


def _dirty(done: Sequence[Done]) -> bool:
    """The last fact report has an unsupported or contradicted verdict, or the last review a blocker."""
    fact = next((d for d in reversed(done) if d.stage == "fact_check"), None)
    review = next((d for d in reversed(done) if d.stage == "adversarial_review"), None)
    return bool((fact and fact.problems) or (review and review.problems))


def next_stage(pipeline: Pipeline, done: Sequence[Done], *, refused: bool) -> str | None:
    """The stage of the next step, or None when the run is finished (VEC-PIPELINE-2)."""
    names = [d.stage for d in done]
    if refused:  # a budget refusal skips the remaining model steps (FR-PIPELINE-5)
        if not any(n in NOTEBOOK_MODEL_STAGES for n in names):
            return None
        return next((s for s in pipeline.final if s not in names), None)
    seq = list(pipeline.first_pass)
    loops = 0
    while loops < pipeline.max_revise_loops and len(done) >= len(seq) and _dirty(done[: len(seq)]):
        seq += pipeline.loop
        loops += 1
    seq += pipeline.final
    return seq[len(names)] if len(names) < len(seq) else None
