"""SRV-SEARCH [local]: the acceptance bench on the owner's Mac (pilot PDF, real embed image, real Postgres).

@trace NFR-SEARCH-1 NFR-SEARCH-2 NFR-SEARCH-3 NFR-SEARCH-4 VEC-SEARCH-3

Opt-in: READER_PILOT_PDF=<path to the pilot PDF> and a built reader-embed:local (see test_search_image.py).
The book's text exists only in memory and in the throwaway database. The results hold numbers only (counts,
ranks, page-free metrics, the PDF's sha256): they are written to spec/fixtures/SRV-SEARCH/results/ (the grid
phase to fusion-weight-v1.json, the test phase to fusion-test-v2.json, both committed as the spec's sources)
and to READER_BENCH_OUT (default: a temp file).

Spans: SRV-INGEST's artifact does not exist yet (its code is built in parallel), so each page's pypdfium2
text layer is one span, the same text the embeddings-local spike chunked (declared in the result). The
chunks, the vectors (the real embed service, batches of 8), the full text and the fusion are this module's.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from .conftest import DOCKER, FIXTURES, Pg, World, add_material, need, page_of, scalar, world

PDF = os.environ.get("READER_PILOT_PDF", "")
pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(not (PDF and DOCKER), reason="opt-in [local]: READER_PILOT_PDF and a docker daemon"),
]
IMAGE = "reader-embed:local"
V1 = Path(__file__).resolve().parents[3] / "spikes" / "embeddings-local" / "questions.yaml"
TMPFS = "/tmp:size=64m,uid=10003"  # noqa: S108 - a container tmpfs, as compose.yaml declares it
SHA = "a" * 64


def _pages(path: str) -> list[str]:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(path)
    return [doc[i].get_textpage().get_text_range() for i in range(len(doc))]


def _questions(path: Path) -> list[dict[str, Any]]:
    from ruamel.yaml import YAML

    data: dict[str, Any] = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    questions: list[dict[str, Any]] = data["questions"]
    return questions


def _start_embed() -> tuple[str, str]:
    run = subprocess.run(
        ["docker", "run", "-d", "--read-only", "--cap-drop", "ALL", "--security-opt",
         "no-new-privileges:true", "--tmpfs", TMPFS, "--user", "10003:10003",
         "--memory", "3g", "--memory-swap", "3g", "-p", "127.0.0.1::8100", IMAGE],
        capture_output=True, text=True, check=True,
    )
    cid = run.stdout.strip()
    port = subprocess.run(
        ["docker", "port", cid, "8100/tcp"], capture_output=True, text=True, check=True
    ).stdout.strip().rsplit(":", 1)[1]
    return cid, f"http://127.0.0.1:{port}"


def _truncated(texts: list[str]) -> int:
    """Chunks over 1,024 tokens, counted by the image's own tokenizer in a one-shot offline container."""
    code = (
        "import json, sys; from transformers import AutoTokenizer; "
        "t = AutoTokenizer.from_pretrained('/opt/models/bge-m3'); "
        "texts = json.loads(sys.stdin.read()); "
        "print(sum(1 for x in texts if len(t(x)['input_ids']) > 1024))"
    )
    out = subprocess.run(
        ["docker", "run", "--rm", "-i", "--network", "none", "--read-only", "--tmpfs", TMPFS,
         "--user", "10003:10003", "--entrypoint", "python", IMAGE, "-I", "-c", code],
        input=json.dumps(texts), capture_output=True, text=True, check=True,
    )
    return int(out.stdout.strip().splitlines()[-1])


GRID = (1, 1.5, 2, 3, 5)  # NFR-SEARCH-1's dev/test protocol
RESULTS = FIXTURES / "results"
PHASE = os.environ.get("READER_BENCH_PHASE", "test")  # grid: v1 only; test: the one v2 evaluation


async def _lists(w: World, questions: list[dict[str, Any]], store: Any) -> list[dict[str, Any]]:
    """Per question: gold pages, the dense and the full-text candidate lists (w-independent), chunk → page."""
    owner = w.users["ivan"]
    out = []
    for q in questions:
        vector = await store.embed_query(q["question"])
        async with w.engine.connect() as conn:
            dense = await store.dense_list(conn, owner, vector, None)
            fts = await store.fts_list(conn, owner, q["question"], None)
        pages = {i: await page_of(w.engine, i) for i in {*dense, *fts}}
        out.append({"gold": set(q["gold_pages"]), "dense": dense, "fts": fts, "pages": pages})
    return out


def _metrics(lists: list[dict[str, Any]], order: Any) -> dict[str, Any]:
    """Hits at 10, recall@1/3 and MRR for the ranking `order(item)` gives each question."""
    hits = r1 = r3 = 0
    mrr = 0.0
    for item in lists:
        pages = [item["pages"][cid] for cid in order(item)[:10]]
        rank = next((i + 1 for i, p in enumerate(pages) if p in item["gold"]), None)
        hits += rank is not None
        r1 += rank == 1
        r3 += rank is not None and rank <= 3
        mrr += 1 / rank if rank else 0.0
    n = len(lists)
    return {"hits": hits, "n": n, "recall_at_10": hits / n, "recall_at_1": r1 / n, "recall_at_3": r3 / n,
            "mrr": round(mrr / n, 4)}


