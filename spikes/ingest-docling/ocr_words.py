# /// script
# requires-python = ">=3.14"
# dependencies = ["pypdfium2==5.15.2"]
# ///
"""Generator of results/ocr-words.json: word-level recall and precision of the OCR output (data/…/ocr/pNNNN.txt)
against the PDF's text layer, as multiset overlap of lowercased \\w+ tokens.

    uv run --script spikes/ingest-docling/ocr_words.py <pdf> data/spikes/ingest-docling/ocr > spikes/ingest-docling/results/ocr-words.json
"""

import json
import re
import sys
from collections import Counter

import pypdfium2 as pdfium

PAGES = (30, 95, 170, 260, 320, 420, 520, 600, 700, 850)


def words(text: str) -> Counter[str]:
    return Counter(re.findall(r"\w+", text.lower()))


doc = pdfium.PdfDocument(sys.argv[1])
rows = []
for n in PAGES:
    truth = words(doc[n - 1].get_textpage().get_text_range())
    ocr = words(open(f"{sys.argv[2]}/p{n:04d}.txt", encoding="utf-8").read())
    hit = sum((truth & ocr).values())
    rows.append({"page": n, "words_text_layer": sum(truth.values()), "words_ocr": sum(ocr.values()),
                 "word_recall": round(hit / sum(truth.values()), 3),
                 "word_precision": round(hit / max(1, sum(ocr.values())), 3)})
print(json.dumps({"metric": "multiset word overlap, lowercased \\w+ tokens, OCR text vs PDF text layer", "pages": rows}))
