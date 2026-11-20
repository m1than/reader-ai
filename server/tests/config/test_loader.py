"""YAML loader: load failures, precedence and core-schema scalar resolution.

@trace FR-CONFIG-15, VEC-CONFIG-4, INV-CONFIG-3
"""

import base64
import os
from pathlib import Path
from typing import Any

import pytest
from conftest import canaries, load_fixture, rendered, skip_as_root, write_config

LOADER = load_fixture("load-failures.yaml")
CASES = LOADER["cases"]
HEAD = "version: 1\ninstance: { url: https://reader.local, data_dir: ./data }\n"


def _nested(case: dict[str, Any]) -> str:
    """The case's text with `{nest}` replaced by `nest` nested flow sequences."""
    depth = case.get("nest", 0)
    return str(case["text"]).replace("{nest}", "[" * depth + "]" * depth)


def _prepare(tmp_path: Path, case: dict[str, Any]) -> Path:
    path = tmp_path / "reader.yaml"
    if case.get("missing"):
        return path
    if "bytes_b64" in case:
        path.write_bytes(base64.b64decode(case["bytes_b64"]))
    else:
        write_config(tmp_path, _nested(case))
    if case.get("mode") is not None:
        os.chmod(path, case["mode"])
    return path


@pytest.mark.parametrize(
    "case",
    [pytest.param(c, marks=skip_as_root) if c.get("mode") is not None else c for c in CASES],
    ids=[c["name"] for c in CASES],
)
def test_load_failures_give_one_coded_error(tmp_path: Path, case: dict[str, Any]) -> None:
    from reader.config import ConfigError, load_config

    path = _prepare(tmp_path, case)
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    error = caught.value
    assert len(error.problems) == 1
    problem = error.problems[0]
    assert problem.code == case["error"]["code"]
    expected_line = case["error"]["line"]
    if expected_line == "any":
        assert isinstance(problem.line, int)
        assert problem.line >= 1
    else:
        assert problem.line == expected_line
    text = rendered(error)
    for canary in canaries(case.get("text", "")):
        assert canary not in text
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__


@pytest.mark.parametrize("case", LOADER["scalars"], ids=[c["plain"] for c in LOADER["scalars"]])
def test_plain_scalars_resolve_by_the_core_schema(tmp_path: Path, case: dict[str, Any]) -> None:
    from reader.config import ConfigError, load_config

    path = write_config(tmp_path, HEAD + f"sectors:\n  k: {case['plain']}\n")
    expect = case["expect"]
    if "error" in expect:
        with pytest.raises(ConfigError) as caught:
            load_config(path)
        assert [p.code for p in caught.value.problems] == [expect["error"]]
        return
    value = load_config(path).sectors["k"]
    match expect:
        case {"str": s}:
            assert isinstance(value, str)
            assert value == s
        case {"bool": b}:
            assert value is b
        case {"null": _}:
            assert value is None
        case {"int": i}:
            assert type(value) is int
            assert value == i
        case {"float": f}:
            assert type(value) is float
            assert value == f
        case _:
            pytest.fail(f"unknown expectation {expect}")


def test_quoted_merge_key_is_an_ordinary_key(tmp_path: Path) -> None:
    from reader.config import load_config

    case = LOADER["quoted_merge_key"]
    config = load_config(write_config(tmp_path, case["text"]))
    assert dict(config.sectors) == case["expect"]


def test_depth_at_the_bound_loads(tmp_path: Path) -> None:
    from reader.config import load_config

    config = load_config(write_config(tmp_path, _nested(LOADER["depth_ok"])))
    value: Any = config.sectors["a"]
    for _ in range(LOADER["depth_ok"]["nest"] - 1):
        value = value[0]
    assert value == ()
