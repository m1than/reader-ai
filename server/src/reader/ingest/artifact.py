"""The ingest artifact: normalisation, writing and validation (SRV-INGEST FR-INGEST-6, -7, -10; INV-INGEST-1).

Stdlib only (imported by the confined converter). The validator is the one both sides run: the host before it
sends (FR-INGEST-10) and the worker on receive and import (FR-INGEST-11, -13). Members are read once
through a directory descriptor with O_NOFOLLOW|O_NONBLOCK, as regular files, at most cap + 1 bytes, because
the converter's output directory is untrusted; the bytes validated are the bytes sent.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import stat
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, BinaryIO, Final

import reader.ingest as ingest
from reader.ingest import CONVERTER_VERSION, locator

FORMAT: Final = "reader-ingest/2"  # revision 3: locators, quote hashes, parse_config and pack_id
MEMBERS: Final = ("manifest.json", "spans.jsonl", "outline.json")
# Structural caps (spec header): ceilings that keep the worker's 2 GiB safe while it parses line by line.
CAPS: Final[Mapping[str, int]] = {
    "manifest.json": 64 * 1024,
    "spans.jsonl": 64 * 1024 * 1024,
    "outline.json": 4 * 1024 * 1024,
}
TOTAL_CAP: Final = 80 * 1024 * 1024
MAX_TEXT_BYTES: Final = 1024 * 1024
MAX_SPANS: Final = 500_000
MAX_PAGES: Final = 5_000
MAX_OUTLINE: Final = 20_000
MAX_TITLE: Final = 1_024
MAX_LEVEL: Final = 16
MAX_COORD: Final = 1e5  # points; structural, far above any page size (A0 is 3,370 pt tall)
KINDS: Final = frozenset({"text", "heading", "list_item", "formula", "code", "table", "figure"})
SPAN_KEYS: Final = frozenset(
    {"seq", "page", "kind", "level", "decoded", "text", "bbox", "locator", "quote_sha256"}
)
OUTLINE_KEYS: Final = frozenset({"seq", "level", "title", "page"})
MANIFEST_KEYS: Final = frozenset(
    {
        "format",
        "material_sha256",
        "converter_version",
        "page_count",
        "pages_converted",
        "pages_without_text",
        "span_count",
        "outline_count",
        "outline_source",
        "spans_sha256",
        "outline_sha256",
        "parse_config",
        "pack_id",
    }
)
SHA256: Final = re.compile(r"[0-9a-f]{64}")
_CONTROLS: Final = re.compile(r"[\x00-\x08\x0b-\x1f\ud800-\udfff]")
SOFT_HYPHEN: Final = "­"


class ArtifactError(Exception):
    """A rejected artifact; `code` is fixed text and never quotes the artifact."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ---- normalisation (FR-INGEST-6, -7) -----------------------------------------------------------------------


def normalise(text: str, *, code: bool) -> str:
    """FR-INGEST-6/-7, in an order that makes it idempotent: remove first, then strip, NFC last.

    Removed: U+0000, C0 controls other than \\n and \\t, lone surrogates, and U+00AD outside code. Code: CRLF
    becomes LF before the removal, and each line loses its trailing spaces after it; U+00AD stays.
    """
    text = _CONTROLS.sub("", text.replace("\r\n", "\n") if code else text.replace(SOFT_HYPHEN, ""))
    if code:
        text = "\n".join(line.rstrip(" ") for line in text.split("\n"))
    return unicodedata.normalize("NFC", text)


def _normalised(text: str, *, code: bool) -> bool:
    return not _CONTROLS.search(text) and normalise(text, code=code) == text


def canonical(value: Any) -> bytes:
    """Sorted keys, separators `,` and `:`, UTF-8, non-ASCII unescaped; NaN and infinities are refused."""
    dumped = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return dumped.encode("utf-8")


def artifact_id(manifest: bytes) -> str:
    return hashlib.sha256(manifest).hexdigest()


def pack_id(material_sha256: str, parse_config: Mapping[str, Any]) -> str:
    """FR-INGEST-35: sha256 of the canonical {material_sha256, parse_config}; no owner."""
    body = canonical({"material_sha256": material_sha256, "parse_config": dict(parse_config)})
    return hashlib.sha256(body).hexdigest()


