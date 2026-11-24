# /// script
# requires-python = ">=3.14"
# dependencies = ["pypdfium2==5.15.2", "pyyaml==6.0.3"]
# ///
"""Spike round 6 (never shipped): do the committed bench questions quote the pilot book? For every question of the
given sets, the longest run of consecutive words (normalised: lower case, ё→е, punctuation dropped) that also occurs
as a consecutive run anywhere in the book's text layer. Prints ids and run lengths only, never book or question text.
A record with a run of QUOTE_WORDS or more words counts as quoted and is excluded from the MLflow datasets.

    uv run --script spikes/obs-stack/check_bench_quotes.py <pdf> <set.yaml> [<set.yaml> ...]
"""

import json
import re
import sys

import pypdfium2 as pdfium
import yaml

QUOTE_WORDS = 6
WORD = re.compile(r"[0-9a-zA-Zа-яА-ЯёЁ]+")


def words(text: str) -> list[str]:
    return [w.lower().replace("ё", "е") for w in WORD.findall(text.replace("­", ""))]


def main() -> None:
    doc = pdfium.PdfDocument(sys.argv[1])
    book: list[str] = []
    for i in range(len(doc)):
        book += words(doc[i].get_textpage().get_text_range())
    grams: dict[int, set[tuple[str, ...]]] = {n: {tuple(book[i:i + n]) for i in range(len(book) - n + 1)}
                                              for n in range(2, QUOTE_WORDS + 1)}
    out: dict = {"book_words": len(book), "quote_words": QUOTE_WORDS, "sets": {}}
    for path in sys.argv[2:]:
        rows = []
        for q in yaml.safe_load(open(path))["questions"]:
            w = words(q["question"])
            longest = 1 if w else 0
            for n in range(2, QUOTE_WORDS + 1):
                if any(tuple(w[i:i + n]) in grams[n] for i in range(len(w) - n + 1)):
                    longest = n
            rows.append({"id": q["id"], "words": len(w), "longest_shared_run": longest,
                         "quoted": longest >= QUOTE_WORDS})
        out["sets"][path] = {"n": len(rows), "quoted": [r["id"] for r in rows if r["quoted"]],
                             "max_run": max(r["longest_shared_run"] for r in rows),
                             "runs": {r["id"]: r["longest_shared_run"] for r in rows}}
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
