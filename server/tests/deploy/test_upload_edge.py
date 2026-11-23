# @trace FR-DEPLOY-13
"""OPS-DEPLOY [config] FR-DEPLOY-13: the upload body limit at the edge and the image's converter source hash."""

import re

from .conftest import CADDYFILE, DEPLOY


def _block(text: str, opener: str) -> str:
    """The body of the brace block that starts at `opener`, matched brace by brace."""
    start = text.index(opener) + len(opener)
    depth, i = 1, start
    while depth:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return text[start : i - 1]


def test_one_request_body_limit_on_exactly_the_upload_path() -> None:
    text = CADDYFILE.read_text()
    assert text.count("request_body") == 1
    assert "read_body" not in text  # no server-wide body timeout: it would bound the WebSocket relays too
    handles = re.findall(r"^\s*handle\s+(\S+)\s*\{", text, flags=re.MULTILINE)
    assert "/api/materials/uploads" in handles
    assert handles.index("/api/materials/uploads") < handles.index("/api/*")
    upload = _block(text, "handle /api/materials/uploads {")
    assert re.search(r"request_body\s*\{\s*max_size\s+200MiB\s*\}", upload)
    api = _block(text, "handle /api/* {")
    proxy = re.search(r"reverse_proxy\s+(\S+)", api)
    assert proxy is not None
    assert re.search(rf"reverse_proxy\s+{re.escape(proxy.group(1))}\s", upload)


def test_server_image_writes_the_converter_source_hash() -> None:
    text = (DEPLOY / "images" / "server.Dockerfile").read_text()
    run = next((ln for ln in text.splitlines() if "converter_source_sha256" in ln), "")
    assert "reader.ingest.host.closure" in run and "source_sha256" in run
    assert "/opt/reader/src" in run and "/usr/local/share/reader/converter_source_sha256" in run
    assert run.lstrip().startswith("RUN ")
    assert text.index("converter_source_sha256") < text.index("USER 10001")
