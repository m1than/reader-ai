"""FR-CONFIG-18: material ids starting with `up-` are reserved for uploads (SRV-INGEST FR-INGEST-23).

@trace FR-CONFIG-18
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import write_config

HEAD = "version: 1\ninstance: { url: https://reader.local, data_dir: ./data }\nmaterials:\n"
SHA = "a" * 64


def _material(material_id: str, sha: str = SHA) -> str:
    fields = f'id: "{material_id}", file: ./m.pdf, sha256: "{sha}", license: personal-use, language: ru'
    return f"  - {{ {fields} }}\n"


@pytest.mark.parametrize("material_id", ["up-", "up-abc", "up-0123456789ab"])
def test_reserved_ids_are_refused(tmp_path: Path, material_id: str) -> None:
    from reader.config import ConfigError, load_config

    path = write_config(tmp_path, HEAD + _material("fine") + _material(material_id, "b" * 64))
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    got = [(p.line, p.path, p.code) for p in caught.value.problems]
    assert got == [(5, "materials.1.id", "reserved_id")]
    assert "material ids starting with up- are reserved for uploads" in str(caught.value)


@pytest.mark.parametrize("material_id", ["upx", "upload-notes", "Up-x", "UP-x", "u-p-x"])
def test_other_ids_are_accepted(tmp_path: Path, material_id: str) -> None:
    from reader.config import load_config

    config = load_config(write_config(tmp_path, HEAD + _material(material_id)))
    assert [m.id for m in config.materials] == [material_id]


def test_reserved_id_is_collected_with_other_problems(tmp_path: Path) -> None:
    from reader.config import ConfigError, load_config

    text = HEAD + _material("up-x") + _material("up-y", "b" * 64)
    with pytest.raises(ConfigError) as caught:
        load_config(write_config(tmp_path, text))
    assert [p.code for p in caught.value.problems] == ["reserved_id", "reserved_id"]
