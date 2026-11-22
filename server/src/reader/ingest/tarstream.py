"""The strict ustar channel between `reader-ingest` and the worker (FR-INGEST-10, -11; INV-INGEST-3).

Written by hand rather than with `tarfile`: the writer is deterministic (mode 0644, mtime 0, uid and gid 0,
no pax headers), and the reader accepts exactly the three regular members in order, nothing else, so no
member can name a path, a link or a device.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO, Final

from reader.ingest.artifact import CAPS, MEMBERS, TOTAL_CAP, ArtifactError

BLOCK: Final = 512
MAGIC: Final = b"ustar\x0000"
END_LIMIT: Final = 20 * BLOCK  # end-of-archive blocks, padded to one 10 KiB record at most
_CHUNK: Final = 1024 * 1024


def _octal(value: int, width: int) -> bytes:
    return f"{value:0{width - 1}o}\0".encode()


def header(name: str, size: int) -> bytes:
    raw = bytearray(BLOCK)
    raw[0:100] = name.encode("ascii").ljust(100, b"\0")
    raw[100:108] = _octal(0o644, 8)
    raw[108:116] = _octal(0, 8)
    raw[116:124] = _octal(0, 8)
    raw[124:136] = _octal(size, 12)
    raw[136:148] = _octal(0, 12)
    raw[148:156] = b" " * 8
    raw[156:157] = b"0"
    raw[257:265] = MAGIC
    raw[148:156] = f"{sum(raw):06o}\0 ".encode()
    return bytes(raw)


def write_dir(root: Path, dest: BinaryIO) -> int:
    """Stream the three members of `root` (O_NOFOLLOW, regular files only) as a tar; returns its size."""
    written = 0
    for name in MEMBERS:
        fd = os.open(root / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ArtifactError("tar_format")
            dest.write(header(name, info.st_size))
            remaining = info.st_size
            while remaining:
                chunk = handle.read(min(_CHUNK, remaining))
                if not chunk:
                    raise ArtifactError("tar_format")
                dest.write(chunk)
                remaining -= len(chunk)
            pad = (-info.st_size) % BLOCK
            dest.write(b"\0" * pad)
            written += BLOCK + info.st_size + pad
    dest.write(b"\0" * (2 * BLOCK))
    return written + 2 * BLOCK


def _exact(src: BinaryIO, size: int) -> bytes:
    data = src.read(size)
    if len(data) != size:
        raise ArtifactError("tar_format")
    return data


def _parse(block: bytes, expected: str) -> int:
    if block[257:265] != MAGIC or block[156:157] not in (b"0", b"\0"):
        raise ArtifactError("tar_format")
    stored = block[148:156]
    probe = bytearray(block)
    probe[148:156] = b" " * 8
    try:
        if int(stored.split(b"\0", 1)[0].strip() or b"x", 8) != sum(probe):
            raise ArtifactError("tar_format")
        size = int(block[124:136].split(b"\0", 1)[0].strip() or b"x", 8)
    except ValueError:
        raise ArtifactError("tar_format") from None
    field, _, rest = block[0:100].partition(b"\0")
    if rest.strip(b"\0") or block[345:500].strip(b"\0") or block[157:257].strip(b"\0"):
        raise ArtifactError("tar_format")  # NUL inside the name, a prefix, or a link name
    if field != expected.encode("ascii"):
        raise ArtifactError("tar_format")
    if size > CAPS[expected]:
        raise ArtifactError("too_large")
    return size


def read_tar(src: BinaryIO, dest: Path, caps: Mapping[str, int] = CAPS) -> None:
    """Read the three members into `dest` (which must be empty); any deviation raises ArtifactError.

    On a failure the caller removes `dest`: partial files may be left in it, never outside it.
    """
    total = 0
    for name in MEMBERS:
        size = _parse(_exact(src, BLOCK), name)
        total += size
        if total > TOTAL_CAP or size > caps[name]:
            raise ArtifactError("too_large")
        fd = os.open(dest / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as out:
            remaining = size
            while remaining:
                chunk = src.read(min(_CHUNK, remaining))
                if not chunk:
                    raise ArtifactError("tar_format")
                out.write(chunk)
                remaining -= len(chunk)
        _exact(src, (-size) % BLOCK)
    end = src.read(END_LIMIT + 1)
    if len(end) < 2 * BLOCK or len(end) > END_LIMIT or end.strip(b"\0") or len(end) % BLOCK:
        raise ArtifactError("tar_format")
