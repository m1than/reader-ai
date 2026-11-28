"""Spike tutor-find-m1 (SRV-TUTOR kl_revision measurement_plan.free, M1): which sources each tutor retrieval
variant sends for the 40 VEC-TUTOR-7 questions, after FR-TUTOR-6's 29,000-byte drop rule. Zero model calls.

    cd server && uv run python ../spikes/tutor-find-m1/m1.py questions --work <work>      # the 40 questions
    cd server && uv run python ../spikes/tutor-find-m1/m1.py measure --art <artifact> --work <work> --out <json>

The shipped code runs unchanged against a throwaway pgvector container (testcontainers, the search tests' pinned
image; never the owner's stack): the search tests' own helpers (server/tests/search/conftest.py `world`,
`add_material`, `embed_material`) seed the pilot's converter artifact as a sealed, active r2 pack with profile
language ru, and the real `search.embed` handler indexes it. The embed service is the real
`reader.search.embed_service` HTTP server whose encoder looks up the bge-m3 vectors embed_m1.py computed (by the
sha256 of each text; a missing text is an error, never a stand-in vector).

Variants (each through `reader.tutor.answer.build_prompt` with no cell, the bench's case):
  a: today's merged path: `reader.search.store.search(k=8)`, span_ids = [span_id], `candidate_span_ids`;
  b: `find(channel tutor, budget 10,500)`'s L1 card ids in response order;
  c: b, then each card's first `spn:` next id in the same order, no repeats (this revision, VEC-TUTOR-9);
  d: c, then each card's previous span in reading order in the same order, no repeats.
Output: counts, bytes, pages-hit flags and question ids only; never span text or span ids.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from statistics import mean
from typing import Any

import numpy as np
from ruamel.yaml import YAML

SERVER = Path(__file__).resolve().parents[2] / "server"
REPO = SERVER.parent
sys.path[:0] = [str(SERVER / "src"), str(SERVER / "tests")]

BUDGET = 10_500  # the revision's channels.tutor.budget_default (paired SRV-SEARCH change)
VARIANTS = ("a", "b", "c", "d")
RETRIEVE_K = 8  # variant (a): the pre-revision tutor's search k (bounds.retrieve_k, removed by the revision)


def questions() -> list[dict[str, Any]]:
    """VEC-TUTOR-7: questions-v2 with v03 replaced by t01, in id order."""
    yaml = YAML(typ="safe")
    v2 = yaml.load((REPO / "spikes/embeddings-local/questions-v2.yaml").read_text(encoding="utf-8"))["questions"]
    fixed = yaml.load((REPO / "spec/fixtures/SRV-TUTOR/bench-fixed.yaml").read_text(encoding="utf-8"))["questions"]
    out = [q for q in v2 if q["id"] != "v03"] + fixed
    assert len(out) == 40 and len({q["id"] for q in out}) == 40
    return [{"id": q["id"], "gold_pages": [int(p) for p in q["gold_pages"]], "question": q["question"]} for q in out]


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class LookupEncoder:
    """The embed service's `Encoder`: the precomputed bge-m3 vector of each text, by its sha256."""

    def __init__(self, work: Path) -> None:
        keys = json.loads((work / "keys.json").read_text())
        vectors = np.frombuffer((work / "vectors.f32").read_bytes(), dtype="<f4").reshape(len(keys), 1024)
        self.rows = {k: i for i, k in enumerate(keys)}
        self.vectors = vectors
        self.lock = threading.Lock()

    def encode(self, kind: str, texts: list[str]) -> list[list[float]]:
        del kind
        return [self.vectors[self.rows[sha(t)]].astype(float).tolist() for t in texts]


def dedupe(ids: list[str]) -> list[str]:
    return list(dict.fromkeys(ids))


def score(prompt_sources: Any, gold: set[int]) -> dict[str, Any]:
    sizes = [len(s.text.encode("utf-8")) for s in prompt_sources]
    on_gold = [n for s, n in zip(prompt_sources, sizes, strict=True) if s.page in gold]
    return {"sources": len(sizes), "source_bytes": sum(sizes), "gold_in_sources": bool(on_gold),
            "gold_spans": len(on_gold), "gold_bytes": sum(on_gold)}


