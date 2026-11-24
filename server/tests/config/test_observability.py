"""SRV-CONFIG FR-CONFIG-17: the optional `observability` section (paired SRV-OBS change).

@trace FR-CONFIG-17
"""

from pathlib import Path

import pytest
from conftest import write_config

HEAD = "version: 1\ninstance: { url: https://reader.local, data_dir: ./data }\n"


def _load(tmp: Path, tail: str) -> object:
    from reader.config import load_config

    return load_config(write_config(tmp, HEAD + tail))


def test_defaults_when_absent_or_empty(tmp_path: Path) -> None:
    for tail in ("", "observability: {}\n"):
        config = _load(tmp_path, tail)
        assert config.observability.enabled is True  # type: ignore[attr-defined]
        assert config.observability.retention_days == 30  # type: ignore[attr-defined]


def test_values(tmp_path: Path) -> None:
    config = _load(tmp_path, "observability: { enabled: false, retention_days: 365 }\n")
    assert config.observability.enabled is False  # type: ignore[attr-defined]
    assert config.observability.retention_days == 365  # type: ignore[attr-defined]
    assert _load(tmp_path, "observability: { retention_days: 1 }\n").observability.retention_days == 1  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("tail", "code", "path"),
    [
        ("observability: { enabled: yes }\n", "wrong_type", "observability.enabled"),
        ("observability: { enabled: 1 }\n", "wrong_type", "observability.enabled"),
        ("observability: { retention_days: 30.0 }\n", "wrong_type", "observability.retention_days"),
        ('observability: { retention_days: "30" }\n', "wrong_type", "observability.retention_days"),
        ("observability: { retention_days: true }\n", "wrong_type", "observability.retention_days"),
        ("observability: { retention_days: 0 }\n", "out_of_range", "observability.retention_days"),
        ("observability: { retention_days: 366 }\n", "out_of_range", "observability.retention_days"),
        ("observability: { sample: 1 }\n", "unknown_key", "observability.<unknown key #1>"),
        ("observability: [true]\n", "wrong_type", "observability"),
    ],
)
def test_refusals(tmp_path: Path, tail: str, code: str, path: str) -> None:
    from reader.config import ConfigError

    with pytest.raises(ConfigError) as caught:
        _load(tmp_path, tail)
    problem = caught.value.problems[0]
    assert (problem.code, problem.path) == (code, path)


def test_model_and_schema_agree() -> None:
    from reader.config import ReaderConfig
    from reader.config.model import export_json_schema

    config = ReaderConfig.model_validate({"version": 1, "instance": {"url": "https://x", "data_dir": "/d"}})
    assert (config.observability.enabled, config.observability.retention_days) == (True, 30)
    schema = export_json_schema()
    assert "observability" in schema["properties"]
    with pytest.raises(ValueError):
        ReaderConfig.model_validate(
            {
                "version": 1,
                "instance": {"url": "https://x", "data_dir": "/d"},
                "observability": {"retention_days": 400},
            }
        )
