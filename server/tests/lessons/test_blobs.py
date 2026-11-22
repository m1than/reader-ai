"""SRV-LESSON blob store, without a database.

@trace FR-LESSON-3 INV-LESSON-6
"""

from __future__ import annotations

import hashlib
import stat
import threading
from pathlib import Path

import pytest

from .conftest import F01, need


def _path(root: Path, digest: str) -> Path:
    return root / "sha256" / digest[:2] / digest


def test_put_writes_the_content_addressed_path(tmp_path: Path) -> None:
    blobs = need("reader.blobs")
    digest = blobs.put(tmp_path, F01)
    assert digest == hashlib.sha256(F01).hexdigest()
    final = _path(tmp_path, digest)
    assert final.read_bytes() == F01
    assert stat.S_IMODE(final.stat().st_mode) == 0o600
    assert [p.name for p in final.parent.iterdir()] == [digest], "no temporary file is left behind"
    assert blobs.get(tmp_path, digest) == F01


def test_put_again_leaves_a_good_blob_alone(tmp_path: Path) -> None:
    blobs = need("reader.blobs")
    digest = blobs.put(tmp_path, F01)
    before = _path(tmp_path, digest).stat()
    assert blobs.put(tmp_path, F01) == digest
    after = _path(tmp_path, digest).stat()
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)


def test_put_heals_wrong_bytes_and_get_detects_them(tmp_path: Path) -> None:
    blobs = need("reader.blobs")
    digest = blobs.put(tmp_path, F01)
    final = _path(tmp_path, digest)
    final.write_bytes(b"corrupt")
    with pytest.raises(blobs.BlobCorrupt):
        blobs.get(tmp_path, digest)
    assert blobs.put(tmp_path, F01) == digest
    assert blobs.get(tmp_path, digest) == F01


def test_missing_blob(tmp_path: Path) -> None:
    blobs = need("reader.blobs")
    with pytest.raises(blobs.BlobMissing):
        blobs.get(tmp_path, "0" * 64)


@pytest.mark.parametrize(
    "bad", ["", "../" + "0" * 61, "A" * 64, "0" * 63, "0" * 65, ".tmp-0123", "0" * 64 + "\n"]
)
def test_only_lowercase_hex_names_are_accepted(tmp_path: Path, bad: str) -> None:
    blobs = need("reader.blobs")
    with pytest.raises(ValueError, match="sha256"):
        blobs.get(tmp_path, bad)


def test_temporary_files_are_never_read(tmp_path: Path) -> None:
    blobs = need("reader.blobs")
    digest = blobs.put(tmp_path, F01)
    stray = _path(tmp_path, digest).parent / ".tmp-deadbeef"
    stray.write_bytes(b"partial")
    assert blobs.get(tmp_path, digest) == F01
    with pytest.raises(ValueError, match="sha256"):
        blobs.get(tmp_path, stray.name)


def test_concurrent_writers_of_one_blob_end_with_good_bytes(tmp_path: Path) -> None:
    blobs = need("reader.blobs")

    def writer() -> None:
        for _ in range(20):
            blobs.put(tmp_path, F01)

    threads = [threading.Thread(target=writer) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    digest = hashlib.sha256(F01).hexdigest()
    assert blobs.get(tmp_path, digest) == F01
    assert [p.name for p in _path(tmp_path, digest).parent.iterdir()] == [digest]
