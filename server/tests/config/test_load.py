"""reader.yaml loading and validation.

@trace FR-CONFIG-1, FR-CONFIG-2, FR-CONFIG-3, FR-CONFIG-9, FR-CONFIG-10, FR-CONFIG-11
@trace VEC-CONFIG-1, VEC-CONFIG-2, INV-CONFIG-3, INV-CONFIG-4, INV-CONFIG-5
"""

import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from conftest import canaries, load_fixture, rendered, write_config
from hypothesis import given, settings
from hypothesis import strategies as st

VALID_HEAD = "version: 1\ninstance: { url: https://reader.local, data_dir: ./data }\n"
INVALID_CASES = load_fixture("invalid/cases.yaml")["cases"]
EXPECT = load_fixture("reader.example.expect.yaml")


def test_example_loads_with_expected_values(tmp_path: Path) -> None:
    from reader.config import load_config

    example = Path(__file__).resolve().parents[3] / "spec/fixtures/SRV-CONFIG/reader.example.yaml"
    text = example.read_text(encoding="utf-8")
    config = load_config(write_config(tmp_path, text))
    d = str(tmp_path)
    expect = EXPECT["example"]
    assert config.instance.data_dir == expect["instance.data_dir"].format(dir=d)
    assert config.materials[0].file == expect["materials.0.file"].format(dir=d)
    assert str(config.users[0].providers[0].token) == expect["users.0.providers.0.token"]
    assert config.budgets is not None
    assert str(config.budgets.monthly_usd) == expect["budgets.monthly_usd"]
    assert str(config.budgets.warn_at) == expect["budgets.warn_at"]
    assert isinstance(config.budgets.monthly_usd, Decimal)


@pytest.mark.parametrize("case", EXPECT["paths"], ids=lambda c: str(c["data_dir"]))
def test_paths_are_joined_and_normalised_lexically(tmp_path: Path, case: dict[str, str]) -> None:
    from reader.config import load_config

    data_dir = json.dumps(case["data_dir"])
    text = f"version: 1\ninstance: {{ url: https://reader.local, data_dir: {data_dir} }}\n"
    config = load_config(write_config(tmp_path / "app", text))
    assert config.instance.data_dir == case["expect"].format(dir=str(tmp_path))


@pytest.mark.parametrize("case", INVALID_CASES, ids=[c["name"] for c in INVALID_CASES])
def test_invalid_configs_give_the_exact_error_list(tmp_path: Path, case: dict[str, Any]) -> None:
    from reader.config import ConfigError, load_config

    path = write_config(tmp_path, case["yaml"])
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    got = [(p.line, p.path, p.code) for p in caught.value.problems]
    assert got == [(e["line"], e["path"], e["code"]) for e in case["errors"]]


@pytest.mark.parametrize("case", INVALID_CASES, ids=[c["name"] for c in INVALID_CASES])
def test_errors_render_file_line_path_code_and_never_input(tmp_path: Path, case: dict[str, Any]) -> None:
    from reader.config import ConfigError, load_config

    path = write_config(tmp_path, case["yaml"])
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    error = caught.value
    lines = str(error).splitlines()
    for problem in error.problems:
        prefix = f"{path}:{problem.line}: {problem.path}: {problem.code}: "
        assert any(line.startswith(prefix) for line in lines)
    text = rendered(error)
    for canary in canaries(case["yaml"]):
        assert canary not in text
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__


@pytest.mark.parametrize("bad", ["1.0", "true", '"1"', "2", "0"])
def test_version_is_a_strict_int_equal_to_one(tmp_path: Path, bad: str) -> None:
    from reader.config import ConfigError, load_config

    text = f"version: {bad}\ninstance: {{ url: https://reader.local, data_dir: ./d }}\n"
    with pytest.raises(ConfigError) as caught:
        load_config(write_config(tmp_path, text))
    assert [p.path for p in caught.value.problems] == ["version"]


def test_config_is_immutable(tmp_path: Path) -> None:
    from reader.config import load_config

    config = load_config(write_config(tmp_path, VALID_HEAD + "sectors: { a: [1, 2] }\n"))
    with pytest.raises(Exception):  # noqa: B017 - Pydantic raises its own ValidationError for frozen models
        config.version = 2  # type: ignore[misc]
    with pytest.raises(TypeError):
        config.sectors["a"] = 3  # type: ignore[index]
    assert config.sectors == {"a": (1, 2)}


def test_load_config_reads_only_the_given_file_and_no_environment(tmp_path: Path) -> None:
    from reader.config import load_config

    text = VALID_HEAD + (
        "users:\n  - handle: ivan\n    role: owner\n    providers:\n"
        "      - { kind: anthropic-subscription, token: secret://env/READER_NEVER_SET_X }\n"
        "      - { kind: other, token: secret://keychain/reader-ai/x }\n"
        "      - { kind: other, token: secret://file/run/secrets/never_there }\n"
    )
    warm = write_config(tmp_path / "warm", text)
    load_config(warm)  # import everything lazily loaded before watching
    path = write_config(tmp_path / "watched", text)
    opened: list[str] = []
    watching = True

    def hook(event: str, args: tuple[Any, ...]) -> None:
        if watching and event == "open" and args and isinstance(args[0], (str, bytes, Path)):
            opened.append(str(args[0]))

    sys.addaudithook(hook)
    try:
        load_config(path)
    finally:
        watching = False
    assert {Path(p).resolve() for p in opened} <= {path.resolve()}


_segment = st.text(alphabet="abcdefghij", min_size=1, max_size=6)


@st.composite
def _valid_config(draw: st.DrawFn) -> str:
    config: dict[str, Any] = {
        "version": 1,
        "instance": {"url": "https://reader.local", "data_dir": "./" + draw(_segment)},
        "users": [
            {"handle": h, "role": draw(_segment),
             "providers": [{"kind": draw(_segment), "token": f"secret://keychain/{draw(_segment)}/{h}"}]}
            for h in draw(st.lists(_segment, unique=True, max_size=3))
        ],
        "kernels": draw(st.lists(_segment, unique=True, max_size=3)),
        "budgets": {"monthly_usd": draw(st.integers(1, 10_000)), "warn_at": 0.5},
        "sectors": {k: draw(st.integers()) for k in draw(st.lists(_segment, unique=True, max_size=3))},
    }
    return json.dumps(config)  # JSON is valid YAML 1.2


@settings(max_examples=50, deadline=None)
@given(text=_valid_config())
def test_loading_is_idempotent(tmp_path_factory: pytest.TempPathFactory, text: str) -> None:
    from reader.config import load_config

    path = write_config(tmp_path_factory.mktemp("idem"), text)
    assert load_config(path) == load_config(path)
