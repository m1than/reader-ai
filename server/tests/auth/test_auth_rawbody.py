"""FR-AUTH-22: the raw-body allow-list in the CSRF gate (one exact entry for the PDF upload).

@trace FR-AUTH-22
"""

from __future__ import annotations

import importlib
from typing import Any
from urllib.parse import unquote

import pytest

pytestmark = pytest.mark.anyio

ORIGIN = "https://localhost"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def through_gate(
    raw_path: bytes, method: str = "POST", content_type: str | None = "application/pdf", origin: bool = True
) -> tuple[int | None, bool, int]:
    """One HTTP request through the gate: (status sent or None, app reached, body messages received)."""
    from reader.api.app import CsrfGate

    reached: list[bool] = []
    sent: list[dict[str, Any]] = []
    reads = [0]
    headers = [(b"content-length", b"1000")]
    if origin:
        headers.append((b"origin", ORIGIN.encode()))
    if content_type is not None:
        headers.append((b"content-type", content_type.encode()))
    scope = {
        "type": "http",
        "method": method,
        "path": unquote(raw_path.decode("ascii")),  # what the server decodes; the gate must not trust it
        "raw_path": raw_path,
        "query_string": b"language=ru",
        "headers": headers,
    }

    async def app(scope: Any, receive: Any, send: Any) -> None:
        reached.append(True)

    async def receive() -> dict[str, Any]:
        reads[0] += 1
        return {"type": "http.request", "body": b"%PDF-1.7", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await CsrfGate(app, ORIGIN)(scope, receive, send)
    status = sent[0]["status"] if sent else None
    return status, bool(reached), reads[0]


def test_the_table_has_exactly_one_entry() -> None:
    csrf = importlib.import_module("reader.auth.csrf")
    assert tuple(csrf.RAW_BODY_ROUTES) == (("POST", "/api/materials/uploads", "application/pdf"),)


async def test_the_upload_route_takes_a_pdf_and_reads_no_body() -> None:
    assert await through_gate(b"/api/materials/uploads") == (None, True, 0)
    with_params = await through_gate(b"/api/materials/uploads", content_type="Application/PDF; x=1")
    assert with_params == (None, True, 0)


async def test_the_upload_route_keeps_the_origin_check() -> None:
    assert (await through_gate(b"/api/materials/uploads", origin=False))[:2] == (403, False)


@pytest.mark.parametrize(
    "content_type", ["text/plain", "application/json", "multipart/form-data; boundary=x", None]
)
async def test_the_upload_route_refuses_other_media_types(content_type: str | None) -> None:
    assert (await through_gate(b"/api/materials/uploads", content_type=content_type))[:2] == (415, False)


@pytest.mark.parametrize(
    "raw_path",
    [
        b"/api/lessons",
        b"/api/materials/uploads/x",
        b"/api/materials/uploads/",
        b"/api/materials/%75ploads",
        b"/api/materials%2Fuploads",
        b"/api//materials/uploads",
    ],
)
async def test_pdf_elsewhere_keeps_the_json_rule(raw_path: bytes) -> None:
    status, reached, reads = await through_gate(raw_path)
    assert (status, reached, reads) == (415, False, 0)


async def test_other_methods_on_the_upload_path_keep_the_json_rule() -> None:
    assert (await through_gate(b"/api/materials/uploads", method="PUT"))[:2] == (415, False)
    safe = await through_gate(b"/api/materials/uploads", method="GET")
    assert safe[:2] == (None, True)  # a safe method: the router answers
