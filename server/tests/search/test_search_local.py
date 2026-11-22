"""SRV-SEARCH [local]: the acceptance bench on the owner's Mac (pilot PDF, real embed image, real Postgres).

@trace NFR-SEARCH-1 NFR-SEARCH-2 NFR-SEARCH-3 NFR-SEARCH-4 VEC-SEARCH-3

Opt-in: READER_PILOT_PDF=<path to the pilot PDF> and a built reader-embed:local (see test_search_image.py).
The book's text exists only in memory and in the throwaway database; the result file holds numbers only and
is written to READER_BENCH_OUT (default: a temp file), never into the repository.

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

from .conftest import DOCKER, FIXTURES, Pg, World, add_material, need, scalar, world

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


def _truncated(cid: str, texts: list[str]) -> int:
    code = (
        "import json, sys; from transformers import AutoTokenizer; "
        "t = AutoTokenizer.from_pretrained('/opt/models/bge-m3'); "
        "texts = json.loads(sys.stdin.read()); "
        "print(sum(1 for x in texts if len(t(x)['input_ids']) > 1024))"
    )
    out = subprocess.run(
        ["docker", "exec", "-i", cid, "python", "-I", "-c", code],
        input=json.dumps(texts), capture_output=True, text=True, check=True,
    )
    return int(out.stdout.strip())


async def _hits(w: World, questions: list[dict[str, Any]], store: Any) -> dict[str, Any]:
    owner = w.users["ivan"]
    counts = {"dense": 0, "fts": 0, "hybrid": 0}
    r1 = r3 = 0
    mrr = 0.0
    empty_fts = 0
    for q in questions:
        gold = set(q["gold_pages"])
        vector = await store.embed_query(q["question"])
        async with w.engine.connect() as conn:
            dense_ids = (await store.dense_list(conn, owner, vector, None))[:10]
            fts_ids = await store.fts_list(conn, owner, q["question"], None)
            empty_fts += not fts_ids
            fts_ids = fts_ids[:10]
            dense_pages = [await store.page_of(conn, i) for i in dense_ids]
            fts_pages = [await store.page_of(conn, i) for i in fts_ids]
        hybrid = await store.search(w.engine, owner, q["question"], k=10)
        pages = [h.page for h in hybrid.results]
        counts["dense"] += bool(gold & set(dense_pages))
        counts["fts"] += bool(gold & set(fts_pages))
        counts["hybrid"] += bool(gold & set(pages))
        rank = next((i + 1 for i, p in enumerate(pages) if p in gold), None)
        r1 += rank == 1
        r3 += rank is not None and rank <= 3
        mrr += 1 / rank if rank else 0.0
    n = len(questions)
    return {
        **counts, "n": n, "recall_at_1": r1 / n, "recall_at_3": r3 / n, "mrr": round(mrr / n, 3),
        "empty_fts_lists": empty_fts,
    }


def _report(result: dict[str, Any]) -> None:
    out = Path(os.environ.get("READER_BENCH_OUT") or tempfile.mktemp(suffix=".json"))  # noqa: S306
    out.write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


async def test_pilot_bench(pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = need("reader.search.store")
    jobs = need("reader.search.jobs")
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
        async with w.engine.connect() as conn:
            texts = [r[0] for r in await conn.execute(text("SELECT text FROM search_chunks ORDER BY id"))]
        truncated = _truncated(cid, texts)
        v2 = await _hits(w, _questions(FIXTURES / "pilot-questions-v2.yaml"), store)
        v1 = await _hits(w, _questions(V1), store)
        _report({
            "pdf_sha256": pdf_sha,
            "spans": "pypdfium2 text layer, one span per page (no SRV-INGEST artifact yet)",
            "chunks": chunks, "embed_job_min": round(embed_min, 2), "truncated_chunks": truncated,
            "questions_v2": v2, "questions_v1": v1,
        })
        await w.engine.dispose()
    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True, check=False)  # noqa: ASYNC221
    assert truncated == 0  # NFR-SEARCH-4
    assert v2["hybrid"] >= v2["dense"]  # NFR-SEARCH-1, first condition
    assert v2["hybrid"] >= 27  # NFR-SEARCH-1, second condition
    assert v1["hybrid"] / v1["n"] >= 0.8  # NFR-SEARCH-2
    assert embed_min <= 60  # NFR-SEARCH-3
