"""Generator of VEC-INGEST-3 and VEC-INGEST-4 (spec/modules/SRV-INGEST.yaml) from the owner's pilot PDF.

Runs on the owner's Mac with pypdfium2 (the converter venv's), never in CI, and never prints or writes book
text: only boxes, counts, page numbers and sha256 values.

    <venv>/bin/python -I tests/ingest/pilot_expectations.py candidates <pdf> <scratch dir>
        monospace text runs of the sample's code pages as candidate boxes, plus page renders with the boxes
        drawn, into a scratch directory outside the repository, for the agent to confirm by eye
    <venv>/bin/python -I tests/ingest/pilot_expectations.py build <pdf> <recorded.json>
        writes spec/fixtures/SRV-INGEST/pilot-expectations.yaml and pilot-outline.yaml

The code hashes are taken from the text layer inside boxes recorded from the rendered pages, normalised as
FR-INGEST-7 says, so they do not depend on Docling. Formula counts are the M1 run's (RESULT.md), table
shapes are counted on the rendered pages, and the outline is read with pypdfium2 directly.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Final

REPO: Final = Path(__file__).resolve().parents[3]
OUT: Final = REPO / "spec" / "fixtures" / "SRV-INGEST"
PILOT_SHA: Final = "61e8d4c390ba63c3f13efe0860d5042f8d1b19ba8f26b4f2e14e73478bc9e2b2"
CODE_PAGES: Final = (133, 141, 143, 378, 381, 461, 468)
_CONTROLS: Final = re.compile(r"[\x00-\x08\x0b-\x1f\ud800-\udfff]")


def normalise_code(text: str) -> str:
    """FR-INGEST-7, written independently of reader.ingest.artifact."""
    text = "\n".join(line.rstrip(" ") for line in text.replace("\r\n", "\n").split("\n"))
    return unicodedata.normalize("NFC", _CONTROLS.sub("", text))


def _open(path: str) -> Any:
    import pypdfium2

    data = Path(path).read_bytes()
    if hashlib.sha256(data).hexdigest() != PILOT_SHA:
        raise SystemExit("not the pilot PDF (sha256 mismatch)")
    return pypdfium2.PdfDocument(path)


def candidates(pdf_path: str, scratch: str) -> None:
    """Group monospace characters into blocks; draw them on page renders in `scratch`."""
    import pypdfium2.raw as raw
    from PIL import ImageDraw

    pdf = _open(pdf_path)
    out = Path(scratch)
    out.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {}
    for number in CODE_PAGES:
        page = pdf[number - 1]
        height = page.get_height()
        text_page = page.get_textpage()
        lines: dict[int, list[float]] = {}
        buffer = (raw.c_char * 256)()
        for index in range(text_page.count_chars()):
            flags = raw.c_int()
            raw.FPDFText_GetFontInfo(text_page, index, buffer, 256, flags)
            name = bytes(buffer).split(b"\0", 1)[0].decode("latin-1").lower()
            if not any(token in name for token in ("mono", "courier", "consol", "code")):
                continue
            box = text_page.get_charbox(index)  # left, bottom, right, top (bottom-left origin)
            key = round((height - box[3]) / 2)  # top edge, 2 pt buckets
            row = lines.setdefault(key, [box[0], height - box[3], box[2], height - box[1]])
            row[0], row[1] = min(row[0], box[0]), min(row[1], height - box[3])
            row[2], row[3] = max(row[2], box[2]), max(row[3], height - box[1])
        blocks: list[list[float]] = []
        for _, row in sorted(lines.items()):
            if blocks and row[1] - blocks[-1][3] < 8:
                block = blocks[-1]
                block[0], block[2], block[3] = min(block[0], row[0]), max(block[2], row[2]), row[3]
            else:
                blocks.append(list(row))
        report[str(number)] = [[round(v, 1) for v in b] for b in blocks]
        image = page.render(scale=2).to_pil()
        draw = ImageDraw.Draw(image)
        for b in blocks:
            draw.rectangle([v * 2 for v in b], outline="red", width=3)
        image.save(out / f"p{number}.png")
        text_page.close()
        page.close()
    print(json.dumps(report))


def _code_entry(pdf: Any, number: int, box: list[float]) -> dict[str, Any]:
    page = pdf[number - 1]
    height = page.get_height()
    text_page = page.get_textpage()
    x0, y0, x1, y1 = box
    text = text_page.get_text_bounded(left=x0, bottom=height - y1, right=x1, top=height - y0)
    text_page.close()
    page.close()
    normalised = normalise_code(text)
    return {
        "box": box,
        "lines": len(normalised.split("\n")),
        "sha256": hashlib.sha256(normalised.encode("utf-8")).hexdigest(),
    }


def _outline(pdf: Any) -> dict[str, Any]:
    entries = []
    for item in pdf.get_toc():  # pypdfium2 5.x bookmark API
        raw_title = str(item.get_title() or "")
        title = unicodedata.normalize("NFC", _CONTROLS.sub("", raw_title)).replace("­", "")
        dest = item.get_dest()
        index = dest.get_index() if dest is not None else None
        entries.append((int(item.level) + 1, title, None if index is None else int(index) + 1))
    pairs = json.dumps([[level, page] for level, _, page in entries], separators=(",", ":")).encode()
    titles = json.dumps([t for _, t, _ in entries], ensure_ascii=False, separators=(",", ":")).encode()
    return {
        "source_sha256": PILOT_SHA,
        "entries": len(entries),
        "level1": sum(1 for level, _, _ in entries if level == 1),
        "levels": max((level for level, _, _ in entries), default=0),
        "level_page_sha256": hashlib.sha256(pairs).hexdigest(),
        "titles_sha256": hashlib.sha256(titles).hexdigest(),
    }


def _write(path: Path, value: Any) -> None:
    """JSON is YAML 1.2, so the fixtures need no YAML library in the converter venv."""
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build(pdf_path: str, boxes_path: str) -> None:
    pdf = _open(pdf_path)
    recorded = json.loads(Path(boxes_path).read_text(encoding="utf-8"))
    pages: dict[str, Any] = {}
    for number, want in sorted(recorded["pages"].items(), key=lambda kv: int(kv[0])):
        entry: dict[str, Any] = {}
        if "code_boxes" in want:
            entry["code"] = [_code_entry(pdf, int(number), [float(v) for v in b]) for b in want["code_boxes"]]
        for key in ("formulas_decoded", "tables", "steps", "order"):
            if key in want:
                entry[key] = want[key]
        pages[str(number)] = entry
    expectations = {
        "source_sha256": PILOT_SHA,
        "note": recorded.get("note", ""),
        "known_misses": recorded.get("known_misses", []),
        "pages": pages,
    }
    _write(OUT / "pilot-expectations.yaml", expectations)
    _write(OUT / "pilot-outline.yaml", _outline(pdf))


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "candidates":
        candidates(argv[1], argv[2])
        return 0
    if len(argv) == 3 and argv[0] == "build":
        build(argv[1], argv[2])
        return 0
    sys.stderr.write(__doc__ or "")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