def _fused(w_dense: float) -> Any:
    fusion = need("reader.search.fusion")
    return lambda item: [f.id for f in fusion.rrf(item["dense"], item["fts"], w_dense=w_dense)]


def _choose(grid: dict[str, dict[str, Any]]) -> float:
    """Max v1 recall@10, then MRR, then the smallest w (NFR-SEARCH-1)."""
    return min(GRID, key=lambda w: (-grid[str(w)]["recall_at_10"], -grid[str(w)]["mrr"], w))


def _write(name: str, result: dict[str, Any]) -> None:
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / name).write_text(json.dumps(result, indent=2) + "\n")
    out = Path(os.environ.get("READER_BENCH_OUT") or tempfile.mktemp(suffix=".json"))  # noqa: S306
    out.write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


async def test_pilot_bench(pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = need("reader.search.store")
    jobs = need("reader.search.jobs")
    params = need("reader.search.params")
    from sqlalchemy import text

    from reader.jobs.model import JobContext

    pdf_sha = hashlib.sha256(Path(PDF).read_bytes()).hexdigest()  # noqa: ASYNC240 - a bench, not a server
    cid, url = _start_embed()
    monkeypatch.setenv("READER_EMBED_URL", url)
    try:
        w = await world(pg, tmp_path, monkeypatch)
        spans = [(i + 1, body) for i, body in enumerate(_pages(PDF)) if body.strip()]
        ingest = await add_material(w, "ivan", SHA, spans, config_id="book-ru")
        started = time.monotonic()
        async with w.engine.begin() as conn:
            args = jobs.embed_args(SHA, ingest, fts_config="russian")
            await jobs.handle_embed(args, conn, JobContext(1, "bench", 0))
        embed_min = (time.monotonic() - started) / 60
        chunks = await scalar(w.engine, "SELECT count(*) FROM search_chunks")
        base = {
            "pdf_sha256": pdf_sha,
            "spans": "pypdfium2 text layer, one span per page (no SRV-INGEST artifact yet)",
            "chunks": chunks, "embed_job_min": round(embed_min, 2),
        }
        v1 = await _lists(w, _questions(V1), store)
        if PHASE == "grid":  # the dev set only; no v2 question is read in this phase
            grid = {str(wd): _metrics(v1, _fused(wd)) for wd in GRID}
            chosen = _choose(grid)
            rule = "max recall@10, then MRR, then smallest w"
            dev = {**base, "set": "v1 (dev)", "grid": grid, "chosen_w_dense": chosen, "rule": rule}
            _write("fusion-weight-v1.json", dev)
            await w.engine.dispose()
            return
        async with w.engine.connect() as conn:
            texts = [r[0] for r in await conn.execute(text("SELECT text FROM search_chunks ORDER BY id"))]
        truncated = _truncated(texts)
        chosen = json.loads((RESULTS / "fusion-weight-v1.json").read_text())["chosen_w_dense"]
        assert chosen == params.W_DENSE
        v2 = await _lists(w, _questions(FIXTURES / "pilot-questions-v2.yaml"), store)
        result = {
            **base, "truncated_chunks": truncated, "w_dense": chosen,
            "v2_test": {
                "dense": _metrics(v2, lambda i: i["dense"]), "fts": _metrics(v2, lambda i: i["fts"]),
                "hybrid": _metrics(v2, _fused(chosen)),
                "empty_fts_lists": sum(not i["fts"] for i in v2),
            },
            "v1_dev": {
                "dense": _metrics(v1, lambda i: i["dense"]), "fts": _metrics(v1, lambda i: i["fts"]),
                "hybrid": _metrics(v1, _fused(chosen)),
            },
        }
        served = await store.search(w.engine, w.users["ivan"], _questions(V1)[0]["question"], k=10)
        result["search_mode"] = "hybrid" if params.HYBRID else "dense-only (hybrid below dense on v2)"
        result["search_served_results"] = len(served.results)
        _write("fusion-test-v2.json", result)
        await w.engine.dispose()
    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True, check=False)  # noqa: ASYNC221
    v2t, v1d = result["v2_test"], result["v1_dev"]
    served_mode = "hybrid" if params.HYBRID else "dense"
    assert truncated == 0  # NFR-SEARCH-4
    # NFR-SEARCH-1, first condition, for what search() serves
    assert v2t[served_mode]["hits"] >= v2t["dense"]["hits"]
    assert v2t[served_mode]["hits"] >= 27  # NFR-SEARCH-1, second condition
    assert v1d[served_mode]["recall_at_10"] >= 0.8  # NFR-SEARCH-2
    assert embed_min <= 60  # NFR-SEARCH-3
