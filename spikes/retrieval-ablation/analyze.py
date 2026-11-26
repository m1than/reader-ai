"""Spike retrieval-ablation, stage 3 (stdlib + PyYAML; runs in the reader-embed image, --network none): recall@10
per question type for the free arms, exact McNemar between neighbouring variants (ADR-0039 B10), and the cost
projection of the paid arms from the real span and outline sizes.

    python analyze.py --work /work --spans /in/spans.jsonl --outline /in/outline.json \
                      --pricing /in/pricing.yaml --out /out

Reads stage 1 and 2 outputs from /work. Writes /out/*.json with ids, counts and numbers only: the book's text and
its outline titles are read in memory to measure lengths and to find chapter starts, and never written.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import yaml

K = 10
TYPES = ("exact", "thematic", "fact-check")

# ---------------------------------------------------------------- the form facet, declared before any result
# ADR-0039 B3: `form` is derived by rule from the span kind; test_vector is a rule over code spans. A heading is
# structure (L2), not a card form, so it has no form value and no route that filters on form returns it.
FORM_OF_KIND = {"text": "prose", "list_item": "prose", "formula": "formula", "code": "code",
                "table": "table", "figure": "figure", "heading": None}
HEX = re.compile(r"[0-9a-fA-F]")
# ADR-0039 B6 route table: exact and fact-check read L1 card forms; thematic has "None on form".
CARD_FORMS = {"prose", "code", "test_vector", "formula", "table"}
ROUTE_FORMS: dict[str, set[str] | None] = {"exact": CARD_FORMS, "thematic": None, "fact-check": CARD_FORMS}

RRF_K = 60  # search/fusion.py RRF_K
W_DENSE = {"w1": 1.0, "w5": 5.0}  # w5 is today's params.W_DENSE (primary); w1 the equal-weight RRF of ADR-0007
CANDIDATES = 50  # params.CANDIDATES

# ---------------------------------------------------------------- cost model inputs, each with its source
TOKENS_PER_CHAR = Decimal(23900) / Decimal(60034)  # spikes/compile-cost: L02 source 60,034 chars ≈ 23,900 tokens
CLI_OVERHEAD = 410       # spikes/tutor-call: fixed tokens per CLI call beyond our own bytes (custom system prompt)
PREFIX_INSTR = 120       # assumption: Contextual Retrieval's prompt template (Anthropic 2024) ≈ 60 words + tags
PREFIX_OUT = (50, 100)   # ADR-0039 B1: prefix of about 50–100 tokens
SUMMARY_INSTR = 150      # assumption: one short instruction
SUMMARY_OUT = {"leaf": 100, "inner": 300, "chapter": 300, "root": 500}  # ADR-0039 B1 card budgets (inner: chapter's)
JUDGE_INSTR = 600        # assumption: rubric + expected facts framing
JUDGE_OUT = 200          # assumption: verdict, missing facts, one line each
HAIKU_S = (Decimal("1.82"), Decimal("2.10"))  # spikes/agent-sdk-container, per call including the CLI spawn
MICRO = Decimal("0.000001")


def micro(v: Decimal) -> Decimal:  # server/src/reader/agent/pricing.py micro
    return v.quantize(MICRO, rounding=ROUND_HALF_UP)


def mcnemar(b: int, c: int) -> float:
    """Exact two-sided McNemar: binomial(b + c, 1/2) on the discordant pairs."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def rrf(dense: list[int], fts: list[int], w: float) -> list[int]:  # search/fusion.py rrf, same tie rule
    dr = {x: i + 1 for i, x in enumerate(dense)}
    fr = {x: i + 1 for i, x in enumerate(fts)}
    order = {x: i for i, x in enumerate([*dense, *(x for x in fts if x not in dr)])}
    worst = len(dense) + 1
    score = {x: (w / (RRF_K + dr[x]) if x in dr else 0.0) + (1 / (RRF_K + fr[x]) if x in fr else 0.0) for x in order}
    return sorted(order, key=lambda x: (-score[x], dr.get(x, worst), order[x]))


