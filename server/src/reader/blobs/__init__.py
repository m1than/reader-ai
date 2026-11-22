"""Content-addressed blob store on the `blobs` volume (SRV-LESSON FR-LESSON-3; ADR-0007).

A blob lives at `<root>/sha256/<first 2 hex>/<64 hex>`. Writes go through a `.tmp-<random>` file in the same
directory (write, fsync, rename, fsync the directory), so a reader sees either no file or the whole blob; a
directory this module creates is made durable by an fsync of its parent. Reads verify the hash. `root` is
always a parameter: no environment variable selects it. The functions block; async callers use a worker
thread.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import secrets
from pathlib import Path
from typing import Final

# The volume's mount point in the api and worker containers (OPS-DEPLOY compose.yaml).
DEFAULT_ROOT: Final = Path("/var/lib/reader/blobs")
SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")


class BlobMissing(Exception):
    pass


class BlobCorrupt(Exception):
    pass


def is_sha256(value: object) -> bool:
    return isinstance(value, str) and SHA256_RE.fullmatch(value) is not None


def _path(root: Path, sha256: str) -> Path:
    if not is_sha256(sha256):
        raise ValueError("sha256 must be 64 lowercase hex characters")
    return Path(root) / "sha256" / sha256[:2] / sha256


def _intact(path: Path, sha256: str) -> bool:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest() == sha256
    except FileNotFoundError:
        return False


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_dir(directory: Path) -> None:
    """Create `directory` and any missing parents below an existing one, fsyncing each new entry's parent."""
    missing: list[Path] = []
    current = directory
    while not current.is_dir():
        missing.append(current)
        current = current.parent
    for path in reversed(missing):
        with contextlib.suppress(FileExistsError):
            path.mkdir()
        _fsync_dir(path.parent)


def put(root: Path, data: bytes) -> str:
    """Store `data`; returns its sha256. An intact blob is left untouched; a damaged one is replaced."""
    sha256 = hashlib.sha256(data).hexdigest()
    final = _path(root, sha256)
    if _intact(final, sha256):
        return sha256
    _ensure_dir(final.parent)
    temporary = final.parent / f".tmp-{secrets.token_hex(8)}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, final)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        raise
    _fsync_dir(final.parent)
    return sha256


def get(root: Path, sha256: str) -> bytes:
    """The bytes of a blob; BlobMissing if absent, BlobCorrupt if they do not hash to the name."""
    path = _path(root, sha256)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise BlobMissing(sha256) from None
    if hashlib.sha256(data).hexdigest() != sha256:
        raise BlobCorrupt(sha256)
    return data
