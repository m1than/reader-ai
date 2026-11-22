"""The embed service (SRV-SEARCH FR-SEARCH-1, ADR-0028): bge-m3 dense vectors over HTTP, one encode at a time.

Runs as `python -I -m reader.search.embed_service` in the reader-embed image, whose site-packages hold only
this package's `__init__.py`, this file, fetch_model.py and models.lock.json. It imports nothing from
`reader` but `reader.search`, and torch and sentence-transformers only inside `load_encoder()`; the HTTP
server is the standard library's. Requests wait in one FIFO queue served by one worker thread, so a query that
arrives during a passage batch waits at most for that batch. Logs carry counts and durations, never a text.
"""

from __future__ import annotations

import argparse
import json
import logging
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from concurrent.futures import Future
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final, Protocol

LOCK_FILE: Final = Path(__file__).resolve().parent / "models.lock.json"
MODEL_DIR: Final = Path("/opt/models/bge-m3")
PORT: Final = 8100
MAX_TEXTS: Final = 8  # spikes/embed-service: 2,410 MiB peak RSS for 8 worst texts, so mem_limit 3g
MAX_PASSAGE_CHARS: Final = 1200  # the chunk size (FR-SEARCH-3)
MAX_QUERY_CHARS: Final = 1000  # the route's limit (FR-SEARCH-8)
MAX_BODY_BYTES: Final = 64 * 1024  # 8 texts of 1,200 chars, 6 bytes each (\uXXXX): 57,600 plus framing
MAX_SEQ_LENGTH: Final = 1024  # measured token counts: prose 266, Rust 603, hex 740, LaTeX 887 per 1,200 chars
DIMENSIONS: Final = 1024
HEALTHCHECK_TIMEOUT_S: Final = 2.0
KINDS: Final = frozenset({"passage", "query"})

log = logging.getLogger("reader.search.embed")


def model_id() -> str:
    lock: dict[str, Any] = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    return f"{lock['repo']}@{lock['revision']}"


class Encoder(Protocol):
    def encode(self, kind: str, texts: Sequence[str]) -> list[list[float]]: ...


class UnsafeCheckpoint(Exception):
    """A checkpoint holds a pickle global outside torch's weights_only allow-list."""


def load_checkpoint(path: str | Path) -> dict[str, Any]:
    """Load a state dict with `weights_only=True`; any refused global raises UnsafeCheckpoint."""
    import pickle

    import torch

    try:
        state: dict[str, Any] = torch.load(str(path), map_location="cpu", weights_only=True)
    except pickle.UnpicklingError as error:
        raise UnsafeCheckpoint("unsafe_checkpoint") from error
    return state


class SentenceEncoder:
    """bge-m3 through sentence-transformers: CPU, fp32, max_seq_length 1,024, normalised, no prefixes."""

    def __init__(self, directory: Path) -> None:
        # refuse an unsafe checkpoint before the library reads it
        load_checkpoint(directory / "pytorch_model.bin")
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(str(directory), device="cpu")
        self.model.max_seq_length = MAX_SEQ_LENGTH

    def encode(self, kind: str, texts: Sequence[str]) -> list[list[float]]:
        del kind  # bge-m3 needs no query or passage instruction (REF-SEARCH-3)
        vectors = self.model.encode(list(texts), batch_size=MAX_TEXTS, normalize_embeddings=True)
        return [[float(x) for x in row] for row in vectors]


def load_encoder(directory: Path = MODEL_DIR) -> Encoder:
    return SentenceEncoder(directory)


class _Job:
    def __init__(self, kind: str, texts: list[str]) -> None:
        self.kind, self.texts = kind, texts
        self.result: Future[list[list[float]]] = Future()


class EmbedServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], encoder: Encoder | None, model: str) -> None:
        super().__init__(address, _Handler)
        self.encoder = encoder
        self.model_id = model
        self.jobs: queue.SimpleQueue[_Job] = queue.SimpleQueue()
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self) -> None:
        while True:
            job = self.jobs.get()
            started = time.monotonic()
            try:
                assert self.encoder is not None  # noqa: S101 - requests are refused until it is set
                job.result.set_result(self.encoder.encode(job.kind, job.texts))
            except Exception as error:  # the type only is logged, never a text
                log.warning("encode_failed %s", type(error).__name__)
                job.result.set_exception(error)
            else:
                ms = (time.monotonic() - started) * 1000
                log.info("encoded kind=%s n=%d ms=%d", job.kind, len(job.texts), ms)


def _validate(body: Any) -> tuple[str, list[str]] | str:
    if not isinstance(body, dict) or set(body) != {"kind", "texts"}:
        return "bad_request"
    kind, texts = body["kind"], body["texts"]
    if kind not in KINDS or not isinstance(texts, list) or not texts:
        return "bad_request"
    if len(texts) > MAX_TEXTS:
        return "too_many_texts"
    if not all(isinstance(t, str) and t for t in texts):
        return "bad_request"
    limit = MAX_PASSAGE_CHARS if kind == "passage" else MAX_QUERY_CHARS
    if any(len(t) > limit for t in texts):
        return "text_too_long"
    return kind, texts


class _Handler(BaseHTTPRequestHandler):
    server: EmbedServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        """No access log: a request line never carries a text, but nothing is logged per request here."""

    def _send(self, status: int, payload: Any) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path != "/health":
            self._send(404, {"error": "not_found"})
        elif self.server.encoder is None:
            self._send(503, {"error": "loading"})
        else:
            self._send(200, {"model": self.server.model_id})

    def do_POST(self) -> None:
        if self.path != "/embed":
            self._send(404, {"error": "not_found"})
            return
        length = self.headers.get("Content-Length", "")
        if not length.isdigit():
            self.close_connection = True
            self._send(411, {"error": "length_required"})
            return
        if int(length) > MAX_BODY_BYTES:
            self.close_connection = True
            self._send(413, {"error": "too_large"})
            return
        raw = self.rfile.read(int(length))
        if self.server.encoder is None:
            self._send(503, {"error": "loading"})
            return
        try:
            body = json.loads(raw)
        except ValueError, RecursionError:
            self._send(400, {"error": "bad_request"})
            return
        checked = _validate(body)
        if isinstance(checked, str):
            self._send(400, {"error": checked})
            return
        job = _Job(*checked)
        self.server.jobs.put(job)
        try:
            vectors = job.result.result()
        except Exception:
            self._send(500, {"error": "encode_failed"})
            return
        self._send(200, {"model": self.server.model_id, "dimensions": DIMENSIONS, "vectors": vectors})


def make_server(encoder: Encoder | None, host: str, port: int, model_id: str) -> EmbedServer:
    return EmbedServer((host, port), encoder, model_id)


def healthcheck(base_url: str = f"http://127.0.0.1:{PORT}") -> int:
    """0 when GET /health answers 200 within 2 s, else 1 (the compose healthcheck)."""
    try:
        with urllib.request.urlopen(base_url + "/health", timeout=HEALTHCHECK_TIMEOUT_S) as response:  # noqa: S310
            return 0 if response.status == 200 else 1
    except urllib.error.URLError, OSError:
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reader.search.embed_service")
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args(argv)
    if args.healthcheck:
        return healthcheck()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    server = make_server(None, "0.0.0.0", PORT, model_id())  # noqa: S104 - the `embed` network only (ADR-0028)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        server.encoder = load_encoder()
    except UnsafeCheckpoint:
        log.error("unsafe_checkpoint")
        return 1
    log.info("ready model=%s", server.model_id)
    threading.Event().wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