def main() -> None:
    ap = argparse.ArgumentParser()
    for name in ("work", "spans", "outline", "pricing", "out"):
        ap.add_argument(f"--{name}", required=True)
    a = ap.parse_args()
    work, out = Path(a.work), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    meta = json.loads((work / "meta.json").read_text())
    dense = json.loads((work / "dense.json").read_text())
    fts = json.loads((work / "fts.json").read_text())
    latency = json.loads((work / "latency.json").read_text())
    spans = [json.loads(line) for line in open(a.spans, encoding="utf-8")]
    outline = json.loads(Path(a.outline).read_text(encoding="utf-8"))
    prices = yaml.safe_load(open(a.pricing))["models"]

    by_seq = {s["seq"]: s for s in spans}

    def form(seq: int) -> str | None:
        s = by_seq[seq]
        f = FORM_OF_KIND.get(s["kind"], "prose")
        if f == "code":
            body = "".join(s["text"].split())
            if len(body) >= 32 and len(HEX.findall(body)) / len(body) >= 0.6:
                return "test_vector"
        return f

    page_of_span = {s["seq"]: s["page"] for s in spans}
    chunk_pages = meta["chunk_pages"]
    qs = meta["questions"]

    def route_filter(qtype: str, ranked: list[int]) -> list[int]:
        allowed = ROUTE_FORMS[qtype]
        return ranked if allowed is None else [x for x in ranked if form(x) in allowed]

    variants: dict[str, dict[str, list[int]]] = {}   # variant -> qid -> top-10 pages
    top_chars: dict[str, dict[str, int]] = {}       # variant -> qid -> chars of the top-10 cards
    top_sets: dict[str, dict[str, tuple]] = {}
    span_chars = {s["seq"]: len(s["text"]) for s in spans}
    for q in qs:
        qid, t = q["id"], q["type"]
        d, f = dense["spans"][qid], fts.get(qid) or []
        runs = {
            "C_chunks": ("chunk", dense["chunks"][qid][:K]),
            "V0_spans": ("span", d[:K]),
            "V3_form": ("span", route_filter(t, d)[:K]),
        }
        for w, wv in W_DENSE.items():
            runs[f"H_{w}"] = ("span", rrf(d[:CANDIDATES], f, wv)[:K])
            runs[f"V3H_{w}"] = ("span", route_filter(t, rrf(d[:CANDIDATES], route_filter(t, f), wv))[:K])
        runs["F_fts"] = ("span", f[:K])
        # informative, not a B10 variant: spans in dense order until they hold as many characters as the
        # question's top-10 chunks (the chunk baseline reads about 4 times the text of 10 spans)
        budget, acc, eq = sum(meta["chunk_chars"][i] for i in dense["chunks"][qid][:K]), 0, []
        for x in d:
            if acc >= budget:
                break
            eq.append(x)
            acc += span_chars[x]
        runs["V0_spans_eqchars"] = ("span", eq)
        for name, (unit, ids) in runs.items():
            pages = [chunk_pages[i] for i in ids] if unit == "chunk" else [page_of_span[i] for i in ids]
            variants.setdefault(name, {})[qid] = pages
            chars = sum(meta["chunk_chars"][i] for i in ids) if unit == "chunk" else sum(span_chars[i] for i in ids)
            top_chars.setdefault(name, {})[qid] = chars
            top_sets.setdefault(name, {})[qid] = (unit, tuple(sorted(ids)))

    hit = {v: {q["id"]: any(p in q["gold"] for p in pages[q["id"]]) for q in qs} for v, pages in variants.items()}
    types = {q["id"]: q["type"] for q in qs}
    recall = {v: {t: sum(h[i] for i in h if types[i] == t) for t in TYPES} | {"all": sum(h.values())}
              for v, h in hit.items()}

    pairs = [("C_chunks", "V0_spans"), ("V0_spans", "V3_form"), ("V0_spans", "H_w5"), ("V0_spans", "H_w1"),
             ("V3_form", "V3H_w5"), ("V3_form", "V3H_w1"), ("C_chunks", "V3_form"),
             ("C_chunks", "V0_spans_eqchars")]
    tests = []
    for base, new in pairs:
        for t in TYPES:
            ids = [i for i in hit[base] if types[i] == t]
            gain = sum(hit[new][i] and not hit[base][i] for i in ids)
            loss = sum(hit[base][i] and not hit[new][i] for i in ids)
            tests.append({"base": base, "new": new, "type": t, "base_hits": sum(hit[base][i] for i in ids),
                          "new_hits": sum(hit[new][i] for i in ids), "gains": gain, "losses": loss,
                          "net": gain - loss, "p_exact": round(mcnemar(gain, loss), 4),
                          "significant": mcnemar(gain, loss) < 0.05 and gain > loss})

    # ---------------------------------------------------------------- sizes for the projection
    indexed = [s for s in spans if s["text"].strip()]
    chapters = []  # (number, first page, last page) from level-1 bookmarks titled "Глава N" (check_eval_set.py)
    starts = sorted(o["page"] for o in outline if o["level"] == 1 and o["page"])
    for o in outline:
        m = re.match(r"Глава (\d+)", o["title"])
        if o["level"] == 1 and o["page"] and m:
            last = next((x - 1 for x in starts if x > o["page"]), 10 ** 6)
            chapters.append((int(m.group(1)), o["page"], last))
    ch_of_page = {}
    for n, lo, hi in chapters:
        for p in range(lo, min(hi, 10 ** 4) + 1):
            ch_of_page[p] = n
    ch_chars: dict[int, int] = {}
    ch_spans: dict[int, int] = {}
    outside = {"spans": 0, "chars": 0}
    for s in indexed:
        n = ch_of_page.get(s["page"])
        if n is None:
            outside["spans"] += 1
            outside["chars"] += len(s["text"])
        else:
            ch_chars[n] = ch_chars.get(n, 0) + len(s["text"])
            ch_spans[n] = ch_spans.get(n, 0) + 1

    def tok(chars: int | Decimal) -> int:
        return int((Decimal(chars) * TOKENS_PER_CHAR).to_integral_value(ROUND_HALF_UP))

    def price(model: str, inp: int = 0, out_: int = 0, cw1h: int = 0, cw5m: int = 0, cr: int = 0) -> Decimal:
        r = prices[model]
        if inp + cw1h + cw5m + cr > 100_000 and "above_100k" in r:
            r = r["above_100k"]
        total = (Decimal(inp) * Decimal(str(r["input"])) + Decimal(out_) * Decimal(str(r["output"]))
                 + Decimal(cw1h) * Decimal(str(r["cache_write_1h"])) + Decimal(cw5m) * Decimal(str(r["cache_write_5m"]))
                 + Decimal(cr) * Decimal(str(r["cache_read"])))
        return total / Decimal(10 ** 6)

    H = "claude-haiku-5-5"

    # spans outside the 23 chapters (front matter, appendices) form group 0, whose context is the mean chapter's
    # size (an estimate: their text is not one coherent chapter)
    mean_ch = sum(ch_chars.values()) // len(ch_chars)
    groups = {n: [s for s in indexed if ch_of_page.get(s["page"]) == n] for n in ch_chars}
    groups[0] = [s for s in indexed if s["page"] not in ch_of_page]
    ctx_chars = {**ch_chars, 0: mean_ch}

    def prefix_cost(chapter_set: list[int], out_tok: int) -> dict:
        calls = 0
        tin = tout = 0
        unc = c1h = c5m = Decimal(0)
        biggest = 0
        for n in chapter_set:
            ctx = CLI_OVERHEAD + PREFIX_INSTR + tok(ctx_chars[n])
            biggest = max(biggest, ctx)
            for i, s in enumerate(groups[n]):
                st = tok(len(s["text"]))
                calls += 1
                tin += ctx + st
                tout += out_tok
                # each call priced on its own: the >100k tier is per request (pricing.py rates)
                unc += price(H, inp=ctx + st, out_=out_tok)
                # cached: the chapter context is the shared prefix, written by the group's first call and read
                # by the rest; the span and the output are never cached
                if i == 0:
                    c1h += price(H, cw1h=ctx, inp=st, out_=out_tok)
                    c5m += price(H, cw5m=ctx, inp=st, out_=out_tok)
                else:
                    c1h += price(H, cr=ctx, inp=st, out_=out_tok)
                    c5m += price(H, cr=ctx, inp=st, out_=out_tok)
        return {"chapters": len(chapter_set), "calls": calls, "input_tokens": tin, "output_tokens": tout,
                "largest_call_input_tokens": biggest,
                "usd_uncached": str(micro(unc)), "usd_cached_1h": str(micro(c1h)), "usd_cached_5m": str(micro(c5m)),
                "wall_h_seq": [str((HAIKU_S[0] * calls / 3600).quantize(Decimal("0.01"))),
                               str((HAIKU_S[1] * calls / 3600).quantize(Decimal("0.01")))]}

    all_ch = sorted(ch_chars)
    q_per_ch: dict[int, int] = {}
    for q in qs:
        q_per_ch[q["chapter"]] = q_per_ch.get(q["chapter"], 0) + 1
    # two pilot chapters: the two with the most evaluation questions, ties to the smaller chapter (cheaper)
    pilot2 = sorted(all_ch, key=lambda n: (-q_per_ch.get(n, 0), ch_chars[n]))[:2]
    prefix = {f"out{o}": {"book": prefix_cost([*all_ch, 0], o), "pilot2": prefix_cost(pilot2, o)}
              for o in PREFIX_OUT}

    # L2 summaries: one call per outline node at level <= 4; a node's own text is the spans from its page to the
    # next node's page (an upper bound on the share of a page; page granularity), inner nodes add their children's
    # summaries, plus one root call over the chapter summaries
    nodes = [o for o in outline if o["level"] <= 4 and o["page"]]
    nodes.sort(key=lambda o: (o["page"], o["seq"]))
    page_chars: dict[int, int] = {}
    for s in indexed:
        page_chars[s["page"]] = page_chars.get(s["page"], 0) + len(s["text"])
    sum_in = sum_out = 0
    sum_usd = Decimal(0)
    kinds = {"leaf": 0, "inner": 0, "chapter": 0, "root": 1}
    for i, o in enumerate(nodes):
        nxt = nodes[i + 1]["page"] if i + 1 < len(nodes) else max(page_chars) + 1
        own = sum(page_chars.get(p, 0) for p in range(o["page"], max(nxt, o["page"] + 1)))
        children = [c for c in nodes[i + 1:] if c["level"] == o["level"] + 1]
        has_child = i + 1 < len(nodes) and nodes[i + 1]["level"] > o["level"]
        kind = "chapter" if o["level"] == 1 else ("inner" if has_child else "leaf")
        kinds[kind] += 1
        child_out = 0
        if has_child:
            j = i + 1
            while j < len(nodes) and nodes[j]["level"] > o["level"]:
                if nodes[j]["level"] == o["level"] + 1:
                    nj = nodes[j + 1]["level"] if j + 1 < len(nodes) else 0
                    child_out += SUMMARY_OUT["leaf" if nj <= nodes[j]["level"] else "inner"]
                j += 1
        del children
        inp = CLI_OVERHEAD + SUMMARY_INSTR + tok(own) + child_out
        sum_in += inp
        sum_out += SUMMARY_OUT[kind]
        sum_usd += price(H, inp=inp, out_=SUMMARY_OUT[kind])
    root_in = CLI_OVERHEAD + SUMMARY_INSTR + SUMMARY_OUT["chapter"] * kinds["chapter"]
    sum_in += root_in
    sum_out += SUMMARY_OUT["root"]
    sum_usd += price(H, inp=root_in, out_=SUMMARY_OUT["root"])
    n_calls = len(nodes) + 1
    summaries = {"calls": n_calls, "nodes_by_kind": kinds, "input_tokens": sum_in, "output_tokens": sum_out,
                 "usd": str(micro(sum_usd)),
                 "wall_h_seq": [str((HAIKU_S[0] * n_calls / 3600).quantize(Decimal("0.01"))),
                                str((HAIKU_S[1] * n_calls / 3600).quantize(Decimal("0.01")))]}

    # judge.eval sufficiency: one call per (question, distinct top-10 card set); prefixed cards add the prefix
    prefix_mid = sum(PREFIX_OUT) // 2
    judged_variants = {"C_chunks": 0, "V0_spans": 0, "V3_form": 0, "H_w5": 0,
                       "V1_prefix": prefix_mid, "V2_summaries": prefix_mid}
    judge = {}
    for model in ("claude-sonnet-5-5", "claude-opus-5-5"):
        calls = tin = 0
        usd = Decimal(0)
        seen = set()
        per_variant = {}
        for v, extra in judged_variants.items():
            src = v if v in top_chars else "V0_spans"  # V1/V2 sizes: V0's cards plus a prefix each (estimate)
            vin = vcalls = 0
            for q in qs:
                # V1 and V2 cards differ from V0's (prefixes, summaries), so they never share a judgement
                key = (top_sets[src][q["id"]], q["id"], v if v not in top_chars else "")
                if key in seen:
                    continue
                seen.add(key)
                inp = CLI_OVERHEAD + JUDGE_INSTR + tok(top_chars[src][q["id"]]) + K * extra
                vin += inp
                vcalls += 1
                usd += price(model, inp=inp, out_=JUDGE_OUT)
            calls += vcalls
            tin += vin
            per_variant[v] = {"calls": vcalls, "input_tokens": vin}
        judge[model] = {"calls": calls, "input_tokens": tin, "output_tokens": calls * JUDGE_OUT,
                        "usd": str(micro(usd)), "per_variant": per_variant}
    # the fact-check route against today's whole-section fact-check: 30 judge calls with the question's chapter
    fc = [q for q in qs if q["type"] == "fact-check"]
    whole = {}
    for model in ("claude-sonnet-5-5", "claude-opus-5-5"):
        usd = sum((price(model, inp=CLI_OVERHEAD + JUDGE_INSTR + tok(ch_chars[q["chapter"]]), out_=JUDGE_OUT)
                   for q in fc), Decimal(0))
        whole[model] = {"calls": len(fc), "input_tokens": sum(CLI_OVERHEAD + JUDGE_INSTR + tok(ch_chars[q["chapter"]])
                                                              for q in fc), "usd": str(micro(usd))}

    chars_sorted = sorted(meta["span_chars"])
    sizes = {
        "spans_total": meta["spans_total"], "spans_indexed": meta["spans_indexed"], "spans_empty": meta["spans_empty"],
        "chunks": meta["chunks"], "outline_entries": len(outline), "outline_nodes_level_le4": len(nodes),
        "chapters_found": len(chapters), "spans_outside_chapters": outside,
        "span_chars_mean": round(statistics.mean(chars_sorted), 1),
        "span_chars_p50": chars_sorted[len(chars_sorted) // 2],
        "span_chars_p95": chars_sorted[int(len(chars_sorted) * 0.95)],
        "span_claude_tokens_est_mean": tok(Decimal(str(statistics.mean(chars_sorted)))),
        "span_bge_tokens_mean": round(statistics.mean(meta["span_bge_tokens"]), 1),
        "spans_over_1200_chars": meta["spans_over_1200_chars"], "spans_over_max_seq": meta["spans_over_max_seq"],
        "chapter_claude_tokens_est": {str(n): tok(ch_chars[n]) for n in all_ch},
        "book_claude_tokens_est": tok(sum(ch_chars.values()) + outside["chars"]),
        "questions_per_chapter": {str(k): v for k, v in sorted(q_per_ch.items())},
        "form_counts": {},
        "top10_chars_mean": {v: round(statistics.mean(c.values())) for v, c in top_chars.items()},
    }
    for s in indexed:
        f = str(form(s["seq"]))
        sizes["form_counts"][f] = sizes["form_counts"].get(f, 0) + 1

    (out / "recall.json").write_text(json.dumps(
        {"k": K, "recall_hits": recall, "per_question": {v: {i: int(h[i]) for i in h} for v, h in hit.items()},
         "route_forms": {t: sorted(f) if f else None for t, f in ROUTE_FORMS.items()}}, indent=1))
    (out / "mcnemar.json").write_text(json.dumps(tests, indent=1))
    (out / "sizes.json").write_text(json.dumps(sizes, indent=1))
    (out / "projection.json").write_text(json.dumps(
        {"assumptions": {"tokens_per_char": str(TOKENS_PER_CHAR.quantize(Decimal("0.0001"))),
                         "cli_overhead": CLI_OVERHEAD, "prefix_instr": PREFIX_INSTR, "summary_instr": SUMMARY_INSTR,
                         "judge_instr": JUDGE_INSTR, "judge_out": JUDGE_OUT, "haiku_s": [str(x) for x in HAIKU_S]},
         "pilot2_chapters": pilot2, "prefix": prefix, "summaries": summaries, "judge_eval": judge,
         "factcheck_whole_section": whole, "latency": latency,
         "embed": {"spans": meta["embed_spans"], "chunks": meta["embed_chunks"],
                   "query_embed_ms_mean": meta["query_embed_ms_mean"]}}, indent=1))
    print(json.dumps({"recall": recall, "significant": [t for t in tests if t["significant"]]}))


main()