def with_locators(material_sha256: str, spans: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Each span with FR-INGEST-33's locator over its page's spans and FR-INGEST-34's quote hash."""
    pages: dict[int, list[int]] = {}
    for i, span in enumerate(spans):
        pages.setdefault(int(span["page"]), []).append(i)
    found: dict[int, str] = {}
    for page, members in pages.items():
        ids = locator.page_locators(material_sha256, page, [str(spans[i]["text"]) for i in members])
        found.update(zip(members, ids, strict=True))
    return [
        {**span, "locator": found[i], "quote_sha256": locator.quote_sha256(str(span["text"]))}
        for i, span in enumerate(spans)
    ]


# ---- writing (FR-INGEST-10) --------------------------------------------------------------------------------


def write_artifact(
    out: Path,
    *,
    material_sha256: str,
    page_count: int,
    pages_converted: int,
    pages_without_text: Sequence[int],
    outline_source: str,
    spans: Sequence[Mapping[str, Any]],
    outline: Sequence[Mapping[str, Any]],
    converter_version: str = CONVERTER_VERSION,
) -> str:
    """Write the three members into `out` (created) and return the artifact id; the locators, quote hashes,
    parse_config and pack_id are computed here (revision 3), never taken from the input."""
    bare = [{k: v for k, v in s.items() if k not in ("locator", "quote_sha256")} for s in spans]
    spans_bytes = b"".join(canonical(s) + b"\n" for s in with_locators(material_sha256, bare))
    config = ingest.PARSE_CONFIGS[converter_version]
    outline_bytes = canonical([dict(o) for o in outline])
    manifest = canonical(
        {
            "format": FORMAT,
            "material_sha256": material_sha256,
            "converter_version": converter_version,
            "page_count": page_count,
            "pages_converted": pages_converted,
            "pages_without_text": list(pages_without_text),
            "span_count": len(spans),
            "outline_count": len(outline),
            "outline_source": outline_source,
            "spans_sha256": hashlib.sha256(spans_bytes).hexdigest(),
            "outline_sha256": hashlib.sha256(outline_bytes).hexdigest(),
            "parse_config": dict(config),
            "pack_id": pack_id(material_sha256, config),
        }
    )
    out.mkdir(parents=True, exist_ok=True)
    for name, body in zip(MEMBERS, (manifest, spans_bytes, outline_bytes), strict=True):
        (out / name).write_bytes(body)
    return artifact_id(manifest)


# ---- validation (INV-INGEST-1) -----------------------------------------------------------------------------


def _reject_constant(_: str) -> Any:
    raise ValueError("non-finite number")


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, RecursionError, OverflowError) as error:
        raise ArtifactError("schema") from error


