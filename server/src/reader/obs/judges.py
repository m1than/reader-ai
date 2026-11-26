"""SRV-OBS FR-OBS-43: the `judge.eval.*` and `judge.align` parsers and the user-message renderer.

The system prompt is `reader.obs.prompts.wrap(...)` only; every data value (queries, cards, answers, notes,
expected facts, judge outputs) travels in the user message, with each `READER-INSTRUCTIONS` replaced by
`READER_INSTRUCTIONS_DATA` and cards labelled by ordinals, never ids. A parser returns the parsed value only;
the model's text never leaves it."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from reader.obs import phase2, prompts

REPLACEMENT: Final[dict[str, str]] = dict(phase2.VEC["align"]["data_delimiter_replacement"])
MAX_INSTRUCTIONS: Final = 8000
FACT_SLOTS: Final = frozenset({"judge.eval.sufficiency", "judge.eval.groundedness"})
SCHEMA_VERSION: Final = 1
_FENCE: Final = re.compile(r"\A```json\n(.*)\n```\Z", re.DOTALL)


class OutputInvalid(Exception):
    """A refused parse; carries the code only."""

    def __init__(self) -> None:
        super().__init__("output_invalid")
        self.code = "output_invalid"


def judge_version(prompt: bytes, model: str, schema_version: int = SCHEMA_VERSION) -> str:
    """SRV-AGENT's `judge_version` formula (FR-AGENT-18), so a prompt edit is a new judge version."""
    digest = hashlib.sha256()
    for part in (prompt, model.encode("utf-8"), str(schema_version).encode("ascii")):
        digest.update(hashlib.sha256(part).digest())
    return digest.hexdigest()[:12]


def judge_name(slot: str, model: str) -> str:
    return f"{slot}@{judge_version(prompts.prompt_bytes(slot), model)}"


def _no_constant(_name: str) -> Any:
    raise ValueError("non-finite number")


def _object(raw: str, key: str) -> Any:
    body = raw.strip()
    fenced = _FENCE.match(body)
    if fenced is not None:
        body = fenced.group(1)
    try:
        data = json.loads(body, parse_constant=_no_constant)
    except ValueError:
        raise OutputInvalid from None
    if not isinstance(data, dict) or set(data) != {key}:
        raise OutputInvalid
    return data[key]


def parse_eval(raw: str) -> float:
    """Exactly {"score": a finite JSON number in [0, 1]}."""
    value = _object(raw, "score")
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise OutputInvalid
    return float(value)


def parse_align(raw: str) -> str:
    """Exactly {"instructions": a JSON string of 1..8,000 characters}."""
    value = _object(raw, "instructions")
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_INSTRUCTIONS:
        raise OutputInvalid
    return value


def neutralise(value: str) -> str:
    for raw, safe in REPLACEMENT.items():
        value = value.replace(raw, safe)
    return value


def render_data(fields: Sequence[tuple[str, str | Sequence[str]]]) -> str:
    """The user message: each (fixed name, data value) as a block; a list value as items [1], [2], ...; every
    data value neutralised."""
    blocks = []
    for name, value in fields:
        if isinstance(value, str):
            blocks.append(f"{name}:\n{neutralise(value)}")
        else:
            items = "\n\n".join(f"[{n}] {neutralise(item)}" for n, item in enumerate(value, start=1))
            blocks.append(f"{name}:\n{items}")
    return "\n\n".join(blocks) + "\n"


def eval_messages(
    slot: str, instructions: str, query: str, cards: Sequence[tuple[str, str]], facts: Sequence[str]
) -> tuple[str, str]:
    """(system, user) for one `judge.eval.*` call: the slot's frame around `instructions`, and the data with
    the cards by ordinal (their ids are dropped); expected facts for sufficiency and groundedness only."""
    fields: list[tuple[str, str | Sequence[str]]] = [("Question", query), ("Cards", [t for _, t in cards])]
    if slot in FACT_SLOTS:
        fields.append(("Expected facts", list(facts)))
    return prompts.wrap(slot, instructions), render_data(fields)


@dataclass(frozen=True)
class AlignError:
    """One training error shown to `judge.align`: the labelled input, the recorded output and the note."""

    query: str
    cards: Sequence[str]
    facts: Sequence[str]
    recorded: str
    note: str | None


def align_messages(current: str, errors: Sequence[AlignError]) -> tuple[str, str]:
    """(system, user) for one `judge.align` call: the file's bytes, and the current instructions and the
    errors as data."""
    cases = []
    for error in errors:
        fields: list[tuple[str, str | Sequence[str]]] = [
            ("Question", error.query), ("Cards", list(error.cards))]
        if error.facts:
            fields.append(("Expected facts", list(error.facts)))
        fields.append(("Recorded output", error.recorded))
        fields.append(("Owner verdict", "wrong"))
        if error.note:
            fields.append(("Owner note", error.note))
        cases.append(render_data(fields))
    user = render_data([("Current instructions", current), ("Cases", cases)])
    return prompts.system_prompt("judge.align"), user
