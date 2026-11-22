"""Generator of VEC-INGEST-2 (spec/modules/SRV-INGEST.yaml): a small synthetic PDF and its expected structure.

    uv run python tests/ingest/make_fixture_pdf.py            # writes spec/fixtures/SRV-INGEST/synthetic.*

The PDF is built with reportlab in invariant mode (fixed IDs and dates), with DejaVu fonts taken from the
matplotlib wheel, so the bytes are the same on every machine with the pinned dev dependencies. The expected
structure is written from the same constants the PDF is drawn from, never from a conversion.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
from pathlib import Path
from typing import Any, Final

REPO: Final = Path(__file__).resolve().parents[3]
OUT: Final = REPO / "spec" / "fixtures" / "SRV-INGEST"

CANARY: Final = "QUOKKAZEBRA"
TITLE: Final = "Синтетический материал"
SECTIONS: Final = ("Введение", "Шаги установки", "Таблица параметров")
PARAGRAPH: Final = (
    f"Это синтетический текст для проверки конвейера. The canary word {CANARY} appears only here.",
    "Строка с мягким переносом: inter­national standard, и ещё немного текста.",
)
STEP_1: Final = "1. Установите пакет и инициализируйте проект."
CODE: Final = (
    "$ pip install example==1.0",
    "$ example --init",
    "# creates config.yaml",
    "$ example run --fast",
)
STEP_2: Final = "2. Запустите проверку и посмотрите отчёт."
TABLE: Final = (
    ("Параметр", "Значение", "Unit"),
    ("timeout", "30", "s"),
    ("retries", "3", "count"),
)
CLOSING: Final = "Последний абзац второй страницы. The table above has three rows and three columns."
HEADER: Final = "Synthetic header line"
FOOTER: Final = "Footer page"


def _fonts() -> dict[str, str]:
    import matplotlib
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    ttf = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
    names = {"regular": "DejaVuSans", "bold": "DejaVuSans-Bold", "mono": "DejaVuSansMono"}
    for name in names.values():
        pdfmetrics.registerFont(TTFont(name, str(ttf / f"{name}.ttf")))
    return names


def build() -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen.canvas import Canvas

    fonts = _fonts()
    buffer = io.BytesIO()
    canvas = Canvas(buffer, pagesize=A4, invariant=1)
    width, height = A4

    def furniture(page: int) -> None:
        canvas.setFont(fonts["regular"], 8)
        canvas.drawString(72, height - 36, HEADER)
        canvas.drawRightString(width - 72, 30, f"{FOOTER} {page}")

    def heading(y: float, text: str, size: int, key: str | None) -> float:
        if key is not None:
            canvas.bookmarkPage(key)
            canvas.addOutlineEntry(text, key, level=0)
        canvas.setFont(fonts["bold"], size)
        canvas.drawString(72, y, text)
        return y - size * 2.2

    def lines(y: float, texts: tuple[str, ...], font: str, size: int, x: float = 72) -> float:
        canvas.setFont(fonts[font], size)
        for text in texts:
            canvas.drawString(x, y, text)
            y -= size * 1.5
        return y - size

    # Page 1: title, two sections, a numbered list with a code block between steps 1 and 2.
    furniture(1)
    y = heading(height - 90, TITLE, 20, None)
    y = heading(y, SECTIONS[0], 14, "s1")
    y = lines(y, PARAGRAPH, "regular", 11)
    y = heading(y - 10, SECTIONS[1], 14, "s2")
    y = lines(y, (STEP_1,), "regular", 11)
    y = lines(y, CODE, "mono", 10, x=96)
    lines(y, (STEP_2,), "regular", 11)
    canvas.showPage()

    # Page 2: a third section with a 3x3 ruled table and a closing paragraph.
    furniture(2)
    y = heading(height - 90, SECTIONS[2], 14, "s3")
    cell_w, cell_h, left = 140.0, 22.0, 72.0
    top = y
    canvas.setFont(fonts["regular"], 11)
    for r, row in enumerate(TABLE):
        for c, cell in enumerate(row):
            canvas.drawString(left + c * cell_w + 6, top - (r + 1) * cell_h + 7, cell)
    for r in range(len(TABLE) + 1):
        canvas.line(left, top - r * cell_h, left + 3 * cell_w, top - r * cell_h)
    for c in range(4):
        canvas.line(left + c * cell_w, top, left + c * cell_w, top - len(TABLE) * cell_h)
    lines(top - len(TABLE) * cell_h - 30, (CLOSING,), "regular", 11)
    canvas.showPage()
    canvas.save()
    return buffer.getvalue()


def expected(pdf: bytes) -> dict[str, Any]:
    return {
        "pdf_sha256": hashlib.sha256(pdf).hexdigest(),
        "page_count": 2,
        "canary": CANARY,
        "headings": [TITLE, *SECTIONS],
        "heading_levels_at_most": 2,
        "code_lines": list(CODE),
        "code_between": [STEP_1[:2], STEP_2[:2]],
        "table_shape": [len(TABLE), len(TABLE[0])],
        "outline": [
            {"level": 1, "title": SECTIONS[0], "page": 1},
            {"level": 1, "title": SECTIONS[1], "page": 1},
            {"level": 1, "title": SECTIONS[2], "page": 2},
        ],
        "outline_source": "bookmarks",
        "furniture_dropped": [HEADER, FOOTER],
    }


def main() -> int:
    pdf = build()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "synthetic.pdf").write_bytes(pdf)
    (OUT / "synthetic.expected.json").write_text(
        json.dumps(expected(pdf), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
