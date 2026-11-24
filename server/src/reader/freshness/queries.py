"""The query plan (FR-FRESH-5, VEC-FRESH-7): fixed, ordered templates as data, filled only from bibliography
fields, topic titles, one claim subject and the template's own words (the material's language plus English).

`plan` returns at most `searches_max` queries:
  (1) original_edition: only with an original title;
  (2) edition;
  (3 to 5) claim: the three kept claims of highest confidence, then lowest page; missing ones become
        topic_docs queries over the level-1 topics in topic order;
  (6) books: the first level-1 topic, "book" and the current year.
A claim whose subject fails the scan's own subject rule (`subject_ok`: 1 to `subject_words_max` words as
`text.words` and `text.split` count them over the non-front spans' vocabulary, each token at most
`subject_token_chars_max` characters, at most `subject_chars_max` characters in all) is skipped
(`subject_too_long`), so a stored or reused claim is held to the same rule as a fresh scan's. A claim whose
subject, with an earlier chosen claim's subject before or after it, forms a `shingle_words`-word run of a
non-front span is skipped too (`subject_split`), so no two queries of one check carry a span's run between
them.
Each query is NFC, whitespace-collapsed and cut to `query_chars_max` by removing words from the end of its
longest variable part. A query is dropped (`query_unsafe`) when, with the bibliography strings, author
surnames and topic titles masked, it still holds a `shingle_words`-word run of a non-front sampled span
(INV-FRESH-1 b). Masking removes the exempt words from both word sequences, the query's and each span's,
before the runs are compared; so an exempt word inside a copied run cannot hide the run. A query equal to an
earlier one is dropped (`query_duplicate`).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final

from reader.freshness import params
from reader.freshness.sample import Topic
from reader.freshness.signals import language
from reader.freshness.text import clean, expanded, vocabulary, words

WORDS: Final[Mapping[str, Mapping[str, str]]] = {
    "en": {
        "new_edition_after": "new edition after",
        "new_edition": "new edition",
        "book": "book",
        "latest_version": "latest version",
        "current_specification": "current specification",
        "official_documentation": "official documentation",
    },
    "ru": {
        "new_edition_after": "новое издание после",
        "new_edition": "новое издание",
        "book": "книга",
        "latest_version": "последняя версия",
        "current_specification": "актуальная спецификация",
        "official_documentation": "официальная документация",
    },
}
KIND_WORDS: Final[Mapping[str, str]] = {
    "version": "latest_version",
    "api": "latest_version",
    "tool": "latest_version",
    "protocol": "current_specification",
    "standard": "current_specification",
    "law": "current_specification",
    "statistic": "official_documentation",
    "other": "official_documentation",
}
CONFIDENCE_RANK: Final[Mapping[str, int]] = {"high": 0, "medium": 1, "low": 2}
CLAIM_SLOTS: Final = 3

# A part is ("var", name) — filled from the context — or ("word", key) — the template's own words.
Part = tuple[str, str]
TEMPLATES: Final[Mapping[str, tuple[Part, ...]]] = {
    "original_edition": (
        ("var", "original_title"),
        ("var", "original_surname"),
        ("word", "new_edition_after"),
        ("var", "original_edition"),
        ("var", "original_year"),
    ),
    "edition": (
        ("var", "title"),
        ("var", "surname"),
        ("var", "publisher"),
        ("word", "new_edition"),
        ("var", "year"),
    ),
    "claim": (("var", "subject"), ("word", "kind")),
    "topic_docs": (("var", "topic"), ("word", "official_documentation")),
    "books": (("var", "topic"), ("word", "book"), ("var", "current_year")),
}


@dataclass(frozen=True)
class Query:
    template: str
    text: str


@dataclass(frozen=True)
class Plan:
    queries: tuple[Query, ...]
    dropped: Mapping[str, int] = field(default_factory=dict)


def template_words(lang: str, key: str) -> str:
    base = language(lang)
    english = WORDS["en"][key]
    own = WORDS.get(base, {}).get(key) if base != "en" else None
    return f"{own} {english}" if own else english


def surname(name: str | None) -> str | None:
    """`Surname, Given` → Surname; `Given Surname` → Surname (the last word)."""
    if not name:
        return None
    if "," in name:
        return name.split(",", 1)[0].strip() or None
    parts = name.split()
    return parts[-1] if parts else None


def _fill(parts: tuple[Part, ...], context: Mapping[str, str | None], lang: str) -> list[tuple[str, bool]]:
    filled: list[tuple[str, bool]] = []
    for kind, name in parts:
        if kind == "word":
            key = KIND_WORDS[str(context["kind"])] if name == "kind" else name
            filled.append((template_words(lang, key), False))
        else:
            value = clean(context.get(name) or "")
            if value:
                filled.append((value, True))
    return filled


def _cut(filled: list[tuple[str, bool]]) -> str:
    """Words are removed from the end of the longest variable part until the query fits."""
    parts = [(text.split(" "), var) for text, var in filled]

    def joined() -> str:
        return " ".join(" ".join(ws) for ws, _ in parts if ws)

    while len(joined()) > params.QUERY_CHARS_MAX:
        candidates = [i for i, (ws, var) in enumerate(parts) if var and ws]
        if not candidates:
            return joined()[: params.QUERY_CHARS_MAX].rstrip()
        longest = max(candidates, key=lambda i: (len(" ".join(parts[i][0])), -i))
        parts[longest][0].pop()
    return joined()


def build_query(template: str, context: Mapping[str, str | None], lang: str) -> str:
    text = _cut(_fill(TEMPLATES[template], context, lang))
    return clean(unicodedata.normalize("NFC", text))


def _exempt(bibliography: Mapping[str, Any], topics: Sequence[Topic]) -> list[list[str]]:
    strings: list[str] = []
    for key in ("title", "edition", "publisher", "isbn", "original_title", "original_edition"):
        if bibliography.get(key):
            strings.append(str(bibliography[key]))
    for key in ("year", "original_year"):
        if bibliography.get(key) is not None:
            strings.append(str(bibliography[key]))
    for key in ("authors", "original_authors"):
        for name in bibliography.get(key) or []:
            strings.append(name)
            last = surname(name)
            if last:
                strings.append(last)
    strings.extend(t.title for t in topics)
    return [w for w in (words(s) for s in strings) if w]


def _masked(tokens: list[str], exempt: list[list[str]]) -> list[bool]:
    mask = [False] * len(tokens)
    for seq in exempt:
        n = len(seq)
        for i in range(len(tokens) - n + 1):
            if tokens[i : i + n] == seq:
                for j in range(i, i + n):
                    mask[j] = True
    return mask


def _unmasked(value: str, exempt: list[list[str]], vocab: frozenset[str] = frozenset()) -> list[str]:
    """The word tokens of `value` (concatenations of `vocab` words cut into them) with every exempt sequence
    removed (INV-FRESH-1 b's masking)."""
    tokens = expanded(value, vocab)
    mask = _masked(tokens, exempt)
    return [token for token, masked in zip(tokens, mask, strict=True) if not masked]


def shingles(
    texts: Sequence[str], n: int = params.SHINGLE_WORDS, exempt: list[list[str]] | None = None
) -> set[tuple[str, ...]]:
    """Every `n`-word run of the texts, after the exempt sequences are removed (none by default)."""
    found: set[tuple[str, ...]] = set()
    for value in texts:
        tokens = _unmasked(value, exempt or [])
        for i in range(len(tokens) - n + 1):
            found.add(tuple(tokens[i : i + n]))
    return found


def _holds_run(tokens: list[str], book: set[tuple[str, ...]]) -> bool:
    n = params.SHINGLE_WORDS
    return any(tuple(tokens[i : i + n]) in book for i in range(len(tokens) - n + 1))


def unsafe(
    query: str, exempt: list[list[str]], book: set[tuple[str, ...]], vocab: frozenset[str] = frozenset()
) -> bool:
    """True when a `shingle_words`-word run of the query, its exempt sequences removed, is a run of a
    non-front span with the same sequences removed (`book` = `shingles(non_front, exempt=exempt)`); a query
    token that concatenates span words (`vocab`) counts as those words."""
    return _holds_run(_unmasked(query, exempt, vocab), book)


def subject_words(subject: str, vocab: frozenset[str] = frozenset()) -> list[str]:
    """The subject's words as FR-FRESH-3 counts them: `words` after `clean`, each token that concatenates
    words of the non-front spans (`vocab`) counted as those words."""
    return expanded(clean(subject), vocab)


def subject_ok(subject: str, vocab: frozenset[str] = frozenset()) -> bool:
    """FR-FRESH-3's subject rule, shared by the scan and the plan: after `clean`, 1 to `subject_words_max`
    words (`subject_words`), no token over `subject_token_chars_max` characters, and at most
    `subject_chars_max` characters."""
    cleaned = clean(subject)
    return (
        0 < len(subject_words(cleaned, vocab)) <= params.SUBJECT_WORDS_MAX
        and all(len(t) <= params.SUBJECT_TOKEN_CHARS_MAX for t in words(cleaned))
        and len(cleaned) <= params.SUBJECT_CHARS_MAX
    )


def _rank(item: tuple[int, Mapping[str, Any]]) -> tuple[int, int, int]:
    index, claim = item
    return CONFIDENCE_RANK.get(str(claim.get("confidence")), 3), int(claim.get("page") or 0), index


def _claims(
    claims: Sequence[Mapping[str, Any]],
    exempt: list[list[str]],
    book: set[tuple[str, ...]],
    vocab: frozenset[str],
) -> tuple[list[Mapping[str, Any]], int]:
    """The `CLAIM_SLOTS` claims of highest rank, skipping one whose subject forms a span run with an earlier
    chosen subject (in either order); returns them and the number skipped."""
    chosen: list[Mapping[str, Any]] = []
    tokens: list[list[str]] = []
    skipped = 0
    for _, claim in sorted(enumerate(claims), key=_rank):
        if len(chosen) == CLAIM_SLOTS:
            break
        mine = _unmasked(str(claim.get("subject") or ""), exempt, vocab)
        if any(_holds_run(a + b, book) for other in tokens for a, b in ((other, mine), (mine, other))):
            skipped += 1
            continue
        chosen.append(claim)
        tokens.append(mine)
    return chosen, skipped


def plan(
    bibliography: Mapping[str, Any],
    topics: Sequence[Topic],
    claims: Sequence[Mapping[str, Any]],
    lang: str,
    today: date,
    non_front_texts: Sequence[str],
) -> Plan:
    authors = bibliography.get("authors") or []
    originals = bibliography.get("original_authors") or []
    level1 = [t.title for t in topics if t.level == 1]
    base: dict[str, str | None] = {
        "title": bibliography.get("title"),
        "surname": surname(authors[0]) if authors else None,
        "publisher": bibliography.get("publisher"),
        "year": str(bibliography["year"]) if bibliography.get("year") is not None else None,
        "original_title": bibliography.get("original_title"),
        "original_surname": surname(originals[0]) if originals else None,
        "original_edition": bibliography.get("original_edition"),
        "original_year": (
            str(bibliography["original_year"]) if bibliography.get("original_year") is not None else None
        ),
        "current_year": str(today.year),
    }
    planned: list[Query] = []
    if bibliography.get("original_title"):
        planned.append(Query("original_edition", build_query("original_edition", base, lang)))
    planned.append(Query("edition", build_query("edition", base, lang)))
    dropped: dict[str, int] = {}
    vocab = vocabulary(non_front_texts)
    exempt = _exempt(bibliography, topics)
    book = shingles(non_front_texts, exempt=exempt)
    eligible = [c for c in claims if subject_ok(str(c.get("subject") or ""), vocab)]
    if len(eligible) < len(claims):
        dropped["subject_too_long"] = len(claims) - len(eligible)
    chosen, skipped = _claims(eligible, exempt, book, vocab)
    if skipped:
        dropped["subject_split"] = skipped
    for claim in chosen:
        subject, kind = str(claim.get("subject") or ""), str(claim.get("kind") or "other")
        context = {**base, "subject": subject, "kind": kind}
        planned.append(Query("claim", build_query("claim", context, lang)))
    for topic in level1[: CLAIM_SLOTS - len(chosen)]:
        planned.append(Query("topic_docs", build_query("topic_docs", {**base, "topic": topic}, lang)))
    if level1:
        planned.append(Query("books", build_query("books", {**base, "topic": level1[0]}, lang)))
    kept: list[Query] = []
    seen: set[str] = set()
    for query in planned:
        reason = None
        if not query.text:
            reason = "query_empty"
        elif query.text in seen:
            reason = "query_duplicate"
        elif unsafe(query.text, exempt, book, vocab):
            reason = "query_unsafe"
        if reason is not None:
            dropped[reason] = dropped.get(reason, 0) + 1
            continue
        seen.add(query.text)
        kept.append(query)
    return Plan(tuple(kept[: params.SEARCHES_MAX]), dropped)
