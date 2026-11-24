"""Upload pieces without services: magic check, filename header, the upload state table, no parser.

@trace VEC-INGEST-7 VEC-INGEST-6 FR-INGEST-18 INV-INGEST-8 FR-INGEST-24
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from .conftest import FIXTURES, SRC, need, pilot_pdf

READER = SRC / "reader"


def _yaml(name: str) -> Any:
    return YAML(typ="safe").load((FIXTURES / name).read_text(encoding="utf-8"))


def _body(case: dict[str, Any]) -> bytes:
    if "whole" in case:
        return str(case["whole"]).encode("ascii")
    head = bytes.fromhex(case["head_hex"]) if "head_hex" in case else str(case["head"]).encode("ascii")
    return head + b"x" * int(case["fill"]) + str(case["eof"]).encode("ascii") + b"\n" * int(case["after"])


MAGIC = _yaml("magic-cases.yaml")["cases"]


# ---- VEC-INGEST-7: the magic check ----


@pytest.mark.parametrize("case", MAGIC, ids=[c["name"] for c in MAGIC])
def test_magic_cases(case: dict[str, Any]) -> None:
    magic = need("reader.ingest.magic")
    body = _body(case)
    assert magic.is_pdf(body) is case["valid"]
    # the route reads only the first 8 and the last 1,024 bytes; the same answer from those two slices
    assert magic.check(body[:8], body[-1024:]) is case["valid"]


def test_magic_eof_positions_are_exact() -> None:
    body = _body({"head": "%PDF-1.7\n", "fill": 2000, "eof": "%%EOF", "after": 1019})
    assert need("reader.ingest.magic").is_pdf(body)  # %%EOF ends exactly at the window's first byte + 5


def test_magic_on_the_pilot_pdf() -> None:
    """[local] the pilot PDF passes the check (sha256-matched on the owner's Mac)."""
    path = pilot_pdf()
    if path is None:
        pytest.skip("[local]: READER_TEST_PILOT_PDF is not set to the sha256-matched pilot PDF")
    data = path.read_bytes()
    assert need("reader.ingest.magic").check(data[:8], data[-1024:])


def test_synthetic_fixture_pdf_passes() -> None:
    """The live check uploads VEC-INGEST-2's generator output; it must pass the magic check."""
    data = (FIXTURES / "synthetic.pdf").read_bytes()
    assert need("reader.ingest.magic").is_pdf(data)


# ---- FR-INGEST-18: the filename header ----


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("Book.pdf", "Book.pdf"),
        ("%D0%9A%D0%BD%D0%B8%D0%B3%D0%B0.pdf", "Книга.pdf"),
        ("a%2Fb%5Cc.pdf", "abc.pdf"),  # separators removed
        ("x%E2%80%AEfdp.exe", "xfdp.exe"),  # U+202E (bidi override) removed
        ("e%CC%81.pdf", "é.pdf"),  # NFC
        ("tab%09nul%00c1%C2%85.pdf", "tabnulc1.pdf"),  # C0, NUL and C1 removed
        ("%E2%81%A6x%E2%81%A9%D8%9Cy%E2%80%8E%E2%80%8Fz", "xyz"),  # U+2066..2069, U+061C, U+200E/F
        ("a" * 200, "a" * 200),
    ],
)
def test_filename_clean(raw: str, want: str) -> None:
    assert need("reader.ingest.filename").clean(raw) == want


@pytest.mark.parametrize(
    "raw",
    ["", "%", "%G0", "%FF.pdf", "%C3", "%2F%5C", "%E2%80%AE", "a" * 201, "%00", "%ED%A0%80"],
)
def test_filename_refused(raw: str) -> None:
    filename = need("reader.ingest.filename")
    with pytest.raises(filename.FilenameError):
        filename.clean(raw)


# ---- VEC-INGEST-6: the upload state table ----


STATES = _yaml("upload-states.yaml")["cases"]


@pytest.mark.parametrize("case", STATES, ids=[f"{i}-{c['row']}-{c['import']}" for i, c in enumerate(STATES)])
def test_upload_state_table(case: dict[str, Any]) -> None:
    uploads = need("reader.ingest.uploads")
    got = uploads.derive(
        row=case["row"],
        attempts=case["attempts"],
        expired=case["expired"],
        row_code=case.get("code"),
        import_status=case["import"],
        import_code=case.get("import_code"),
        ingest=case["ingest"],
        embed_status=case["embed"],
        embed_code=case.get("embed_code"),
    )
    assert got == (case["want"]["state"], case["want"]["code"], case["want"]["retry"])


# ---- INV-INGEST-8: no parser of an uploaded file in the stack ----

PARSERS = {"pypdfium2", "pypdf", "docling", "fitz", "pdfminer"}


def _stack_modules() -> list[Path]:
    host = READER / "ingest" / "host"
    return [p for p in sorted(READER.rglob("*.py")) if not p.is_relative_to(host)]


def _imports(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_no_pdf_parser_importable_by_api_or_worker() -> None:
    offenders = {
        str(p.relative_to(SRC)): sorted(_imports(ast.parse(p.read_text(encoding="utf-8"))) & PARSERS)
        for p in _stack_modules()
    }
    assert {k: v for k, v in offenders.items() if v} == {}
    # and nothing the api or worker can load imports the host package (which holds the converter); the host
    # CLI `reader` (reader/cli, OPS-CLI FR-CLI-23) runs on the Mac and may read its closure hash
    importers = [
        str(p.relative_to(SRC))
        for p in _stack_modules()
        if not p.is_relative_to(READER / "cli")
        if any(
            isinstance(n, ast.ImportFrom) and (n.module or "").startswith("reader.ingest.host")
            for n in ast.walk(ast.parse(p.read_text(encoding="utf-8")))
        )
    ]
    assert importers == []


OPENERS = {"open", "fdopen", "read_bytes", "read_text", "open_text", "open_binary"}
# (module, function) pairs allowed to open a file in a module that names the inbox: the body writer, the magic
# check's two reads (first 8 and last 1,024 bytes) and `inbox fetch`'s byte copy.
ALLOWED_OPENS = {
    ("reader/ingest/upload.py", "_write_body"),
    ("reader/ingest/upload.py", "_magic_of"),
    ("reader/ingest/inbox.py", "_copy_out"),
}


def _opens(tree: ast.Module) -> set[str]:
    found: set[str] = set()

    def visit(node: ast.AST, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else function
            if isinstance(child, ast.Call):
                func = child.func
                called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if called in OPENERS:
                    found.add(function)
            visit(child, name)

    visit(tree, "<module>")
    return found


def test_inbox_files_are_read_only_by_the_magic_check_and_fetch() -> None:
    naming = [p for p in _stack_modules() if "INBOX" in p.read_text(encoding="utf-8")]
    rel = {str(p.relative_to(SRC)) for p in naming}
    assert "reader/ingest/upload.py" in rel and "reader/ingest/inbox.py" in rel
    opened = {
        (str(p.relative_to(SRC)), fn)
        for p in naming
        for fn in _opens(ast.parse(p.read_text(encoding="utf-8")))
    }
    assert opened <= ALLOWED_OPENS, opened - ALLOWED_OPENS


# ---- FR-INGEST-24: the filename is never logged by the upload pieces ----


def test_filename_clean_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    need("reader.ingest.filename").clean("QUOKKAZEBRA-title.pdf")
    assert "QUOKKAZEBRA" not in caplog.text
