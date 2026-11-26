# /// script
# requires-python = ">=3.14"
# dependencies = ["pypdfium2==5.15.2", "pyyaml==6.0.3"]
# ///
"""Check the typed evaluation set (ADR-0039 B9) against its rules and the pilot PDF read locally.

Rules checked: the record schema; 30 records per type (exact, thematic, fact-check); unique ids; reused records
(origin v1, v2, t01) carry their source's id and byte-identical question; new ids are not taken by a source;
`claim_holds` on fact-check records only; 1-4 expected facts per record, each at most 300 characters; gold page
ranges inside the stated chapter; `topic` from the closed list, depth at most 3; the PDF's sha256 equals
`source_sha256`; and the verbatim check: no question and no expected fact shares a run of QUOTE_WORDS or more
consecutive words with the book's text layer (normalisation as spikes/obs-stack/check_bench_quotes.py).
The book text is read at check time and never stored. Prints ids and numbers only, never book or set text.

    uv run --script spec/fixtures/eval/check_eval_set.py <pdf> [spec/fixtures/eval/pilot-eval-v1.yaml]

Exit status 0 when every rule holds, 1 otherwise.
"""

import hashlib
import re
import sys
import unicodedata
from pathlib import Path

import pypdfium2 as pdfium
import yaml

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SET = ROOT / "spec/fixtures/eval/pilot-eval-v1.yaml"
SOURCES = {  # origin -> owner-accepted file whose records may be reused with their ids
    "v1": ROOT / "spikes/embeddings-local/questions.yaml",
    "v2": ROOT / "spikes/embeddings-local/questions-v2.yaml",
    "t01": ROOT / "spec/fixtures/SRV-TUTOR/bench-fixed.yaml",
}
TYPES = ("exact", "thematic", "fact-check")
PER_TYPE = 30
FACT_CHARS = 300
FACTS_PER_RECORD = (1, 4)
QUOTE_WORDS = 8  # ADR-0039 B9: no 8-word sequence shared with the book (initial value)
TOPIC_DEPTH = 3
REQUIRED = {"id", "type", "origin", "chapter", "topic", "question", "gold_pages", "expected_facts"}
OPTIONAL = {"claim_holds", "note"}
TOP_KEYS = {"status", "source_sha256", "language", "topics", "questions"}
RANGE = re.compile(r"^(\d+)(?:-(\d+))?$")
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


def chapter_ranges(doc: pdfium.PdfDocument) -> dict[int, tuple[int, int]]:
    toc = [(b.level, d.get_index() + 1, b.get_title() or "") for b in doc.get_toc() if (d := b.get_dest())]
    starts = sorted(s for lv, s, _ in toc if lv == 0)
    out = {}
    for lv, s, t in toc:
        if lv == 0 and (m := re.match(r"Глава (\d+)", t)):
            out[int(m.group(1))] = (s, next((x - 1 for x in starts if x > s), len(doc)))
    return out


def pages_of(ranges: list[str]) -> list[tuple[int, int]] | None:
    out = []
    for r in ranges:
        m = RANGE.match(str(r))
        if not m:
            return None
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        if lo > hi:
            return None
        out.append((lo, hi))
    return out


def longest_run(w: list[str], grams: dict[int, set[int]]) -> int:
    best = 1 if w else 0
    for n in range(2, QUOTE_WORDS + 1):
        if any(hash(tuple(w[i:i + n])) in grams[n] for i in range(len(w) - n + 1)):
            best = n
        else:
            break  # a shared run of n words contains shared runs of every shorter length
    return best


