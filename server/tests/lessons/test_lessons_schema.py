"""SRV-LESSON validation and canonical form, without a database.

@trace FR-LESSON-1 FR-LESSON-2 VEC-LESSON-1 VEC-LESSON-2 VEC-LESSON-3 INV-LESSON-1 INV-LESSON-2
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from .conftest import F01, FIXTURES, PACKAGE, case_bytes, cases, need, spec_data

VALIDATION_CODES = {
    "not_utf8",
    "not_nbformat",
    "duplicate_cell_id",
    "missing_reader_metadata",
    "cell_schema",
    "cell_type_mismatch",
    "too_large",
    "too_many_cells",
}
CASES = [c for c in cases()["cases"] if c["write"] is None or c["write"] in VALIDATION_CODES]


def _generator() -> Any:
    spec = importlib.util.spec_from_file_location("gen_f01", FIXTURES / "gen_f01.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _code(data: bytes) -> str | None:
    schema = need("reader.lessons.schema")
    try:
        schema.validate(data)
    except schema.LessonWriteError as error:
        return str(error.code)
    return None


def test_packaged_schema_is_the_vector() -> None:
    packaged = PACKAGE / "cell.schema.json"
    assert packaged.is_file(), "server/src/reader/lessons/cell.schema.json is missing"
    assert packaged.read_bytes() == (FIXTURES / "cell.schema.json").read_bytes()


def test_f01_regenerates_and_packaged_copy_matches() -> None:
    import nbformat

    gen = _generator()
    notebook = gen.build()
    gen.check(notebook)
    assert nbformat.writes(notebook).encode("utf-8") == F01
    packaged = PACKAGE / "fixtures" / "F01.ipynb"
    assert packaged.is_file(), "server/src/reader/lessons/fixtures/F01.ipynb is missing"
    assert packaged.read_bytes() == F01


def test_f01_validates_to_itself() -> None:
    schema = need("reader.lessons.schema")
    validated = schema.validate(F01)
    assert validated.canonical == F01
    assert validated.sha256 == hashlib.sha256(F01).hexdigest()
    assert validated.cell_count == 10
    assert validated.notebook["cells"][6]["id"] == "f01-lab"


def test_validated_notebook_is_the_stored_one() -> None:
    """`notebook` is parsed back from the canonical bytes: the transient keys nbformat strips on write
    (notebook `signature`, `orig_nbformat`; cell `trusted`) are absent from both."""
    schema = need("reader.lessons.schema")
    doc = json.loads(F01)
    doc["metadata"]["signature"] = "sha256:abc"
    doc["metadata"]["orig_nbformat"] = 4
    doc["cells"][6]["metadata"]["trusted"] = True
    validated = schema.validate(json.dumps(doc).encode())
    assert "signature" not in validated.notebook["metadata"]
    assert "orig_nbformat" not in validated.notebook["metadata"]
    assert "trusted" not in validated.notebook["cells"][6]["metadata"]
    assert validated.canonical == F01
    assert validated.notebook == json.loads(validated.canonical)


def test_key_order_permutation_has_the_same_revision() -> None:
    schema = need("reader.lessons.schema")

    def reverse(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: reverse(value[k]) for k in reversed(list(value))}
        if isinstance(value, list):
            return [reverse(v) for v in value]
        return value

    permuted = json.dumps(reverse(json.loads(F01))).encode()
    assert permuted != F01
    assert schema.validate(permuted).canonical == F01


def test_f01_lab_asserts_hold() -> None:
    doc = json.loads(F01)
    lab = next(c for c in doc["cells"] if c["metadata"]["reader"]["kind"] == "lab")
    namespace: dict[str, Any] = {}
    exec("".join(lab["source"]), namespace)  # noqa: S102 — the fixture's own lab
    for expression in lab["metadata"]["reader"]["asserts"]:
        assert eval(expression, namespace), expression  # noqa: S307
    assert _code(F01) is None


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_vector_case(case: dict[str, Any]) -> None:
    assert _code(case_bytes(case)) == case["write"]


def test_a_free_key_map_yields_no_span_ref() -> None:
    """Code review round 1 (r3_gates_revision): `rejected_because: {"span": ...}` is not a span holder."""
    schema = need("reader.lessons.schema")
    case = next(c for c in cases()["cases"] if c["id"] == "rejected-because-span-key")
    mutated = schema.validate(case_bytes(case)).notebook
    assert schema.span_refs(mutated) == schema.span_refs(schema.validate(F01).notebook)
    assert all(not span.startswith("spn: ") for span, _ in schema.span_refs(mutated))


def test_reindented_input_has_the_same_revision() -> None:
    schema = need("reader.lessons.schema")
    doc = json.loads(F01)
    wide = json.dumps(doc, indent=4, ensure_ascii=True).encode()
    assert wide != F01
    validated = schema.validate(wide)
    assert validated.sha256 == hashlib.sha256(F01).hexdigest()
    assert schema.validate(validated.canonical).canonical == validated.canonical


def test_too_many_cells() -> None:
    bounds = spec_data()["bounds"]
    doc = json.loads(F01)
    template = doc["cells"][7]
    doc["cells"] = [dict(copy.deepcopy(template), id=f"c{i}") for i in range(bounds["cells_max"] + 1)]
    assert _code(json.dumps(doc).encode()) == "too_many_cells"


def test_error_codes_are_the_declared_ones() -> None:
    schema = need("reader.lessons.schema")
    declared = set(spec_data()["write_errors"])
    assert declared.issuperset(VALIDATION_CODES)
    error = schema.LessonWriteError("cell_schema")
    assert error.code == "cell_schema"


def _mutations() -> st.SearchStrategy[Any]:
    leaf = st.one_of(
        st.none(), st.booleans(), st.integers(-3, 3), st.floats(allow_nan=False), st.text(max_size=8)
    )
    return st.recursive(
        leaf,
        lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=5), inner, max_size=3),
        max_leaves=6,
    )


KEYS = ["kind", "sectors", "provenance", "claims", "asserts", "front", "x"]


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(cell=st.integers(0, 9), key=st.sampled_from(KEYS), value=_mutations())
def test_any_mutation_is_refused_with_a_code_or_round_trips(cell: int, key: str, value: Any) -> None:
    schema = need("reader.lessons.schema")
    doc = json.loads(F01)
    doc["cells"][cell]["metadata"]["reader"][key] = value
    data = json.dumps(doc, ensure_ascii=False).encode()
    code: str | None = None
    validated = None
    try:
        validated = schema.validate(data)
    except schema.LessonWriteError as error:
        code = str(error.code)
    if validated is None:
        assert code in set(spec_data()["write_errors"])
        return
    again = schema.validate(validated.canonical)
    assert again.canonical == validated.canonical
    assert again.sha256 == hashlib.sha256(validated.canonical).hexdigest()