def _int(value: Any, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ArtifactError("schema")
    return value


def _check_manifest(raw: bytes) -> dict[str, Any]:
    meta = _json(raw)
    if not isinstance(meta, dict) or set(meta) != MANIFEST_KEYS:
        raise ArtifactError("schema")
    try:
        if canonical(meta) != raw:
            raise ArtifactError("manifest_not_canonical")
    except (TypeError, ValueError) as error:
        raise ArtifactError("schema") from error
    if meta["format"] != FORMAT or meta["outline_source"] not in ("bookmarks", "headings"):
        raise ArtifactError("schema")
    for field in ("material_sha256", "spans_sha256", "outline_sha256"):
        if not isinstance(meta[field], str) or not SHA256.fullmatch(meta[field]):
            raise ArtifactError("schema")
    version = meta["converter_version"]
    if not isinstance(version, str) or not 0 < len(version) <= 64:
        raise ArtifactError("schema")
    pages = _int(meta["page_count"], 1, MAX_PAGES)
    _int(meta["pages_converted"], 0, pages)
    _int(meta["span_count"], 0, MAX_SPANS)
    _int(meta["outline_count"], 0, MAX_OUTLINE)
    without = meta["pages_without_text"]
    if not isinstance(without, list) or any(type(p) is not int or not 1 <= p <= pages for p in without):
        raise ArtifactError("schema")
    if without != sorted(set(without)):
        raise ArtifactError("schema")
    if not isinstance(meta["parse_config"], dict) or not isinstance(meta["pack_id"], str):
        raise ArtifactError("schema")
    pinned = ingest.PARSE_CONFIGS.get(version)
    if pinned is None:
        raise ArtifactError("version_mismatch")
    if meta["parse_config"] != pinned or meta["pack_id"] != pack_id(meta["material_sha256"], pinned):
        raise ArtifactError("pack_mismatch")
    return meta


def _finite(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def check_span(span: Any, seq: int, page_count: int) -> None:
    if not isinstance(span, dict) or set(span) != SPAN_KEYS:
        raise ArtifactError("schema")
    if type(span["seq"]) is not int or span["seq"] != seq:
        raise ArtifactError("schema")
    _int(span["page"], 1, page_count)
    kind = span["kind"]
    if kind not in KINDS:
        raise ArtifactError("schema")
    if kind == "heading":
        _int(span["level"], 1, MAX_LEVEL)
    elif span["level"] is not None:
        raise ArtifactError("schema")
    if kind == "formula":
        if type(span["decoded"]) is not bool:
            raise ArtifactError("schema")
    elif span["decoded"] is not None:
        raise ArtifactError("schema")
    text = span["text"]
    if not isinstance(text, str) or len(text.encode("utf-8", "surrogatepass")) > MAX_TEXT_BYTES:
        raise ArtifactError("schema")
    if not _normalised(text, code=kind == "code"):
        raise ArtifactError("schema")
    bbox = span["bbox"]
    if not isinstance(bbox, list) or len(bbox) != 4 or not all(_finite(v) for v in bbox):
        raise ArtifactError("schema")
    x0, y0, x1, y1 = bbox
    if not (0 <= x0 <= x1 <= MAX_COORD and 0 <= y0 <= y1 <= MAX_COORD):
        raise ArtifactError("schema")
    if not isinstance(span["locator"], str) or not isinstance(span["quote_sha256"], str):
        raise ArtifactError("schema")
    if span["quote_sha256"] != locator.quote_sha256(text):
        raise ArtifactError("locator_mismatch")


def check_outline(entries: Any, page_count: int) -> None:
    if not isinstance(entries, list) or len(entries) > MAX_OUTLINE:
        raise ArtifactError("schema")
    for seq, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != OUTLINE_KEYS:
            raise ArtifactError("schema")
        if type(entry["seq"]) is not int or entry["seq"] != seq:
            raise ArtifactError("schema")
        _int(entry["level"], 1, MAX_LEVEL)
        title = entry["title"]
        if not isinstance(title, str) or len(title) > MAX_TITLE or not _normalised(title, code=False):
            raise ArtifactError("schema")
        if entry["page"] is not None:
            _int(entry["page"], 1, page_count)


def _spans(handle: BinaryIO, page_count: int, material_sha256: str) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    pages: dict[int, tuple[list[int], list[str]]] = {}  # page → (code-point lengths, locators); no texts
    for line in handle:
        digest.update(line)
        if not line.endswith(b"\n"):
            raise ArtifactError("schema")
        if count >= MAX_SPANS:
            raise ArtifactError("schema")
        span = _json(line)
        check_span(span, count, page_count)
        lengths, ids = pages.setdefault(span["page"], ([], []))
        lengths.append(len(span["text"]))
        ids.append(span["locator"])
        count += 1
    for page, (lengths, ids) in pages.items():  # INV-INGEST-1 rev: FR-INGEST-33 over the page's spans
        want = [locator.pdf_locator(material_sha256, page, *r) for r in locator.ranges_of_lengths(lengths)]
        if ids != want:
            raise ArtifactError("locator_mismatch")
    return count, digest.hexdigest()


def validate(manifest: bytes, spans: BinaryIO, outline: bytes) -> dict[str, Any]:
    """Validate the three members; returns the manifest. Raises ArtifactError with a fixed code."""
    meta = _check_manifest(manifest)
    if hashlib.sha256(outline).hexdigest() != meta["outline_sha256"]:
        raise ArtifactError("hash_mismatch")
    if meta["pages_converted"] != meta["page_count"]:
        raise ArtifactError("incomplete")
    entries = _json(outline)
    check_outline(entries, meta["page_count"])
    count, digest = _spans(spans, meta["page_count"], meta["material_sha256"])
    if digest != meta["spans_sha256"]:
        raise ArtifactError("hash_mismatch")
    if count != meta["span_count"] or len(entries) != meta["outline_count"]:
        raise ArtifactError("schema")
    return meta


def read_members(root: Path) -> dict[str, bytes]:
    """Read the three members once, through a directory descriptor: no symlink is followed (the directory
    itself included), FIFOs do not block, each must be a regular file, and at most cap + 1 bytes are read."""
    flags = os.O_RDONLY | os.O_NOFOLLOW
    try:
        dir_fd = os.open(root, flags | os.O_DIRECTORY)
    except OSError as error:
        raise ArtifactError("missing") from error
    members: dict[str, bytes] = {}
    try:
        for name in MEMBERS:
            try:
                fd = os.open(name, flags | os.O_NONBLOCK, dir_fd=dir_fd)
            except OSError as error:
                raise ArtifactError("missing") from error
            with os.fdopen(fd, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise ArtifactError("tar_format")
                data = handle.read(CAPS[name] + 1)
            if len(data) > CAPS[name]:
                raise ArtifactError("too_large")
            members[name] = data
    finally:
        os.close(dir_fd)
    return members


def validate_members(members: Mapping[str, bytes]) -> dict[str, Any]:
    """Validate member bytes read once (read_members); returns the manifest."""
    return validate(members["manifest.json"], io.BytesIO(members["spans.jsonl"]), members["outline.json"])


def validate_dir(root: Path) -> dict[str, Any]:
    return validate_members(read_members(root))
