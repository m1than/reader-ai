"""Locator span ids, outline ids and quote hashes over VEC-INGEST-8 (SRV-INGEST revision 3, ADR-0039 P2).

@trace FR-INGEST-33 FR-INGEST-34 VEC-INGEST-8 FR-INGEST-35
"""

from __future__ import annotations

import hashlib
import unicodedata
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from ruamel.yaml import YAML

from .conftest import need

FIXTURE = Path(__file__).resolve().parents[3] / "spec" / "fixtures" / "SRV-INGEST" / "locators.yaml"
CASES: dict[str, Any] = YAML(typ="safe").load(FIXTURE.read_text(encoding="utf-8"))
SHA: str = CASES["material_sha256"]


def loc() -> Any:
    return need("reader.ingest.locator")


@pytest.mark.parametrize("case", CASES["pages"], ids=lambda c: c["name"])
def test_page_locators(case: dict[str, Any]) -> None:
    m = loc()
    texts = case["texts"]
    assert m.page_text(texts) == case["page_text"]
    expected = [f"spn:{SHA}:p{case['page']}:{r}" for r in case["ranges"]]
    assert m.page_locators(SHA, case["page"], texts) == expected
    page = m.page_text(texts)
    for text, span_id in zip(texts, expected, strict=True):
        parsed = m.parse(span_id)
        assert page[parsed.start : parsed.end] == text
        assert m.format(parsed) == span_id


@pytest.mark.parametrize("case", CASES["files"], ids=lambda c: c["id"])
def test_file_locators(case: dict[str, Any]) -> None:
    m = loc()
    a, b = case["lines"]
    span_id = m.file_locator(SHA, case["path"], a, b)
    assert span_id == f"spn:{SHA}:{case['id']}"
    parsed = m.parse(span_id)
    assert (parsed.path, parsed.line_start, parsed.line_end) == (case["path"], a, b)
    assert m.format(parsed) == span_id
    # A reader that splits on the last ":" still finds the range (FR-NOTEBOOK-2).
    assert span_id.rsplit(":", 1)[1] == f"L{a}-{b}"


@pytest.mark.parametrize("bad", CASES["malformed"])
def test_malformed_refused(bad: str) -> None:
    m = loc()
    with pytest.raises(m.LocatorError) as info:
        m.parse(bad.replace("{sha}", SHA))
    assert info.value.code == "locator_invalid"


def test_file_path_bound_and_controls() -> None:
    m = loc()
    with pytest.raises(m.LocatorError):
        m.file_locator(SHA, "a" * 1025, 1, 1)
    m.file_locator(SHA, "a" * 1024, 1, 1)
    with pytest.raises(m.LocatorError):
        m.file_locator(SHA, "a\u0001b", 1, 1)
    with pytest.raises(m.LocatorError):
        m.file_locator(SHA, "a", 3, 2)


@pytest.mark.parametrize("case", CASES["outlines"], ids=lambda c: c["name"])
def test_outline_paths(case: dict[str, Any]) -> None:
    m = loc()
    assert m.outline_paths(case["levels"]) == case["paths"]
    assert m.outline_id(SHA, case["paths"][0]) == f"sec:{SHA}:{case['paths'][0]}"


@pytest.mark.parametrize("case", CASES["quote_hashes"], ids=lambda c: c["sha256"][:8])
def test_quote_hash(case: dict[str, Any]) -> None:
    assert loc().quote_sha256(case["text"]) == case["sha256"]


def test_equal_hash_ordinals() -> None:
    case = CASES["equal_hash_ordinals"]
    assert loc().equal_hash_ordinals(case["texts"]) == case["ordinals"]


def test_legacy_id_is_not_a_locator() -> None:
    m = loc()
    assert m.is_legacy_id(f"{SHA}:12")
    assert not m.is_legacy_id(f"spn:{SHA}:p1:0-1")


_nfc_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc")), max_size=12
).map(lambda s: unicodedata.normalize("NFC", s))


@given(st.lists(st.one_of(st.just(""), _nfc_text), min_size=1, max_size=12), st.integers(1, 5000))
def test_property_locators(texts: list[str], page: int) -> None:
    m = loc()
    ids = m.page_locators(SHA, page, texts)
    body = m.page_text(texts)
    assert len(set(ids)) == len(ids)
    starts = [m.parse(i).start for i in ids]
    for i, (text, span_id) in enumerate(zip(texts, ids, strict=True)):
        parsed = m.parse(span_id)
        assert m.format(parsed) == span_id
        assert body[parsed.start : parsed.end] == text
        if text == "":
            following = [starts[j] for j in range(i + 1, len(texts)) if texts[j] != ""]
            assert parsed.start == (following[0] if following else len(body))
            assert parsed.ordinal is not None
        else:
            assert parsed.ordinal is None
    assert m.quote_sha256(body) == hashlib.sha256(body.encode()).hexdigest()


R2_PROFILE = "d7a0ee528c62746bf86f6b813d4a8464b98bbb333c1dbb4fffb9d56433ce2bcb"  # converter.sb at 47aa55a


def test_parse_configs_are_pinned_literals() -> None:
    ingest = need("reader.ingest")
    configs = ingest.PARSE_CONFIGS
    assert list(configs) == ["docling-2.151.1+r2", "docling-2.151.1+r3"]  # converter-version order
    profile = Path(ingest.__file__).resolve().parent / "host" / "converter.sb"
    assert configs["docling-2.151.1+r3"]["profile_sha256"] == hashlib.sha256(profile.read_bytes()).hexdigest()
    assert configs["docling-2.151.1+r2"]["profile_sha256"] == R2_PROFILE
    for version, config in configs.items():
        assert config["converter_version"] == version
        assert (config["docling"], config["batch_pages"]) == ("2.151.1", 50)
        assert config["pipeline"] == {"do_ocr": False, "do_table_structure": True,
                                      "do_formula_enrichment": True, "do_code_enrichment": False,
                                      "device": "cpu"}
    assert ingest.LEGACY_VERSIONS == ("docling-2.151.1+r2",)
