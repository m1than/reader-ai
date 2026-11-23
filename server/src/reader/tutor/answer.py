"""The prompt, the answer check and the reserve floor (SRV-TUTOR FR-TUTOR-6, -7, -8; INV-TUTOR-2, -3).

Pure functions over data. The model sees source aliases s1…sk, never a span id; the alias → span map stays
here. A block is supported only when its quote, after collapsing whitespace and NFC, occurs in the first
source it names. Nothing here logs.
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from reader.agent.pricing import micro, rates
from reader.tutor import params
from reader.tutor.errors import TutorError, TutorTransient

SYSTEM_PROMPT: Final = Path(__file__).resolve().parent / "prompts" / "answer-v1.md"
# The prompt without sources gets what the bounds give question, cell and JSON overhead: 3000 + 4000 + 1000.
_QUESTION_CELL_BYTES: Final = params.QUESTION_CELL_BYTES_MAX
_FENCE: Final = re.compile(r"\A```(?:json)?[ \t]*\n(.*)\n```\Z", re.DOTALL)
_BLOCK_KEYS: Final = frozenset({"text", "spans", "quote"})
_TOP_KEYS: Final = frozenset({"blocks", "insufficient"})


@dataclass(frozen=True)
class Span:
    """A retrieved span, in rank order: SRV-SEARCH's span id, its page and its text."""

    span_id: str
    page: int
    text: str


@dataclass(frozen=True)
class Source:
    """A span as sent: its alias in the prompt and its text after the byte cut."""

    alias: str
    span_id: str
    page: int
    text: str


@dataclass(frozen=True)
class Cell:
    id: str
    kind: str
    text: str


@dataclass(frozen=True)
class Prompt:
    text: str
    sources: tuple[Source, ...]


@dataclass(frozen=True)
class Block:
    text: str
    kept: tuple[str, ...]  # aliases of the sent set, deduplicated, in the model's order
    quote: str
    supported: bool


@dataclass(frozen=True)
class Checked:
    blocks: tuple[Block, ...]
    insufficient: bool
    grounded: bool


@functools.cache
def system_prompt() -> tuple[str, str]:
    """The versioned system prompt and the sha256 of its bytes (FR-TUTOR-6)."""
    data = SYSTEM_PROMPT.read_bytes()
    return data.decode("utf-8"), hashlib.sha256(data).hexdigest()