def main() -> int:
    if len(sys.argv) < 2 or not Path(sys.argv[1]).is_file():
        print("usage: check_eval_set.py <pilot pdf> [set.yaml]  (the PDF is required and is never committed)")
        return 2
    pdf = sys.argv[1]
    set_path = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_SET
    data = yaml.safe_load(set_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        print("ISSUE file: the set is not a mapping")
        return 1
    problems: list[str] = []

    def bad(rid: str, what: str) -> None:
        problems.append(f"{rid}: {what}")

    if set(data) != TOP_KEYS:
        bad("file", f"top-level keys {sorted(set(data) ^ TOP_KEYS)} differ")
    if sha256(pdf) != data.get("source_sha256"):
        print("ISSUE file: the PDF's sha256 differs from source_sha256; nothing else is checked")
        return 1
    topics = set(data.get("topics") or [])
    for t in topics:
        if len(t.split("/")) > TOPIC_DEPTH:
            bad("topics", f"{t.count('/') + 1} levels")

    sources = {o: {q["id"]: q for q in yaml.safe_load(p.read_text(encoding="utf-8"))["questions"]}
               for o, p in SOURCES.items()}
    source_ids = set().union(*sources.values())
    doc = pdfium.PdfDocument(pdf)
    chapters = chapter_ranges(doc)

    records = data.get("questions") or []
    ids = [r.get("id") for r in records]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        bad(str(dup), "duplicate id")
    counts = {t: sum(r.get("type") == t for r in records) for t in TYPES}
    for t, n in counts.items():
        if n != PER_TYPE:
            bad("file", f"{n} records of type {t}, expected {PER_TYPE}")

    for r in records:
        rid = str(r.get("id"))
        keys = set(r)
        if not REQUIRED <= keys or keys - REQUIRED - OPTIONAL:
            bad(rid, f"keys missing {sorted(REQUIRED - keys)} or unknown {sorted(keys - REQUIRED - OPTIONAL)}")
            continue
        if r["type"] not in TYPES:
            bad(rid, "unknown type")
        if ("claim_holds" in r) != (r["type"] == "fact-check") or (
                "claim_holds" in r and not isinstance(r["claim_holds"], bool)):
            bad(rid, "claim_holds must be a boolean on fact-check records and absent elsewhere")
        origin = r["origin"]
        if origin in sources:
            src = sources[origin].get(rid)
            if src is None:
                bad(rid, f"not an id of origin {origin}")
            elif src["question"] != r["question"] or src["chapter"] != r["chapter"]:
                bad(rid, f"question or chapter differs from origin {origin}")
        elif origin == "new":
            if rid in source_ids:
                bad(rid, "a new record reuses a source id")
        else:
            bad(rid, "origin must be v1, v2, t01 or new")
        if r["topic"] not in topics:
            bad(rid, "topic not in the closed list")
        if not isinstance(r["question"], str) or not r["question"].strip():
            bad(rid, "empty question")
        facts = r["expected_facts"]
        if not isinstance(facts, list) or not FACTS_PER_RECORD[0] <= len(facts) <= FACTS_PER_RECORD[1]:
            bad(rid, f"expected_facts must be a list of {FACTS_PER_RECORD[0]}-{FACTS_PER_RECORD[1]}")
            facts = []
        for k, f in enumerate(facts):
            if not isinstance(f, str) or not f.strip() or len(f) > FACT_CHARS:
                bad(rid, f"fact {k + 1}: {len(f) if isinstance(f, str) else 'not a string'} characters")
        spans = pages_of(r["gold_pages"]) if isinstance(r["gold_pages"], list) and r["gold_pages"] else None
        lo_hi = chapters.get(r["chapter"])
        if spans is None:
            bad(rid, "gold_pages must be a non-empty list of 'N' or 'N-M'")
        elif lo_hi is None:
            bad(rid, "unknown chapter")
        elif any(not lo_hi[0] <= a <= b <= lo_hi[1] for a, b in spans):
            bad(rid, f"gold pages outside chapter {r['chapter']} ({lo_hi[0]}-{lo_hi[1]})")

    book: list[str] = []
    for i in range(len(doc)):
        book += words(doc[i].get_textpage().get_text_range())
    grams = {n: {hash(tuple(book[i:i + n])) for i in range(len(book) - n + 1)} for n in range(2, QUOTE_WORDS + 1)}
    del book
    max_q, max_f = 0, 0
    for r in records:
        rid = str(r.get("id"))
        q = longest_run(words(str(r.get("question", ""))), grams)
        max_q = max(max_q, q)
        if q >= QUOTE_WORDS:
            bad(rid, f"question shares a {q}-word run with the book")
        for k, f in enumerate(r.get("expected_facts") or []):
            n = longest_run(words(str(f)), grams)
            max_f = max(max_f, n)
            if n >= QUOTE_WORDS:
                bad(rid, f"fact {k + 1} shares a {n}-word run with the book")
        if r.get("note"):
            n = longest_run(words(str(r["note"])), grams)
            if n >= QUOTE_WORDS:
                bad(rid, f"note shares a {n}-word run with the book")

    longest_fact = max((len(f) for r in records for f in r.get("expected_facts") or [] if isinstance(f, str)),
                       default=0)
    print(f"records={len(records)} per_type={counts} unique_ids={len(set(ids)) == len(ids)}")
    print(f"fact_check_claims_true={sum(r.get('claim_holds') is True for r in records)} "
          f"false={sum(r.get('claim_holds') is False for r in records)}")
    print(f"origins={ {o: sum(r.get('origin') == o for r in records) for o in (*SOURCES, 'new')} }")
    print(f"longest_fact_chars={longest_fact} max_shared_run_question={max_q} max_shared_run_fact={max_f} "
          f"(limit: below {QUOTE_WORDS} words)")
    for p in problems:
        print("ISSUE", p)
    print(f"issues={len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
