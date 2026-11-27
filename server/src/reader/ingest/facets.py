"""The facets file: closed vocabularies, `enabled` with a consumer, and the `form` rule (SRV-INGEST
FR-INGEST-37; ADR-0039 B3, F1-F6).

`load` validates facets.yaml against facets.schema.json (Draft 2020-12) and the rules the schema cannot state;
any failure is `facets_invalid`, never a default.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final

import jsonschema
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

FACETS_FILE: Final = Path(__file__).resolve().parent / "facets.yaml"
SCHEMA_FILE: Final = Path(__file__).resolve().parent / "facets.schema.json"
REQUIREMENT: Final = re.compile(r"(FR|INV|NFR)-[A-Z]+-[0-9]+")
TOPIC_PATH: Final = re.compile(r"[a-z0-9_-]+(/[a-z0-9_-]+){0,2}")  # a sector path, depth cap 3 (B3)


class FacetsError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code} {detail}".strip())
        self.code = code


@dataclass(frozen=True)
class Facet:
    level: str
    values: tuple[str, ...] | None  # None: `type: date` or `tree: sectors`
    kind: str  # closed, date or tree
    by: str
    enabled: bool
    consumer: str


@dataclass(frozen=True)
class Facets:
    facets: dict[str, Facet]
    form_rule: dict[str, str]
    vector_lines: int
    vector_pattern: re.Pattern[str]
    sha256: str


def _views() -> tuple[str, ...]:
    from reader.ingest.manifest import VIEWS

    return tuple(VIEWS)


def load(path: Path | None = None) -> Facets:
    raw = (path or FACETS_FILE).read_bytes()
    try:
        data = YAML(typ="safe").load(raw)
        schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(data)
        pattern = re.compile(data["test_vector_rule"]["pattern"])
    except (YAMLError, jsonschema.ValidationError, re.error) as error:
        raise FacetsError("facets_invalid", type(error).__name__) from None
    facets: dict[str, Facet] = {}
    for name, body in data["facets"].items():
        kind = "date" if "type" in body else "tree" if "tree" in body else "closed"
        values = tuple(body["values"]) if kind == "closed" else None
        facet = Facet(body["level"], values, kind, body["by"], body["enabled"], body["consumer"])
        if facet.enabled and not (facet.consumer in _views() or REQUIREMENT.fullmatch(facet.consumer)):
            raise FacetsError("facets_invalid", f"consumer {name}")
        facets[name] = facet
    rule = dict(data["facets"]["form"]["rule"])
    if not set(rule.values()) <= set(facets["form"].values or ()):
        raise FacetsError("facets_invalid", "form rule")
    return Facets(facets, rule, int(data["test_vector_rule"]["min_lines"]), pattern,
                  hashlib.sha256(raw).hexdigest())


def form_of(loaded: Facets, kind: str, text: str) -> str:
    """FR-INGEST-37's `form` by rule; a code span is `test_vector` when enough of its lines match."""
    form = loaded.form_rule[kind]
    if kind == "code" and sum(1 for line in text.split("\n") if loaded.vector_pattern.match(line)) >= (
        loaded.vector_lines
    ):
        return "test_vector"
    return form


def check_tag(loaded: Facets, facet: str, value: str) -> None:
    """A tag's facet and value are in the file (`tag_value_unknown`) and its facet is enabled
    (`facet_disabled`)."""
    found = loaded.facets.get(facet)
    if found is None or not _valid(found, value):
        raise FacetsError("tag_value_unknown")
    if not found.enabled:
        raise FacetsError("facet_disabled")


def _valid(facet: Facet, value: str) -> bool:
    if facet.kind == "closed":
        return value in (facet.values or ())
    if facet.kind == "date":
        try:
            date.fromisoformat(value)
        except ValueError:
            return False
        return True
    return TOPIC_PATH.fullmatch(value) is not None