async def measure(art: Path, work: Path) -> dict[str, Any]:
    from search import conftest as sc  # the SRV-SEARCH tests' helpers (world, add_material, embed_material)
    from testcontainers.community.postgres import PostgresContainer

    from reader.ingest import views
    from reader.search import embed_service, params
    from reader.search import policy as policy_mod
    from reader.search import store
    from reader.search.cards import SEPARATOR_TOKENS, text_of
    from reader.search.corpus import StoreCorpus
    from reader.search.find import Caller, Deps, find
    from reader.search.policy import estimate
    from reader.tutor import params as tutor_params
    from reader.tutor.answer import Span, build_prompt
    from reader.tutor.jobs import candidate_ids

    def candidate_span_ids(hits: list[Any]) -> list[str]:
        """Variant (a) frozen as recorded at 1dc2990 (the tutor's pre-revision rule, removed from reader.tutor):
        every hit's primary span in rank order, then its other chunk spans, no repeats."""
        ordered = [hit.span_id for hit in hits]
        for hit in hits:
            ordered.extend(s for s in hit.span_ids if s != hit.span_id)
        return list(dict.fromkeys(ordered))

    spans = [json.loads(line) for line in (art / "spans.jsonl").open(encoding="utf-8")]
    outline = json.loads((art / "outline.json").read_text(encoding="utf-8"))
    manifest = json.loads((art / "manifest.json").read_text(encoding="utf-8"))
    qs = questions()
    server = embed_service.make_server(LookupEncoder(work), "127.0.0.1", 0, model_id=params.MODEL_ID)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    os.environ["READER_EMBED_URL"] = f"http://127.0.0.1:{server.server_address[1]}"
    password = "pw-" + uuid.uuid4().hex[:16]
    with PostgresContainer(sc.PG_IMAGE, username="postgres", password=password, dbname="postgres",
                           driver=None) as c, tempfile.TemporaryDirectory() as tmp:
        pg = sc.Pg(c.get_container_host_ip(), int(c.get_exposed_port(5432)), "postgres", password, "postgres")
        w = await sc.world(pg, Path(tmp))
        # policy_version reads the judge slots from the test config (none), so the version is this run's only
        os.environ["READER_CONFIG"] = str(w.config_path)
        owner = w.users["ivan"]
        t0 = time.monotonic()
        ingest = await sc.add_material(
            w, "ivan", sc.MAT_RU, [(int(s["page"]), str(s["text"])) for s in spans],
            kinds=[str(s["kind"]) for s in spans],
            outline=[(int(o["level"]), str(o["title"]), o["page"]) for o in outline])
        seeded_s = time.monotonic() - t0
        await sc.embed_material(w, ingest, sc.MAT_RU)
        indexed_s = time.monotonic() - t0 - seeded_s
        windows = await sc.scalar(w.engine, "SELECT count(*) FROM search_span_vectors")
        loaded_policy = policy_mod.policy()
        version = policy_mod.policy_version(loaded_policy)
        caller = Caller(owner, ("owner",), True)
        records: list[dict[str, Any]] = []
        costs: list[float] = []
        async with w.engine.connect() as conn:
            pack = await views.active_pack(conn, owner, sc.MAT_RU)
            assert pack is not None
            order = [s.locator for s in await views.pack_spans(conn, pack)]
            previous = {loc: order[i - 1] for i, loc in enumerate(order) if i > 0 and loc is not None}

            async def sources(ids: list[str], question: str, gold: set[int]) -> dict[str, Any]:
                ids = ids[: tutor_params.SOURCES_MAX]
                loaded = await views.active_span_texts(conn, owner, ids)
                prompt = build_prompt(question, None, [Span(i, loaded[i][0], loaded[i][1]) for i in ids if i in loaded])
                return {**score(prompt.sources, gold), "prompt_bytes": len(prompt.text.encode("utf-8"))}

            for q in qs:
                gold = set(q["gold_pages"])
                rec: dict[str, Any] = {"id": q["id"]}
                found = await store.search(w.engine, owner, q["question"], RETRIEVE_K, sc.MAT_RU)
                rec["a"] = {"hits": len(found.results), "degraded": found.degraded,
                            **await sources(candidate_span_ids(found.results), q["question"], gold)}
                t1 = time.monotonic()
                response = await find(caller, "tutor", q["question"], budget=BUDGET, deps=Deps(
                    corpus=StoreCorpus(conn, loaded_policy), policy=loaded_policy, policy_version=lambda: version))
                find_ms = round(1000 * (time.monotonic() - t1))
                l1 = [card for card in response["cards"] if card["layer"] == "L1" and card["id"].startswith("spn:")]
                costs.extend(estimate(text_of({**card, "judged": False})) + SEPARATOR_TOKENS for card in l1)
                primaries = [card["id"] for card in l1]
                nexts = [next((n for n in card["next"] if n.startswith("spn:")), None) for card in l1]
                prevs = [previous.get(card["id"]) for card in l1]
                pages = await views.active_span_texts(conn, owner, primaries)
                rec["find"] = {"cards": len(response["cards"]), "l1_cards": len(l1), "ms": find_ms,
                               "degraded": bool(response.get("degraded")),
                               "gold_in_find": any(pages[i][0] in gold for i in primaries if i in pages)}
                b = dedupe(primaries)
                c_ids = candidate_ids(response["cards"])  # the shipped rule (VEC-TUTOR-9)
                assert c_ids == dedupe(b + [n for n in nexts if n])  # equals the rule as recorded at 1dc2990
                d_ids = dedupe(c_ids + [p for p in prevs if p])
                for name, ids in (("b", b), ("c", c_ids), ("d", d_ids)):
                    rec[name] = {"candidates": len(ids), **await sources(ids, q["question"], gold)}
                records.append(rec)

    summary: dict[str, Any] = {}
    for v in VARIANTS:
        rows = [r[v] for r in records]
        summary[v] = {
            "gold_in_sources": sum(r["gold_in_sources"] for r in rows),
            "sources_mean": round(mean(r["sources"] for r in rows), 1),
            "source_bytes_mean": round(mean(r["source_bytes"] for r in rows)),
            "prompt_bytes_max": max(r["prompt_bytes"] for r in rows),
            "gold_spans_mean": round(mean(r["gold_spans"] for r in rows), 2),
            "gold_bytes_mean": round(mean(r["gold_bytes"] for r in rows)),
        }
    summary["find"] = {
        "gold_in_find": sum(r["find"]["gold_in_find"] for r in records),
        "l1_cards_mean": round(mean(r["find"]["l1_cards"] for r in records), 1),
        "l1_card_cost_mean_tokens": round(mean(costs), 1),
        "ms_p50": sorted(r["find"]["ms"] for r in records)[len(records) // 2],
        "degraded": sum(r["find"]["degraded"] for r in records),
    }
    c, a = summary["c"]["gold_in_sources"], summary["a"]["gold_in_sources"]
    summary["build_threshold_met"] = c >= a and c >= 26
    summary["p1_threshold_met"] = c == 40
    return {
        "spike": "tutor-find-m1", "model_calls": 0, "usd": 0,
        "artifact": {k: manifest[k] for k in ("material_sha256", "converter_version", "span_count", "outline_count",
                                              "spans_sha256") if k in manifest},
        "index": {"windows": windows, "seed_s": round(seeded_s), "embed_job_s": round(indexed_s),
                  "embed_model": params.MODEL_ID},
        "policy_version": version, "budget": BUDGET, "prompt_bytes_max": tutor_params.PROMPT_BYTES_MAX,
        "summary": summary, "questions": records,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=("questions", "measure"))
    ap.add_argument("--art", type=Path)
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    a.work.mkdir(parents=True, exist_ok=True)
    if a.step == "questions":
        (a.work / "questions.json").write_text(json.dumps(questions(), ensure_ascii=False), encoding="utf-8")
        return
    result = asyncio.run(measure(a.art, a.work))
    a.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"], indent=1))


main()
