"""Generator of VEC-INGEST-1 (spec/modules/SRV-INGEST.yaml): a valid synthetic artifact, no book text.

    uv run python tests/ingest/make_artifact_fixture.py       # writes spec/fixtures/SRV-INGEST/artifact-ok/

Stdlib only and independent of `reader.ingest`, so the fixture is an oracle for the module, not its output.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Final

REPO: Final = Path(__file__).resolve().parents[3]
OUT: Final = REPO / "spec" / "fixtures" / "SRV-INGEST" / "artifact-ok"
MATERIAL_SHA256: Final = "a" * 64
CONVERTER_VERSION: Final = "docling-2.151.1+r1"

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


def files() -> dict[str, bytes]:
    spans = b"".join(canonical(s) + b"\n" for s in SPANS)
    outline = canonical(OUTLINE)
    manifest = canonical(
        {
            "format": "reader-ingest/1",
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
