"""YAML 1.2 loading into a positioned value tree (FR-CONFIG-15).

ruamel.yaml is used only as a parser to events with marks. An event pass rejects anchors, aliases, merge
keys, tags and extra documents; construction then resolves plain scalars by the YAML 1.2.2 core schema and
rejects duplicate and non-string keys. Every failure is a `LoadFailure(code, line)`; no parser text is kept.
"""

import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from ruamel.yaml.events import (
    AliasEvent,
    CollectionEndEvent,
    DocumentStartEvent,
    Event,
    MappingStartEvent,
    NodeEvent,
    ScalarEvent,
    SequenceStartEvent,
)

Kind = Literal["null", "bool", "int", "float", "str"]


@dataclass(frozen=True, slots=True)
class Scalar:
    line: int
    kind: Kind
    value: object
    text: str


@dataclass(frozen=True, slots=True)
class Entry:
    key: str
    key_line: int
    value: Node


@dataclass(frozen=True, slots=True)
class Map:
    line: int
    entries: tuple[Entry, ...]


@dataclass(frozen=True, slots=True)
class Seq:
    line: int
    items: tuple[Node, ...]


Node = Scalar | Map | Seq


class LoadFailure(Exception):
    def __init__(self, code: str, line: int | None) -> None:
        self.code = code
        self.line = line
        super().__init__(code)


# YAML 1.2.2 §10.3.2, core schema: (pattern, conversion)
_INT: Final[tuple[tuple[re.Pattern[str], Callable[[str], int]], ...]] = (
    (re.compile(r"[-+]?[0-9]+"), lambda t: int(t, 10)),
    (re.compile(r"0o[0-7]+"), lambda t: int(t[2:], 8)),
    (re.compile(r"0x[0-9a-fA-F]+"), lambda t: int(t[2:], 16)),
)
_FLOAT: Final[tuple[tuple[re.Pattern[str], Callable[[str], float]], ...]] = (
    (re.compile(r"[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?"), float),
    (re.compile(r"[-+]?(\.inf|\.Inf|\.INF)"), lambda t: float(t.replace(".", "").lower())),
    (re.compile(r"\.nan|\.NaN|\.NAN"), lambda t: float("nan")),
)
_NULL: Final = frozenset({"", "null", "Null", "NULL", "~"})
_BOOL: Final = {"true": True, "True": True, "TRUE": True, "false": False, "False": False, "FALSE": False}


def _line(event: Event) -> int:
    return int(event.start_mark.line) + 1


class _Oversized:
    """An int whose absolute value is at least 10^4300, in any base; validation reports `out_of_range`."""

    def __repr__(self) -> str:
        return "<oversized int>"


OVERSIZED: Final = _Oversized()
MAX_DEPTH: Final = 64  # structural bound on collection nesting (FR-CONFIG-15), not a measured limit
INT_BOUND: Final = 10**4300  # decided by value for every base; matches Python's decimal conversion limit


def _int(convert: Callable[[str], int], text: str) -> int | _Oversized:
    try:
        value = convert(text)
    except ValueError:  # decimal text beyond Python's 4,300-digit limit
        return OVERSIZED
    return value if abs(value) < INT_BOUND else OVERSIZED


