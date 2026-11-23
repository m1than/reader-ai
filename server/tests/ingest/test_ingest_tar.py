"""The tar channel between host and worker (SRV-INGEST FR-INGEST-10, FR-INGEST-11 streams; INV-INGEST-3).

@trace FR-INGEST-10 FR-INGEST-11 INV-INGEST-3
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from .conftest import artifact_files, need, tar_header, tar_member, tar_of

NAMES = ("manifest.json", "spans.jsonl", "outline.json")


def read(tmp: Path, data: bytes) -> list[str]:
    tarstream = need("reader.ingest.tarstream")
    dest = tmp / "dest"
    dest.mkdir(exist_ok=True)
    tarstream.read_tar(io.BytesIO(data), dest)
    return sorted(p.name for p in dest.iterdir())


def rejected(tmp: Path, data: bytes) -> str:
    artifact = need("reader.ingest.artifact")
    with pytest.raises(artifact.ArtifactError) as failure:
        read(tmp, data)
    return str(failure.value.code)


def test_writer_is_deterministic_ustar_and_reader_accepts_it(tmp_path: Path) -> None:
    tarstream = need("reader.ingest.tarstream")
    files = artifact_files()
    src = tmp_path / "src"
    src.mkdir()
    for name, body in files.items():
        (src / name).write_bytes(body)
    first, second = io.BytesIO(), io.BytesIO()
    tarstream.write_dir(src, first)
    tarstream.write_dir(src, second)
    assert first.getvalue() == second.getvalue()
    raw = first.getvalue()
    assert raw[257:263] == b"ustar\x00"
    assert raw[156:157] == b"0"
    assert int(raw[100:107], 8) == 0o644
    assert int(raw[136:147], 8) == 0
    assert read(tmp_path, raw) == sorted(NAMES)
    for name in NAMES:
        assert (tmp_path / "dest" / name).read_bytes() == files[name]


def test_independent_ustar_is_accepted(tmp_path: Path) -> None:
    assert read(tmp_path, tar_of(artifact_files())) == sorted(NAMES)


def test_trailing_zero_record_padding_is_accepted(tmp_path: Path) -> None:
    assert read(tmp_path, tar_of(artifact_files(), end=b"\0" * 10240)) == sorted(NAMES)


HOSTILE: list[tuple[str, str]] = [
    ("symlink", "typeflag 2"),
    ("hardlink", "typeflag 1"),
    ("device", "typeflag 3"),
    ("directory", "typeflag 5"),
    ("fifo", "typeflag 6"),
    ("pax", "typeflag x"),
    ("gnu longname", "typeflag L"),
    ("absolute name", "/manifest.json"),
    ("dotdot", "../manifest.json"),
    ("slash", "a/manifest.json"),
    ("nul in name", "manifest.json\0x"),
    ("duplicate", "duplicate"),
    ("order", "order"),
    ("extra member", "extra"),
    ("missing member", "missing"),
    ("trailing data", "trailing"),
    ("short stream", "short"),
    ("size beyond data", "declared"),
    ("over cap", "cap"),
    ("bad magic", "magic"),
    ("bad checksum", "checksum"),
    ("negative size", "negative"),
    ("size with a leading space", "spaced"),
    ("size with a non-octal digit", "nonoctal"),
]


def hostile(case: str) -> bytes:
    files = artifact_files()
    good = [tar_member(n, files[n]) for n in NAMES]
    end = b"\0" * 1024
    if case.startswith("typeflag "):
        flag = case.split()[1].encode()
        return tar_member("manifest.json", files["manifest.json"], typeflag=flag) + good[1] + good[2] + end
    if case in ("/manifest.json", "../manifest.json", "a/manifest.json", "manifest.json\0x"):
        return tar_member(case, files["manifest.json"]) + good[1] + good[2] + end
    if case == "duplicate":
        return good[0] + good[0] + good[1] + good[2] + end
    if case == "order":
        return good[1] + good[0] + good[2] + end
    if case == "extra":
        return b"".join(good) + tar_member("x.json", b"{}") + end
    if case == "missing":
        return good[0] + good[1] + end
    if case == "trailing":
        return b"".join(good) + end + b"junk"
    if case == "short":
        return b"".join(good)[:-700]
    if case == "declared":
        body = files["outline.json"]
        return good[0] + good[1] + tar_header("outline.json", len(body) + 4096) + body + end
    if case == "cap":
        return good[0] + tar_header("spans.jsonl", 64 * 1024 * 1024 + 1) + end
    if case in ("negative", "spaced", "nonoctal"):
        # A size field int(..., 8) would accept after .strip(), or with a sign; the checksum is valid.
        field = {"negative": b"-0000000001\0", "spaced": b" 0000000004\0", "nonoctal": b"00000000008\0"}[case]
        raw = bytearray(tar_header("manifest.json", 0))
        raw[124:136] = field
        raw[148:156] = b" " * 8
        raw[148:156] = f"{sum(raw):06o}\0 ".encode()
        return bytes(raw) + good[1] + good[2] + end
    if case == "magic":
        return tar_header("manifest.json", len(files["manifest.json"]), magic=b"ustar  \x00") + end
    if case == "checksum":
        raw = bytearray(good[0])
        raw[148:156] = b"0000000\0"
        return bytes(raw) + good[1] + good[2] + end
    raise AssertionError(case)


@pytest.mark.parametrize(("label", "case"), HOSTILE, ids=[c[0] for c in HOSTILE])
def test_hostile_tar_is_rejected(tmp_path: Path, label: str, case: str) -> None:
    assert rejected(tmp_path, hostile(case)) in ("tar_format", "too_large"), label


@settings(max_examples=80, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.binary(max_size=2048), st.integers(min_value=0, max_value=4000))
def test_random_corruption_never_escapes(tmp_path: Path, junk: bytes, at: int) -> None:
    artifact = need("reader.ingest.artifact")
    data = bytearray(tar_of(artifact_files()))
    at = min(at, len(data))
    data[at : at + len(junk)] = junk
    dest = tmp_path / f"d{abs(hash((junk, at)))}"
    dest.mkdir()
    with contextlib.suppress(artifact.ArtifactError):
        need("reader.ingest.tarstream").read_tar(io.BytesIO(bytes(data)), dest)
    # Whatever happens, only the expected names can exist, as regular files, inside dest.
    for path in dest.rglob("*"):
        assert path.name in NAMES and path.is_file() and not path.is_symlink()
