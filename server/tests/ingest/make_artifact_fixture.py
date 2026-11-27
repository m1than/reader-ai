"""Generator of VEC-INGEST-1 (spec/modules/SRV-INGEST.yaml): a valid synthetic artifact, no book text.

    uv run python tests/ingest/make_artifact_fixture.py       # writes spec/fixtures/SRV-INGEST/artifact-ok/

Stdlib only and independent of `reader.ingest`, so the fixture is an oracle for the module, not its output.
Format reader-ingest/2 (revision 3): locators (FR-INGEST-33), quote hashes (FR-INGEST-34), parse_config and
pack_id (FR-INGEST-35), each computed here by its own code from the spec's words.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

REPO: Final = Path(__file__).resolve().parents[3]
OUT: Final = REPO / "spec" / "fixtures" / "SRV-INGEST" / "artifact-ok"
MATERIAL_SHA256: Final = "a" * 64
CONVERTER_VERSION: Final = "docling-2.151.1+r3"
FORMAT: Final = "reader-ingest/2"
# FR-INGEST-35's pinned parse configs, copied as literals (an oracle, not an import).
_PIPELINE: Final = {"do_ocr": False, "do_table_structure": True, "do_formula_enrichment": True,
                    "do_code_enrichment": False, "device": "cpu"}
_PROFILE: Final = "d7a0ee528c62746bf86f6b813d4a8464b98bbb333c1dbb4fffb9d56433ce2bcb"
PARSE_CONFIGS: Final = {
    v: {"converter_version": v, "docling": "2.151.1", "pipeline": _PIPELINE, "batch_pages": 50,
        "profile_sha256": _PROFILE}
    for v in ("docling-2.151.1+r2", "docling-2.151.1+r3")
}

SPANS: Final[list[dict[str, Any]]] = [
    {"seq": 0, "page": 1, "kind": "heading", "level": 1, "decoded": None, "text": "Глава 1",
     "bbox": [72.0, 60.0, 300.0, 80.0]},
    {"seq": 1, "page": 1, "kind": "text", "level": None, "decoded": None, "text": "Абзац с текстом.\tTab ok",
     "bbox": [72.0, 90.0, 520.0, 130.0]},
    {"seq": 2, "page": 1, "kind": "list_item", "level": None, "decoded": None, "text": "1. Шаг",
     "bbox": [72.0, 140.0, 520.0, 155.0]},
    {"seq": 3, "page": 1, "kind": "code", "level": None, "decoded": None,
     "text": "$ run --x\nline­two", "bbox": [90.0, 160.0, 400.0, 190.0]},
    {"seq": 4, "page": 2, "kind": "formula", "level": None, "decoded": True, "text": "E = mc^2",
     "bbox": [200.0, 100.0, 300.0, 120.0]},
    {"seq": 5, "page": 2, "kind": "formula", "level": None, "decoded": False, "text": "",
     "bbox": [200.0, 130.0, 300.0, 150.0]},
    {"seq": 6, "page": 2, "kind": "table", "level": None, "decoded": None,
     "text": "| a | b |\n|---|---|\n| 1 | 2 |", "bbox": [72.0, 200.0, 400.0, 260.0]},
    {"seq": 7, "page": 2, "kind": "figure", "level": None, "decoded": None, "text": "",
     "bbox": [72.0, 300.0, 400.0, 500.0]},
]
OUTLINE: Final[list[dict[str, Any]]] = [
    {"seq": 0, "level": 1, "title": "Глава 1", "page": 1},
    {"seq": 1, "level": 2, "title": "Раздел", "page": None},
]


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def with_locators(spans: Sequence[Mapping[str, Any]], material_sha256: str) -> list[dict[str, Any]]:
    """Each span plus its locator and quote hash. Offsets count code points into the page text (the non-empty
    texts joined by one newline); an empty span is a zero-length range where the next non-empty span starts
    (or the page text ends), with #<ordinal> among the empty spans at that offset."""
    pages: dict[int, list[int]] = {}
    for i, span in enumerate(spans):
        pages.setdefault(int(span["page"]), []).append(i)
    found: dict[int, str] = {}
    for page, members in pages.items():
        texts = [str(spans[i]["text"]) for i in members]
        starts: dict[int, int] = {}
        offset = -1
        for i, t in zip(members, texts, strict=True):
            if t:
                starts[i] = offset + 1
                offset += 1 + len(t)
        end = max(offset, 0)
        ordinals: dict[int, int] = {}
        for k, (i, t) in enumerate(zip(members, texts, strict=True)):
            prefix = f"spn:{material_sha256}:p{page}"
            if t:
                found[i] = f"{prefix}:{starts[i]}-{starts[i] + len(t)}"
                continue
            at = next((starts[j] for j in members[k + 1 :] if j in starts), end)
            found[i] = f"{prefix}:{at}-{at}#{ordinals.get(at, 0)}"
            ordinals[at] = ordinals.get(at, 0) + 1
    return [
        {**span, "locator": found[i], "quote_sha256": hashlib.sha256(str(span["text"]).encode()).hexdigest()}
        for i, span in enumerate(spans)
    ]


def pack_fields(material_sha256: str, converter_version: str) -> dict[str, Any]:
    config = PARSE_CONFIGS[converter_version]
    pid = hashlib.sha256(canonical({"material_sha256": material_sha256, "parse_config": config})).hexdigest()
    return {"parse_config": config, "pack_id": pid}


def files() -> dict[str, bytes]:
    spans = b"".join(canonical(s) + b"\n" for s in with_locators(SPANS, MATERIAL_SHA256))
    outline = canonical(OUTLINE)
    manifest = canonical(
        {
            "format": FORMAT,
            "material_sha256": MATERIAL_SHA256,
            "converter_version": CONVERTER_VERSION,
            "page_count": 2,
            "pages_converted": 2,
            "pages_without_text": [2],
            "span_count": len(SPANS),
            "outline_count": len(OUTLINE),
            "outline_source": "bookmarks",
            "spans_sha256": hashlib.sha256(spans).hexdigest(),
            "outline_sha256": hashlib.sha256(outline).hexdigest(),
            **pack_fields(MATERIAL_SHA256, CONVERTER_VERSION),
        }
    )
    return {"manifest.json": manifest, "spans.jsonl": spans, "outline.json": outline}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    data = files()
    for name, body in data.items():
        (OUT / name).write_bytes(body)
    (OUT.parent / "artifact-ok.id").write_text(hashlib.sha256(data["manifest.json"]).hexdigest() + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
