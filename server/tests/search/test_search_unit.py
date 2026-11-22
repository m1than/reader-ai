"""SRV-SEARCH [unit]: chunking, fusion, query quoting, parameters, status map, the embed service protocol.

@trace FR-SEARCH-1 FR-SEARCH-2 FR-SEARCH-3 FR-SEARCH-5 FR-SEARCH-7 FR-SEARCH-9
@trace INV-SEARCH-6 INV-SEARCH-8 VEC-SEARCH-1 VEC-SEARCH-2 VEC-SEARCH-4
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from .conftest import REPO, EmbedServer, HashEncoder, fixture, need

# ---- FR-SEARCH-3, VEC-SEARCH-2 ----------------------------------------------------------------------------


def test_chunking_matches_the_fixture() -> None:
    chunking = need("reader.search.chunking")
    data = fixture("chunking.yaml")
    p = data["params"]
    spans = [chunking.Span(s["seq"], s["page"], s["text"]) for s in data["spans"]]
    chunks = chunking.chunk_spans(spans, size=p["size"], overlap=p["overlap"], min_len=p["min"])
    got = [
        {
            "page": c.page, "start": c.char_start, "end": c.char_stop, "text": c.text,
            "span_seqs": list(c.span_seqs), "primary_seq": c.primary_seq,
        }
        for c in chunks
    ]
    assert got == data["expected"]
    assert [c.chunk_no for c in chunks] == list(range(len(chunks)))


def test_chunking_production_parameters_are_the_spikes() -> None:
    params = need("reader.search.params")
    assert (params.CHUNK_SIZE, params.CHUNK_OVERLAP, params.CHUNK_MIN) == (1200, 200, 80)


def test_primary_span_is_the_largest_overlap() -> None:
    chunking = need("reader.search.chunking")
    spans = [chunking.Span(0, 1, "ab"), chunking.Span(1, 1, "cdefghij")]
    (chunk,) = chunking.chunk_spans(spans, size=20, overlap=5, min_len=4)
    assert chunk.span_seqs == (0, 1)
    assert chunk.primary_seq == 1


@given(
    st.lists(
        st.tuples(st.integers(1, 3), st.text(alphabet="ab \nц", max_size=60)), min_size=0, max_size=12
    )
)
def test_chunking_is_deterministic_and_covers_its_spans(raw: list[tuple[int, str]]) -> None:
    chunking = need("reader.search.chunking")
    spans = [chunking.Span(i, page, body) for i, (page, body) in enumerate(sorted(raw, key=lambda r: r[0]))]
    first = chunking.chunk_spans(spans, size=20, overlap=5, min_len=4)
    assert first == chunking.chunk_spans(spans, size=20, overlap=5, min_len=4)
    by_seq = {s.seq: s for s in spans}
    for chunk in first:
        assert len(chunk.text) >= 4
        assert chunk.span_seqs and chunk.primary_seq in chunk.span_seqs
        assert all(by_seq[q].page == chunk.page for q in chunk.span_seqs)


# ---- FR-SEARCH-7, INV-SEARCH-6, VEC-SEARCH-1 ---------------------------------------------------------------


def test_rrf_matches_the_fixture() -> None:
    fusion = need("reader.search.fusion")
    data = fixture("rrf.yaml")
    for case in data["cases"]:
        fused = fusion.rrf(case["dense"], case["fts"], k=data["k"], w_dense=case.get("w_dense", 1))
        got = [{"id": f.id, "score": round(f.score, 9)} for f in fused]
        assert got == case["expected"], case["name"]


@given(
    st.lists(st.integers(0, 40), unique=True, max_size=15),
    st.lists(st.integers(0, 40), unique=True, max_size=15),
    st.sampled_from([1, 1.5, 2, 3, 5]),
)
def test_rrf_is_the_formula_and_tie_rule(dense: list[int], fts: list[int], w: float) -> None:
    fusion = need("reader.search.fusion")
    fused = fusion.rrf(dense, fts, k=60, w_dense=w)
    assert {f.id for f in fused} == set(dense) | set(fts)
    for f in fused:
        expected = (w / (60 + dense.index(f.id) + 1) if f.id in dense else 0.0) + (
            1 / (60 + fts.index(f.id) + 1) if f.id in fts else 0.0
        )
        assert f.score == pytest.approx(expected, abs=1e-12)
    worst = len(dense) + 1
    keys = [(-f.score, f.dense_rank or worst) for f in fused]
    assert keys == sorted(keys)


def test_snippet_cuts_at_whitespace_or_hard() -> None:
    fusion = need("reader.search.fusion")
    assert fusion.snippet("word " * 100) == ("word " * 60).rstrip()
    assert fusion.snippet("x" * 400) == "x" * 300
    assert fusion.snippet("short text") == "short text"


@pytest.mark.parametrize(
    ("lexemes", "expected"),
    [
        (["block", "link"], "'block' | 'link'"),
        (["it's"], "'it''s'"),
        (["a\\b"], "'a\\\\b'"),
        ([], ""),
    ],
)
def test_lexemes_are_quoted_explicitly(lexemes: list[str], expected: str) -> None:
    fusion = need("reader.search.fusion")
    assert fusion.tsquery_text(lexemes) == expected


# ---- FR-SEARCH-5, FR-SEARCH-2 ----------------------------------------------------------------------------


def test_params_sha256_covers_parameters_model_and_language() -> None:
    params = need("reader.search.params")
    ru, en = params.params_sha256("russian"), params.params_sha256("english")
    assert ru != en and len(ru) == 64 and int(ru, 16) >= 0
    assert params.params_sha256("russian") == ru
    assert params.MAX_SEQ_LENGTH == 1024 and params.DIMENSIONS == 1024
    assert params.fts_config_for("ru") == "russian"
    assert params.fts_config_for("en") == "english"
    assert params.fts_config_for("de") == "simple"
    assert params.fts_config_for(None) == "simple"


def test_models_lock_pins_the_measured_revision_and_files() -> None:
    params = need("reader.search.params")
    lock = json.loads((REPO / "server" / "src" / "reader" / "search" / "models.lock.json").read_text())
    assert lock["repo"] == "BAAI/bge-m3"
    assert lock["revision"] == "5617a9f61b028005a4858fdac845db406aefb181"
    files = lock["files"]
    assert files["pytorch_model.bin"]["size"] == 2271145830
    assert {"config.json", "tokenizer.json", "modules.json", "1_Pooling/config.json"} <= set(files)
    assert all(len(f["sha256"]) == 64 for f in files.values())
    assert params.MODEL_ID == "BAAI/bge-m3@5617a9f61b028005a4858fdac845db406aefb181"


def test_job_kind_is_declared() -> None:
    jobs = need("reader.search.jobs")
    (kind,) = jobs.JOB_KINDS
    assert kind.name == "search.embed"
    assert kind.key == ("ingest_id", "model", "params_sha256")
    assert kind.max_attempts == 3 and kind.timeout_s == 3600
    errors = need("reader.search.errors")
    import asyncpg
    import httpx

    for cls in (
        asyncpg.exceptions.ConnectionDoesNotExistError, asyncpg.exceptions.CannotConnectNowError,
        ConnectionRefusedError, httpx.ConnectError, httpx.TimeoutException,
        errors.EmbedUnavailable, errors.IngestNotReady,
    ):
        assert cls in kind.transient, cls
    from reader.jobs.catalog import KIND_MODULES

    assert "reader.search.jobs" in KIND_MODULES


# ---- FR-SEARCH-9, VEC-SEARCH-4 ---------------------------------------------------------------------------


def test_status_map_matches_the_fixture() -> None:
    params = need("reader.search.params")
    for row in fixture("status-map.yaml")["rows"]:
        assert params.status_from(row["job"], row["embeds_row"]) == row["status"], row


# ---- FR-SEARCH-1, INV-SEARCH-8: the service protocol with a fake encoder --------------------------------


def _post(url: str, body: Any, raw: bytes | None = None) -> tuple[int, Any]:
    data = raw if raw is not None else json.dumps(body).encode()
    req = urllib.request.Request(
        url + "/embed", data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_embed_returns_unit_vectors_and_the_model(embed_server: EmbedServer) -> None:
    status, body = _post(embed_server.url, {"kind": "passage", "texts": ["one two", "три"]})
    assert status == 200
    assert body["dimensions"] == 1024 and len(body["vectors"]) == 2
    assert all(abs(sum(v * v for v in vec) - 1.0) < 1e-6 for vec in body["vectors"])
    params = need("reader.search.params")
    assert body["model"] == params.MODEL_ID


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"kind": "passage", "texts": ["x"] * 9}, "too_many_texts"),
        ({"kind": "passage", "texts": []}, "bad_request"),
        ({"kind": "passage", "texts": ["x" * 1201]}, "text_too_long"),
        ({"kind": "query", "texts": ["x" * 1001]}, "text_too_long"),
        ({"kind": "query", "texts": [""]}, "bad_request"),
        ({"kind": "other", "texts": ["x"]}, "bad_request"),
        ({"texts": ["x"]}, "bad_request"),
        ([1, 2], "bad_request"),
    ],
)
def test_embed_limits(embed_server: EmbedServer, body: Any, code: str) -> None:
    status, answer = _post(embed_server.url, body)
    assert (status, answer) == (400, {"error": code})


def test_embed_refuses_a_large_body_before_parsing(embed_server: EmbedServer) -> None:
    status, answer = _post(embed_server.url, None, raw=b"[" * (64 * 1024 + 1))
    assert (status, answer) == (413, {"error": "too_large"})
    assert embed_server.encoder.calls == []


def test_health_and_healthcheck_cli(embed_server: EmbedServer) -> None:
    with urllib.request.urlopen(embed_server.url + "/health", timeout=5) as r:
        assert r.status == 200
    svc = need("reader.search.embed_service")
    assert svc.healthcheck(embed_server.url) == 0
    assert svc.healthcheck("http://127.0.0.1:9") == 1


def test_health_is_503_before_the_model_loads() -> None:
    svc = need("reader.search.embed_service")
    params = need("reader.search.params")
    server = svc.make_server(None, "127.0.0.1", 0, model_id=params.MODEL_ID)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        assert svc.healthcheck(f"http://{host}:{port}") == 1
        status, answer = _post(f"http://{host}:{port}", {"kind": "query", "texts": ["x"]})
        assert (status, answer) == (503, {"error": "loading"})
    finally:
        server.shutdown()
        server.server_close()


def test_a_query_waits_at_most_one_batch() -> None:
    svc = need("reader.search.embed_service")
    params = need("reader.search.params")
    delay = 0.4
    encoder = HashEncoder(delay_s=delay)
    server = svc.make_server(encoder, "127.0.0.1", 0, model_id=params.MODEL_ID)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "http://{}:{}".format(*server.server_address[:2])
    done: dict[str, float] = {}

    def call(name: str, kind: str) -> None:
        _post(url, {"kind": kind, "texts": ["a"] * (8 if kind == "passage" else 1)})
        done[name] = time.monotonic()

    try:
        first = threading.Thread(target=call, args=("p1", "passage"))
        first.start()
        time.sleep(delay / 4)  # p1 is encoding
        start = time.monotonic()
        query = threading.Thread(target=call, args=("q", "query"))
        query.start()
        time.sleep(0.02)
        second = threading.Thread(target=call, args=("p2", "passage"))
        second.start()
        for t in (first, query, second):
            t.join(10)
        assert [k for k, _ in encoder.calls] == ["passage", "query", "passage"]
        assert done["q"] - start <= 2 * delay + 0.3
        assert done["q"] < done["p2"]
    finally:
        server.shutdown()
        server.server_close()


def test_embed_service_imports_no_config_or_db() -> None:
    code = (
        "import sys, reader.search.embed_service as m; "
        "bad = [n for n in sys.modules if n.startswith('reader.') and not n.startswith('reader.search')]; "
        "names = ('torch', 'sentence_transformers', 'sqlalchemy', 'fastapi'); "
        "heavy = [n for n in names if n in sys.modules]; "
        "print(bad, heavy)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False,
        cwd=REPO / "server", env={"PYTHONPATH": str(REPO / "server" / "src")},
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[] []"
