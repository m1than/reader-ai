"""The facets file, its schema and the form rule (SRV-INGEST [unit]).

@trace FR-INGEST-37 VEC-INGEST-9
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from .conftest import FIXTURES, REPO, need

CASES = YAML(typ="safe").load((FIXTURES / "facets-cases.yaml").read_text(encoding="utf-8"))
SEVEN = {"role", "reliability", "credibility", "as_of", "license", "form", "topic"}
REQUIREMENT = re.compile(r"(FR|INV|NFR)-[A-Z]+-[0-9]+")


def committed() -> dict[str, Any]:
    facets = need("reader.ingest.facets")
    data: dict[str, Any] = YAML(typ="safe").load(facets.FACETS_FILE.read_text(encoding="utf-8"))
    return data


def changed(case: dict[str, Any]) -> dict[str, Any]:
    data = copy.deepcopy(committed())
    *parents, last = case["path"]
    node = data
    for key in parents:
        node = node[key]
    if case.get("delete"):
        del node[last]
    else:
        node[last] = case["set"]
    return data


def write(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "facets.yaml"
    with path.open("w", encoding="utf-8") as out:
        YAML(typ="safe").dump(data, out)
    return path


def test_the_committed_file_validates() -> None:
    facets = need("reader.ingest.facets")
    loaded = facets.load()
    assert set(loaded.facets) == SEVEN
    assert loaded.facets["license"].values == ("personal-use", "shareable")
    assert loaded.facets["reliability"].values == tuple("ABCDEF")
    assert loaded.facets["credibility"].values == tuple("123456")
    assert re.fullmatch(r"[0-9a-f]{64}", loaded.sha256)


@pytest.mark.parametrize("case", CASES["invalid"], ids=[c["name"] for c in CASES["invalid"]])
def test_each_invalid_variant_is_refused(case: dict[str, Any], tmp_path: Path) -> None:
    facets = need("reader.ingest.facets")
    with pytest.raises(facets.FacetsError) as refused:
        facets.load(write(tmp_path, changed(case)))
    assert refused.value.code == "facets_invalid"


def test_a_round_trip_of_the_committed_file_still_loads(tmp_path: Path) -> None:
    need("reader.ingest.facets").load(write(tmp_path, committed()))  # the harness itself changes nothing


@pytest.mark.parametrize("kind", sorted(CASES["forms"]))
def test_form_of_every_kind(kind: str) -> None:
    facets = need("reader.ingest.facets")
    assert facets.form_of(facets.load(), kind, "plain words") == CASES["forms"][kind]


def test_the_form_rule_covers_exactly_the_span_kinds() -> None:
    kinds = need("reader.ingest.artifact").KINDS
    assert set(CASES["forms"]) == set(kinds) == set(committed()["facets"]["form"]["rule"])


@pytest.mark.parametrize("case", CASES["test_vectors"], ids=[c["name"] for c in CASES["test_vectors"]])
def test_the_test_vector_rule(case: dict[str, Any]) -> None:
    facets = need("reader.ingest.facets")
    assert facets.form_of(facets.load(), "code", case["text"]) == case["form"]
    for kind in ("text", "table"):  # the rule reads code spans only
        assert facets.form_of(facets.load(), kind, case["text"]) == CASES["forms"][kind]


def declared_ids() -> set[str]:
    found: set[str] = set()
    for spec in (REPO / "spec" / "modules").glob("*.yaml"):
        data = YAML(typ="safe").load(spec.read_text(encoding="utf-8"))
        for section in ("requirements", "invariants", "nfr"):
            found |= {str(item["id"]) for item in data.get(section) or ()}
    return found


def test_enabled_consumers_resolve_and_form_is_disabled() -> None:
    views = need("reader.ingest.manifest").VIEWS
    ids = declared_ids()
    loaded = need("reader.ingest.facets").load()
    for name, facet in loaded.facets.items():
        if facet.enabled:
            assert facet.consumer in views or (
                REQUIREMENT.fullmatch(facet.consumer) and facet.consumer in ids), name
    assert [n for n, f in loaded.facets.items() if f.enabled] == ["license"]
    assert loaded.facets["license"].consumer == "profile_for_pack"
    assert loaded.facets["form"].enabled is False


def test_tag_checks() -> None:
    facets = need("reader.ingest.facets")
    loaded = facets.load()
    facets.check_tag(loaded, "license", "shareable")
    for facet, value, code in (("license", "public-domain", "tag_value_unknown"),
                               ("mood", "calm", "tag_value_unknown"),
                               ("form", "code", "facet_disabled"),
                               ("role", "map", "facet_disabled")):
        with pytest.raises(facets.FacetsError) as refused:
            facets.check_tag(loaded, facet, value)
        assert refused.value.code == code, (facet, value)
