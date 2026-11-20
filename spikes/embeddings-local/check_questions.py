# /// script
# requires-python = ">=3.14"
# dependencies = ["pypdfium2==5.15.2", "pyyaml==6.0.3"]
# ///
"""Lint a question set against the pilot PDF: every gold page exists and lies in the stated chapter, and the
question shares no word stem with (a) the outline headings whose sections cover a gold page or (b) bold words
on the gold pages (pdfium font weight >= 600). Prints the shared stems only, never page text.

    uv run --script spikes/embeddings-local/check_questions.py <pdf> spikes/embeddings-local/questions-v2.yaml
"""

import ctypes
import re
import sys

import pypdfium2 as pdfium
import pypdfium2.raw as raw
import yaml

STEM = 5                       # crude Russian/English stem: the first 5 letters of a word of 4+ letters
ALLOW = {"блокч"}              # the book's subject word appears in nearly every heading; allowed
WORD = re.compile(r"[a-zA-Zа-яА-ЯёЁ]{4,}")


def stems(text: str) -> set[str]:
    return {w.lower().replace("ё", "е")[:STEM] for w in WORD.findall(text)} - ALLOW


def outline(doc: pdfium.PdfDocument) -> list[tuple[int, int, str]]:
    """(level, first page, title) in document order."""
    out = []
    for b in doc.get_toc():
        d = b.get_dest()
        if d is not None:
            out.append((b.level, d.get_index() + 1, b.get_title() or ""))
    return out


def covering_titles(toc: list[tuple[int, int, str]], page: int, last_page: int) -> list[str]:
    titles = []
    for i, (level, start, title) in enumerate(toc):
        end = next((s - 1 for lv, s, _ in toc[i + 1:] if lv <= level), last_page)
        if start <= page <= max(start, end):
            titles.append(title)
    return titles


def bold_words(doc: pdfium.PdfDocument, page: int) -> str:
    tp = doc[page - 1].get_textpage()
    n = raw.FPDFText_CountChars(tp.raw)
    chars = []
    for i in range(n):
        if raw.FPDFText_GetFontWeight(tp.raw, i) >= 600:
            chars.append(chr(raw.FPDFText_GetUnicode(tp.raw, i)))
        else:
            chars.append(" ")
    return "".join(chars)


def chapter_ranges(toc: list[tuple[int, int, str]], last_page: int) -> dict[int, tuple[int, int]]:
    chapters = [(int(m.group(1)), s) for lv, s, t in toc if lv == 0 and (m := re.match(r"Глава (\d+)", t))]
    starts = [s for lv, s, _ in toc if lv == 0]
    return {n: (s, next((x - 1 for x in starts if x > s), last_page)) for n, s in chapters}


def main() -> None:
    doc = pdfium.PdfDocument(sys.argv[1])
    qs = yaml.safe_load(open(sys.argv[2], encoding="utf-8"))["questions"]
    toc, last = outline(doc), len(doc)
    chapters = chapter_ranges(toc, last)
    problems = 0
    for q in qs:
        lo, hi = chapters[q["chapter"]]
        bad_pages = [p for p in q["gold_pages"] if not lo <= p <= hi]
        heading = set().union(*(stems(t) for p in q["gold_pages"] for t in covering_titles(toc, p, last)))
        bold = set().union(*(stems(bold_words(doc, p)) for p in q["gold_pages"]))
        shared_h, shared_b = sorted(stems(q["question"]) & heading), sorted(stems(q["question"]) & bold - heading)
        if bad_pages or shared_h or shared_b:
            problems += 1
        print(f"{q['id']} ch{q['chapter']:>2} pages={q['gold_pages']} out_of_chapter={bad_pages or '-'} "
              f"heading_stems={shared_h or '-'} bold_stems={shared_b or '-'}")
    per_chapter = {c: sum(q["chapter"] == c for q in qs) for c in sorted(chapters)}
    print(f"questions={len(qs)} with_issues={problems} per_chapter={per_chapter}")
    print(f"multi_page={sum(len(q['gold_pages']) > 1 for q in qs)} why_how={sum(q.get('kind') == 'why_how' for q in qs)}")


main()