def _encodable(text: str) -> bool:
    """False for text that UTF-8 cannot encode (lone surrogates from `\\ud800`-style escapes)."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def resolve_plain(text: str) -> tuple[Kind, object]:
    if text in _NULL:
        return "null", None
    if text in _BOOL:
        return "bool", _BOOL[text]
    for pattern, convert in _INT:
        if pattern.fullmatch(text):
            return "int", _int(convert, text)
    for pattern, convert_float in _FLOAT:
        if pattern.fullmatch(text):
            return "float", convert_float(text)
    return "str", text


def _parse(text: str) -> list[Event]:
    outcome: list[Event] | LoadFailure
    try:
        outcome = list(YAML(typ="rt", pure=True).parse(text))
    except YAMLError as error:
        mark = getattr(error, "problem_mark", None) or getattr(error, "context_mark", None)
        outcome = LoadFailure("yaml_syntax", int(mark.line) + 1 if mark is not None else None)
    if isinstance(outcome, LoadFailure):
        raise outcome from None
    return outcome


def _check_events(events: Sequence[Event]) -> None:
    """First violation in stream order: anchors, aliases, merge keys, tags, a second document, depth."""
    documents = 0
    expect_key: list[bool | None] = []  # per open collection: mapping → expecting a key next; sequence → None
    for event in events:
        if isinstance(event, DocumentStartEvent):
            documents += 1
            if documents > 1:
                raise LoadFailure("multiple_documents", _line(event))
        elif isinstance(event, AliasEvent):
            raise LoadFailure("alias_not_allowed", _line(event))
        elif isinstance(event, NodeEvent):
            if event.anchor is not None:
                raise LoadFailure("alias_not_allowed", _line(event))
            if getattr(event, "tag", None) is not None:
                raise LoadFailure("tag_not_allowed", _line(event))
            is_key = bool(expect_key) and expect_key[-1] is True
            if expect_key and expect_key[-1] is not None:
                expect_key[-1] = not expect_key[-1]
            if is_key and isinstance(event, ScalarEvent) and event.style is None and event.value == "<<":
                raise LoadFailure("alias_not_allowed", _line(event))
            if isinstance(event, MappingStartEvent | SequenceStartEvent):
                if len(expect_key) >= MAX_DEPTH:
                    raise LoadFailure("too_deep", _line(event))
                expect_key.append(True if isinstance(event, MappingStartEvent) else None)
        elif isinstance(event, CollectionEndEvent):
            expect_key.pop()


def _build(events: Iterator[Event], first: Event) -> Node:
    """Construct one node. The parser guarantees balanced events, and the event pass bounds the depth."""
    if isinstance(first, ScalarEvent):
        text = str(first.value)
        if not _encodable(text):  # scalars and keys alike: keys are built through this branch
            raise LoadFailure("not_utf8_encodable", _line(first))
        kind, value = resolve_plain(text) if first.style is None else ("str", text)
        return Scalar(_line(first), kind, value, text)
    if isinstance(first, SequenceStartEvent):
        items: list[Node] = []
        while not isinstance(event := next(events), CollectionEndEvent):
            items.append(_build(events, event))
        return Seq(_line(first), tuple(items))
    entries: list[Entry] = []
    seen: set[str] = set()
    while not isinstance(event := next(events), CollectionEndEvent):
        key = _build(events, event)
        if not (isinstance(key, Scalar) and key.kind == "str"):
            raise LoadFailure("non_string_key", key.line)
        key_text = str(key.value)
        if key_text in seen:
            raise LoadFailure("duplicate_key", key.line)
        seen.add(key_text)
        entries.append(Entry(key_text, key.line, _build(events, next(events))))
    return Map(_line(first), tuple(entries))


def load_tree(text: str) -> Map:
    events = _parse(text)
    _check_events(events)
    nodes = iter(e for e in events if isinstance(e, NodeEvent | CollectionEndEvent))
    first = next(nodes, None)
    if first is None:
        raise LoadFailure("empty_document", None)
    if isinstance(first, ScalarEvent):  # decided at the root, before anything inside it (precedence)
        scalar = _build(nodes, first)
        if isinstance(scalar, Scalar) and scalar.kind == "null":
            raise LoadFailure("empty_document", None)
        raise LoadFailure("not_mapping", scalar.line)
    if not isinstance(first, MappingStartEvent):
        raise LoadFailure("not_mapping", _line(first))
    root = _build(nodes, first)
    assert isinstance(root, Map)  # noqa: S101 - a MappingStartEvent always builds a Map
    return root