def cut_bytes(text: str, limit: int) -> str:
    """`text` cut to at most `limit` UTF-8 bytes: at the last whitespace within the limit, else a hard cut on
    a character boundary."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    head = data[: max(limit, 0)].decode("utf-8", errors="ignore")
    if text[len(head) : len(head) + 1].isspace():
        return head.rstrip()
    space = max((i for i, ch in enumerate(head) if ch.isspace()), default=-1)
    return head[:space].rstrip() if space > 0 else head


def _size(doc: Mapping[str, Any]) -> int:
    return len(json.dumps(doc, ensure_ascii=False).encode("utf-8"))


def _doc(question: str, cell: Cell | None, sources: Sequence[Source]) -> dict[str, Any]:
    return {
        "question": question,
        "cell": None if cell is None else {"id": cell.id, "kind": cell.kind, "text": cell.text},
        "sources": [{"id": s.alias, "page": s.page, "text": s.text} for s in sources],
    }


def _shrink(text: str, excess: int) -> str:
    return cut_bytes(text, max(len(text.encode("utf-8")) - max(excess, 1), 0))


def build_prompt(question: str, cell: Cell | None, spans: Sequence[Span]) -> Prompt:
    """FR-TUTOR-6: the user prompt as one JSON document, sized after escaping, lowest ranks dropped first."""
    sources = [
        Source(f"s{i + 1}", s.span_id, s.page, cut_bytes(s.text, params.SPAN_BYTES_MAX))
        for i, s in enumerate(spans[: params.SOURCES_MAX])
    ]
    if not sources:
        raise TutorError("sources_unavailable")
    question = cut_bytes(question, params.QUESTION_BYTES_MAX)
    if cell is not None:
        cell = Cell(cell.id, cell.kind, cut_bytes(cell.text, params.CELL_BYTES_MAX))
    while (excess := _size(_doc(question, cell, [])) - _QUESTION_CELL_BYTES) > 0:
        if cell is not None and cell.text:
            cell = Cell(cell.id, cell.kind, _shrink(cell.text, excess))
        elif question:
            question = _shrink(question, excess)
        else:  # only the cell id and kind are left; they are short structural strings
            break
    while sources and _size(_doc(question, cell, sources)) > params.PROMPT_BYTES_MAX:
        sources.pop()
    if not sources:
        raise TutorError("sources_unavailable")
    return Prompt(json.dumps(_doc(question, cell, sources), ensure_ascii=False), tuple(sources))


def _invalid() -> TutorTransient:
    return TutorTransient("output_invalid")


def _block(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != _BLOCK_KEYS:
        raise _invalid()
    text, spans, quote = raw["text"], raw["spans"], raw["quote"]
    if not isinstance(text, str) or not 1 <= len(text) <= params.BLOCK_CHARS_MAX:
        raise _invalid()
    if not isinstance(spans, list) or len(spans) > params.SPANS_PER_BLOCK_MAX:
        raise _invalid()
    if not all(isinstance(s, str) for s in spans) or not isinstance(quote, str):
        raise _invalid()
    return {"text": text, "spans": list(spans), "quote": quote}


def parse(text: str) -> dict[str, Any]:
    """FR-TUTOR-8: one JSON object of the answer shape, maybe in one ```json fence; else `output_invalid`."""
    body = text.strip()
    fenced = _FENCE.match(body)
    if fenced:
        body = fenced.group(1)
    try:
        raw = json.loads(body)
    except ValueError, RecursionError:
        raise _invalid() from None
    if not isinstance(raw, dict) or set(raw) != _TOP_KEYS or type(raw["insufficient"]) is not bool:
        raise _invalid()
    blocks = raw["blocks"]
    if not isinstance(blocks, list) or not 1 <= len(blocks) <= params.BLOCKS_MAX:
        raise _invalid()
    return {"blocks": [_block(b) for b in blocks], "insufficient": raw["insufficient"]}


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


def _supported(quote: str, source: Source) -> bool:
    words = normalize(quote).split()
    low, high = params.QUOTE_WORDS
    return low <= len(words) <= high and " ".join(words) in normalize(source.text)


def check(output: Mapping[str, Any], sent: Sequence[Source]) -> Checked:
    """FR-TUTOR-8 by code: aliases outside the sent set removed, duplicates collapsed, quotes verified."""
    by_alias = {s.alias: s for s in sent}
    blocks = []
    for raw in output["blocks"]:
        kept = tuple(dict.fromkeys(a for a in raw["spans"] if a in by_alias))
        supported = bool(kept) and _supported(raw["quote"], by_alias[kept[0]])
        blocks.append(Block(raw["text"], kept, raw["quote"], supported))
    insufficient = bool(output["insufficient"])
    return Checked(tuple(blocks), insufficient, not insufficient and all(b.supported for b in blocks))


def floor_usd(model: str, system_bytes: int, prompt_bytes: int) -> Decimal:
    """The tutor's worst case for one attempt (VEC-TUTOR-3): every byte sent as one token written to the
    1-hour cache, plus the CLI's overhead, plus the output cap at the output rate."""
    table = rates(model)
    tokens = params.CLI_OVERHEAD_TOKENS + system_bytes + prompt_bytes
    total = Decimal(tokens) * table["cache_write_1h"] + Decimal(params.MAX_OUTPUT_TOKENS) * table["output"]
    return micro(total / Decimal(10**6))


def max_reserve_usd(model: str) -> Decimal:
    return floor_usd(model, params.SYSTEM_BYTES_MAX, params.PROMPT_BYTES_MAX)
