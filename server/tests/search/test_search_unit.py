"""SRV-SEARCH [unit]: chunking, fusion, query quoting, parameters, status map, the embed service protocol.

@trace FR-SEARCH-1 FR-SEARCH-2 FR-SEARCH-3 FR-SEARCH-5 FR-SEARCH-7 FR-SEARCH-9
@trace INV-SEARCH-6 INV-SEARCH-8 VEC-SEARCH-1 VEC-SEARCH-2 VEC-SEARCH-4
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import itertools
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Sequence
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from .conftest import REPO, EmbedServer, HashEncoder, fixture, need

# ---- FR-SEARCH-3, VEC-SEARCH-2 ----------------------------------------------------------------------------


def test_windows_match_the_fixture() -> None:
    chunking = need("reader.search.chunking")
    data = fixture("chunking.yaml")
    p = data["params"]
    for case in data["cases"]:
        got = chunking.windows(case["text"], p["size"], p["overlap"], p["min"], prefix=case.get("prefix"))
        assert [{"window_no": x.window_no, "start": x.char_start, "stop": x.char_stop, "passage": x.passage}
                for x in got] == case["expected"], case["name"]


def test_window_production_parameters_are_the_spikes() -> None:
    params = need("reader.search.params")
    chunking = need("reader.search.chunking")
    assert (params.CHUNK_SIZE, params.CHUNK_OVERLAP, params.CHUNK_MIN) == (1200, 200, 80)
    (short,) = chunking.windows("x" * 79, 1200, 200, 80)  # a span under 80 characters is one window
    assert (short.char_start, short.char_stop) == (0, 79)
    prefixed = chunking.windows("y " * 2000, 1200, 200, 80, prefix="Глава 1 / Раздел 2")
    assert len(prefixed) > 3 and all(len(x.passage) <= 1200 for x in prefixed)
    assert chunking.windows(" \n\t", 1200, 200, 80) == []


@given(st.text(alphabet="ab \nц", max_size=3000), st.sampled_from([None, "", "Ch 1", "Глава " * 20]))
def test_windows_cover_the_span_and_stay_within_the_size(body: str, prefix: str | None) -> None:
    chunking = need("reader.search.chunking")
    got = chunking.windows(body, 1200, 200, 80, prefix=prefix)
    assert got == chunking.windows(body, 1200, 200, 80, prefix=prefix)
    if not body.strip():
        assert got == []
        return
    assert got[0].char_start == 0 and got[-1].char_stop == len(body)
    for before, after in itertools.pairwise(got):
        assert after.char_start < before.char_stop  # overlapping: no gap
    head = "" if prefix is None else prefix + "\n"
    for x in got:
        assert len(x.passage) <= 1200 and x.passage == head + body[x.char_start : x.char_stop]


# ---- FR-SEARCH-7, INV-SEARCH-6, VEC-SEARCH-1 ---------------------------------------------------------------


def test_rrf_matches_the_fixture() -> None:
    fusion = need("reader.search.fusion")
    data = fixture("rrf.yaml")
    policy = need("reader.search.policy").load().data
    params = need("reader.search.params")
    assert (policy["rrf_k"], policy["w_dense"]) == (data["k"], params.W_DENSE) == (fusion.RRF_K, 5)
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
    jobs = need("reader.search.jobs")
    inputs = jobs.vector_inputs("russian")
    assert inputs == {"model": params.MODEL_ID, "params_sha256": params.params_sha256("russian"),
                      "prefix_overlay_id": None}
    assert jobs.BUILDER == "search.vectors/1"
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
    assert kind.key == ("owner_id", "overlay_id")
    assert set(kind.args.model_fields) == {"owner_id", "pack_id", "overlay_id"}
    assert kind.max_attempts == 3 and kind.timeout_s == 3600
    errors = need("reader.search.errors")
    import asyncpg
    import httpx

    for cls in (
        asyncpg.exceptions.ConnectionDoesNotExistError, asyncpg.exceptions.CannotConnectNowError,
        ConnectionRefusedError, httpx.TransportError,
        errors.EmbedUnavailable, errors.IngestNotReady,
    ):
        assert cls in kind.transient, cls
    for cls in (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException):  # covered by TransportError
        assert issubclass(cls, kind.transient), cls
    from reader.jobs.catalog import KIND_MODULES

    assert "reader.search.jobs" in KIND_MODULES


# ---- FR-SEARCH-9, VEC-SEARCH-4 ---------------------------------------------------------------------------


def test_status_map_matches_the_fixture() -> None:
    params = need("reader.search.params")
    rows = fixture("status-map.yaml")["rows"]
    assert {(r["job"], r["overlay"]) for r in rows} == {
        (j, o) for j in ("none", "queued", "running", "succeeded", "dead")
        for o in ("none", "building", "sealed", "active", "collected")
    }
    for row in rows:
        assert params.status_from(row["job"], row["overlay"]) == row["status"], row


# ---- FR-SEARCH-7: the compatibility Hit against SRV-TUTOR's Protocol ---------------------------------------


def test_hit_satisfies_the_tutor_protocol() -> None:
    store = need("reader.search.store")
    from typing import get_type_hints

    from reader.tutor import jobs as tutor_jobs

    loc = "spn:" + "a" * 64
    hits = [store.Hit(material_sha256="a" * 64, page=1, span_id=f"{loc}:p1:0-{n}",
                      span_ids=[f"{loc}:p1:0-{n}"], snippet="s", score=1.0 - n / 10, dense_rank=n,
                      fts_rank=None)
            for n in (3, 1, 2)]
    protocol = {name for name in vars(tutor_jobs.Hit) if not name.startswith("_")}
    assert protocol == {"span_id", "span_ids"}
    hints = get_type_hints(store.Hit)
    assert hints["span_id"] is str and hints["span_ids"] == list[str]
    assert tutor_jobs.candidate_span_ids(hits) == [h.span_id for h in hits]  # locators in rank order


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


def test_encoder_is_never_entered_twice_at_once() -> None:
    svc = need("reader.search.embed_service")
    params = need("reader.search.params")
    encoder = HashEncoder(delay_s=0.05)
    server = svc.make_server(encoder, "127.0.0.1", 0, model_id=params.MODEL_ID)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "http://{}:{}".format(*server.server_address[:2])
    try:
        callers = [
            threading.Thread(target=_post, args=(url, {"kind": kind, "texts": ["a b"] * 3}))
            for kind in ("passage", "query") * 4
        ]
        for t in callers:
            t.start()
        for t in callers:
            t.join(10)
        assert len(encoder.calls) == 8
        assert encoder.max_active == 1
    finally:
        server.shutdown()
        server.server_close()


# ---- FR-SEARCH-2: fetch_model verifies sha256 and size --------------------------------------------------


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def file_server(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    root = tmp_path / "srv"
    root.mkdir()
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(root)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield "http://{}:{}".format(*server.server_address[:2]), root
    finally:
        server.shutdown()
        server.server_close()


def test_fetch_model_verifies_hash_and_size(
    file_server: tuple[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetch_model = need("reader.search.fetch_model")
    base, root = file_server
    body = b"synthetic model bytes"
    served = root / "org" / "model" / "resolve" / "rev1" / "sub" / "w.bin"
    served.parent.mkdir(parents=True)
    served.write_bytes(body)
    monkeypatch.setattr(fetch_model, "BASE", base)
    good = {"sha256": hashlib.sha256(body).hexdigest(), "size": len(body)}

    def lock(meta: dict[str, Any]) -> dict[str, Any]:
        return {"repo": "org/model", "revision": "rev1", "files": {"sub/w.bin": meta}}

    fetch_model.fetch(tmp_path / "ok", lock(good))
    assert (tmp_path / "ok" / "sub" / "w.bin").read_bytes() == body
    for name, meta in (("sha", {**good, "sha256": "0" * 64}), ("size", {**good, "size": len(body) + 1})):
        with pytest.raises(SystemExit):
            fetch_model.fetch(tmp_path / name, lock(meta))
        assert not (tmp_path / name / "sub" / "w.bin").exists(), name


# ---- FR-SEARCH-1: the client's error mapping --------------------------------------------------------------


def _fixed_server(status: int, body: bytes) -> ThreadingHTTPServer:
    class Fixed(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixed)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class _ShortEncoder:
    def encode(self, kind: str, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]


def _client_error(monkeypatch: pytest.MonkeyPatch, url: str) -> Exception:
    client = need("reader.search.embed_client")
    monkeypatch.setenv("READER_EMBED_URL", url)
    try:
        asyncio.run(client.embed("query", ["x"], 5.0))
    except Exception as error:
        return error
    raise AssertionError("the call succeeded")


def test_client_maps_service_failures(embed_server: EmbedServer, monkeypatch: pytest.MonkeyPatch) -> None:
    errors = need("reader.search.errors")
    embed_server.server.encoder = None  # 503 loading
    assert isinstance(_client_error(monkeypatch, embed_server.url), errors.EmbedUnavailable)
    embed_server.server.encoder = _ShortEncoder()  # 3-dimensional vectors
    error = _client_error(monkeypatch, embed_server.url)
    assert isinstance(error, errors.SearchError)
    assert error.code == "model_mismatch"
    assert isinstance(_client_error(monkeypatch, "http://127.0.0.1:9"), errors.EmbedUnavailable)
    for status, body, expected in (
        (200, b"not json", errors.EmbedUnavailable),
        (500, b"{}", errors.EmbedUnavailable),
        (418, b"{}", errors.SearchError),
    ):
        server = _fixed_server(status, body)
        try:
            url = "http://{}:{}".format(*server.server_address[:2])
            assert isinstance(_client_error(monkeypatch, url), expected), status
        finally:
            server.shutdown()
            server.server_close()
