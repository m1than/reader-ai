# /// script
# requires-python = ">=3.14"
# dependencies = ["pypdfium2==5.15.2", "pyyaml==6.0.3"]
# ///
"""Spike rounds 6 and 8 (never shipped): do the committed bench questions quote the pilot book? A verbatim check
only: for every question of the given sets, the longest run of consecutive words that also occurs as a consecutive
run anywhere in the book's text layer. A paraphrase that keeps the meaning is not detected (SRV-OBS FR-OBS-22).
Normalisation, on both sides: Unicode NFC; pdfium's U+FFFE soft-hyphen marker, a soft hyphen (U+00AD) and a
hyphen at a line end are joined (the two halves of a word split across lines become one word); lower case; ё→е;
punctuation dropped. A record whose longest shared run is QUOTE_WORDS or more words is excluded from the MLflow
datasets. Prints ids, lengths and sha256s only, never book or question text.

    uv run --script spikes/obs-stack/check_bench_quotes.py <pdf> <set.yaml> [<set.yaml> ...]
"""

import hashlib
import json
import re
import sys
import unicodedata

import pypdfium2 as pdfium
import yaml

QUOTE_WORDS = 5  # design review round 1, B6: 5 or more consecutive shared words count as quoted
WORD = re.compile(r"[0-9a-zA-Zа-яА-ЯёЁ]+")
JOIN = re.compile(r"[￾­]\s*|(?<=\w)-[ \t]*\r?\n\s*")  # soft-hyphen markers and line-end hyphens


def words(text: str) -> list[str]:
    text = JOIN.sub("", unicodedata.normalize("NFC", text))
    return [w.lower().replace("ё", "е") for w in WORD.findall(text)]


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main() -> None:
    pdf = sys.argv[1]
    doc = pdfium.PdfDocument(pdf)
    book: list[str] = []
    for i in range(len(doc)):
        book += words(doc[i].get_textpage().get_text_range())
    grams: dict[int, set[tuple[str, ...]]] = {n: {tuple(book[i:i + n]) for i in range(len(book) - n + 1)}
                                              for n in range(2, QUOTE_WORDS + 1)}
    out: dict = {"pdf_sha256": sha256(pdf), "book_words": len(book), "quote_words": QUOTE_WORDS,
                 "normalisation": "NFC; U+FFFE, U+00AD and line-end hyphens joined; lower; ё→е; words only",
                 "sets": {}}
    for path in sys.argv[2:]:
        rows = []
        for q in yaml.safe_load(open(path, encoding="utf-8"))["questions"]:
            w = words(q["question"])
            longest = 1 if w else 0
            for n in range(2, QUOTE_WORDS + 1):
                if any(tuple(w[i:i + n]) in grams[n] for i in range(len(w) - n + 1)):
                    longest = n
            rows.append({"id": q["id"], "longest_shared_run": longest, "quoted": longest >= QUOTE_WORDS})
        out["sets"][path] = {"sha256": sha256(path), "n": len(rows),
                             "quoted": [r["id"] for r in rows if r["quoted"]],
                             "max_run": max(r["longest_shared_run"] for r in rows),
                             "runs": {r["id"]: r["longest_shared_run"] for r in rows}}
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
