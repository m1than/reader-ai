"""The real converter under the real profile ([host]) and on the pilot PDF ([local]) (SRV-INGEST).

@trace VEC-INGEST-2 VEC-INGEST-3 VEC-INGEST-4 INV-INGEST-4 INV-INGEST-5 FR-INGEST-5 FR-INGEST-7 FR-INGEST-8

[host] needs READER_TEST_INGEST_HOST=1 and READER_TEST_INGEST_DATA=<the data_dir that --setup used>.
[local] also needs READER_TEST_PILOT_PDF=<the sha256-matched pilot PDF>; without it the tests report a skip.
No book text is printed or compared: only counts, page numbers and sha256 values.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from .conftest import CANARY, FIXTURES, HOST, need, pilot_pdf

HERE = Path(__file__).resolve().parent


def data_dir() -> Path:
    if not HOST:
        pytest.skip("[host]: set READER_TEST_INGEST_HOST=1 (and READER_TEST_INGEST_DATA)")
    raw = os.environ.get("READER_TEST_INGEST_DATA")
    if not raw:
        pytest.skip("[host]: READER_TEST_INGEST_DATA is not set")
    return Path(raw)


def convert(pdf: Path, work: Path) -> tuple[Path, list[str]]:
    cli = need("reader.ingest.host.cli")
    deps = cli.real_deps(data_dir())
    work.mkdir(parents=True)
    (work / "in").mkdir()
    shutil.copyfile(pdf, work / "in" / "material.pdf")
    progress: list[str] = []
    out = cli.convert(work, deps, progress.append)
    return out, progress


def spans_of(out: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (out / "spans.jsonl").read_bytes().splitlines()]


# ---- VEC-INGEST-2: the generator is deterministic (runs everywhere) ----


def test_fixture_pdf_regenerates_byte_for_byte() -> None:
    sys.path.insert(0, str(HERE))
    try:
        import make_fixture_pdf
    finally:
        sys.path.remove(str(HERE))
    assert make_fixture_pdf.build() == (FIXTURES / "synthetic.pdf").read_bytes()
    expected = json.loads((FIXTURES / "synthetic.expected.json").read_text())
    assert expected == make_fixture_pdf.expected((FIXTURES / "synthetic.pdf").read_bytes())


# ---- VEC-INGEST-2 [host]: the real converter on the synthetic PDF ----


@pytest.mark.ingest_host
def test_synthetic_pdf_converts_as_expected(tmp_path: Path) -> None:
    artifact = need("reader.ingest.artifact")
    expected = json.loads((FIXTURES / "synthetic.expected.json").read_text())
    out, progress = convert(FIXTURES / "synthetic.pdf", tmp_path / "w")
    manifest = artifact.validate_dir(out)
    assert manifest["page_count"] == manifest["pages_converted"] == expected["page_count"]
    assert manifest["material_sha256"] == expected["pdf_sha256"]
    assert progress[-2:] == [
        "page 2/2",
        "code boxes 1, indentation fallbacks 0, majority-cell boxes 0, multi-box code items 0",  # FR-INGEST-5
    ]
    spans = spans_of(out)
    headings = [s for s in spans if s["kind"] == "heading"]
    assert [h["text"] for h in headings] == expected["headings"]
    assert all(1 <= h["level"] <= expected["heading_levels_at_most"] for h in headings)
    (code,) = [s for s in spans if s["kind"] == "code"]
    assert code["text"].split("\n") == expected["code_lines"]
    steps = [s for s in spans if s["kind"] == "list_item"]
    before, after = expected["code_between"]
    step1 = next(s for s in steps if s["text"].startswith(before))
    step2 = next(s for s in steps if s["text"].startswith(after))
    assert step1["seq"] < code["seq"] < step2["seq"]
    (table,) = [s for s in spans if s["kind"] == "table"]
    rows = [r for r in table["text"].split("\n") if r.startswith("|") and not set(r) <= set("|-: ")]
    assert [len(rows), rows[0].count("|") - 1] == expected["table_shape"]
    outline = json.loads((out / "outline.json").read_bytes())
    assert [{k: o[k] for k in ("level", "title", "page")} for o in outline] == expected["outline"]
    assert manifest["outline_source"] == expected["outline_source"]
    texts = [s["text"] for s in spans]
    for furniture in expected["furniture_dropped"]:
        assert not any(furniture in t for t in texts)
    assert sum(CANARY in t for t in texts) == 1
    assert all("­" not in s["text"] for s in spans if s["kind"] != "code")


# ---- INV-INGEST-5 [host]: the real child, through reader-ingest's own output ----


@pytest.mark.ingest_host
def test_real_child_output_never_reaches_the_terminal(tmp_path: Path) -> None:
    cli = need("reader.ingest.host.cli")
    deps = cli.real_deps(data_dir())
    work = tmp_path / "w"
    work.mkdir()
    (work / "in").mkdir()
    shutil.copyfile(FIXTURES / "synthetic.pdf", work / "in" / "material.pdf")
    terminal = io.StringIO()
    cli.convert(work, deps, lambda line: terminal.write(line + "\n"))
    assert CANARY not in terminal.getvalue()


# ---- [local] the pilot: VEC-INGEST-3, VEC-INGEST-4, INV-INGEST-4 ----


def pilot() -> Path:
    data_dir()
    path = pilot_pdf()
    if path is None:
        pytest.skip("[local]: READER_TEST_PILOT_PDF is not the sha256-matched pilot PDF")
    return path


def extract(pdf: Path, pages: list[int], dest: Path) -> Path:
    """The given 1-based pages of `pdf` as a new PDF (pypdfium2 from the converter venv)."""
    venv_python = need("reader.ingest.host.setup").venv_dir(data_dir()) / "bin" / "python"
    code = (
        "import sys, pypdfium2 as p\n"
        "src = p.PdfDocument(sys.argv[1]); out = p.PdfDocument.new()\n"
        "out.import_pages(src, [int(x) - 1 for x in sys.argv[3].split(',')]); out.save(sys.argv[2])\n"
    )
    subprocess.run(
        [str(venv_python), "-I", "-c", code, str(pdf), str(dest), ",".join(map(str, pages))], check=True
    )
    return dest


def _inside(span: dict[str, Any], box: list[float]) -> bool:
    """The span's vertical centre lies in the recorded box (boxes are top-left origin, in points)."""
    centre = (span["bbox"][1] + span["bbox"][3]) / 2
    return box[1] <= centre <= box[3]


def order_failures(page: int, got: list[dict[str, Any]], want: dict[str, Any]) -> list[str]:
    """ADR-0021, relational: each recorded code block comes after the step before it and before the next one;
    steps are the list items inside the hand-recorded step boxes, other list items do not count."""
    steps: dict[str, int] = {}
    for number, box in want["steps"].items():
        found = [s["seq"] for s in got if s["kind"] == "list_item" and _inside(s, box)]
        if len(found) != 1:
            return [f"page {page}: step {number} matched {len(found)} list items"]
        steps[number] = found[0]
    failures = []
    for index, after, before in want["order"]:
        box = want["code"][index]["box"]
        found = [s["seq"] for s in got if s["kind"] == "code" and _inside(s, box)]
        if len(found) != 1:
            failures.append(f"page {page}: code block {index} matched {len(found)} code spans")
            continue
        if not (steps[after] < found[0] and (before is None or found[0] < steps[before])):
            failures.append(f"page {page}: code block {index} is not between steps {after} and {before}")
    return failures


@pytest.mark.ingest_host
def test_pilot_page_expectations(tmp_path: Path) -> None:
    path = pilot()
    artifact = need("reader.ingest.artifact")
    expectations = YAML(typ="safe").load((FIXTURES / "pilot-expectations.yaml").read_text())
    pages = sorted(int(p) for p in expectations["pages"])
    sample = extract(path, pages, tmp_path / "sample.pdf")
    out, _ = convert(sample, tmp_path / "w")
    spans = spans_of(out)
    by_page: dict[int, list[dict[str, Any]]] = {}
    for span in spans:
        by_page.setdefault(pages[span["page"] - 1], []).append(span)
    failures: list[str] = []
    for page, want in sorted(expectations["pages"].items(), key=lambda kv: int(kv[0])):
        got = by_page.get(int(page), [])
        code = [s for s in got if s["kind"] == "code"]
        if "code" in want:
            hashes = [
                hashlib.sha256(artifact.normalise(s["text"], code=True).encode()).hexdigest() for s in code
            ]
            lines = [len(s["text"].split("\n")) for s in code]
            if [b["sha256"] for b in want["code"]] != hashes or [b["lines"] for b in want["code"]] != lines:
                failures.append(f"page {page}: code blocks {lines} != {[b['lines'] for b in want['code']]}")
        if "formulas_decoded" in want:
            decoded = sum(1 for s in got if s["kind"] == "formula" and s["decoded"])
            if decoded != want["formulas_decoded"]:
                failures.append(f"page {page}: formulas {decoded} != {want['formulas_decoded']}")
        if "tables" in want:
            shapes = []
            for s in got:
                if s["kind"] == "table":
                    rows = [
                        r for r in s["text"].split("\n") if r.startswith("|") and not set(r) <= set("|-: ")
                    ]
                    shapes.append([len(rows), rows[0].count("|") - 1 if rows else 0])
            if shapes != want["tables"]:
                failures.append(f"page {page}: tables {shapes} != {want['tables']}")
        if "order" in want:
            failures.extend(order_failures(int(page), got, want))
        counted = [(i, want["code"][int(i)]["box"], n) for i, n in want.get("indents", {}).items()]
        counted += [(f"box{k}", b["box"], b["indents"]) for k, b in enumerate(want.get("indent_boxes", []))]
        for index, box, counts in counted:  # FR-INGEST-7, counted by eye
            found = [s for s in code if _inside(s, box)]
            measured = (
                [len(line) - len(line.lstrip(" ")) for line in found[0]["text"].split("\n")]
                if len(found) == 1
                else None
            )
            if measured != counts:
                failures.append(f"page {page}: block {index} indents {measured} != {counts}")
    known = set(expectations.get("known_misses", []))
    unexpected = [f for f in failures if int(f.split()[1].rstrip(":")) not in known]
    assert not unexpected, unexpected
    # Page 468's Solidity block must keep more than one indentation level (owner, 2026-11-23).
    assert len(set(expectations["pages"]["468"]["indents"]["3"])) > 1
    # The hand-counted blocks are all present: a dropped key must not silently skip its check.
    hand_counted = {(p, b) for p, w in expectations["pages"].items() for b in w.get("indents", {})}
    for p, w in expectations["pages"].items():
        hand_counted |= {(p, f"box{k}") for k in range(len(w.get("indent_boxes", [])))}
    assert hand_counted == {("133", "2"), ("419", "box0"), ("461", "0"), ("468", "0"), ("468", "3")}


@pytest.mark.ingest_host
def test_pilot_outline(tmp_path: Path) -> None:
    path = pilot()
    want = YAML(typ="safe").load((FIXTURES / "pilot-outline.yaml").read_text())
    venv_python = need("reader.ingest.host.setup").venv_dir(data_dir()) / "bin" / "python"
    code = (
        "import json, sys\n"
        "from reader.ingest.host.convert import read_outline\n"
        "print(json.dumps(read_outline(sys.argv[1]), ensure_ascii=False))\n"
    )
    done = subprocess.run(
        [str(venv_python), "-I", "-c", code, str(path)], capture_output=True, check=True
    )
    outline = json.loads(done.stdout)
    pairs = json.dumps([[o["level"], o["page"]] for o in outline], separators=(",", ":")).encode()
    titles = json.dumps([o["title"] for o in outline], ensure_ascii=False, separators=(",", ":")).encode()
    assert len(outline) == want["entries"]
    assert sum(1 for o in outline if o["level"] == 1) == want["level1"]
    assert max(o["level"] for o in outline) == want["levels"]
    assert hashlib.sha256(pairs).hexdigest() == want["level_page_sha256"]
    assert hashlib.sha256(titles).hexdigest() == want["titles_sha256"]


PROBES = r"""
import ctypes, json, os, socket, subprocess, sys
results = {}
def attempt(name, fn):
    try:
        fn(); results[name] = "allowed"
    except Exception as e:
        results[name] = type(e).__name__
def tcp(host, port):
    s = socket.create_connection((host, port), timeout=3); s.close()
def bind_tcp():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(); s.close()
def unix(path):
    s = socket.socket(socket.AF_UNIX); s.connect(path); s.close()
def bind_unix():
    s = socket.socket(socket.AF_UNIX); s.bind(os.path.join(os.getcwd(), "u.sock")); s.close()
def lookup(name):
    lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    port = ctypes.c_uint32()
    bp = ctypes.c_uint32.in_dll(lib, "bootstrap_port")
    kr = lib.bootstrap_look_up(bp, name.encode(), ctypes.byref(port))
    if kr != 0:
        raise PermissionError(kr)
def run(path):
    subprocess.run([path, "--version"], capture_output=True, timeout=10)
def read(path):
    open(path, "rb").read(1)
def write(path):
    open(path, "wb").write(b"x")
cfg = json.loads(sys.argv[1])
attempt("tcp_external", lambda: tcp("1.1.1.1", 443))
attempt("tcp_loopback", lambda: tcp("127.0.0.1", cfg["port"]))
attempt("tcp_host_docker_internal", lambda: tcp("host.docker.internal", 443))
attempt("bind_tcp", bind_tcp)
attempt("bind_unix", bind_unix)
for sock in cfg["docker_sockets"]:
    attempt("unix " + sock, lambda s=sock: unix(s))
attempt("mach SecurityServer", lambda: lookup("com.apple.SecurityServer"))
attempt("mach launchservicesd", lambda: lookup("com.apple.coreservices.launchservicesd"))
for binary in ("/usr/bin/security", "/bin/sh", "/usr/local/bin/docker"):
    attempt("exec " + binary, lambda b=binary: run(b))
attempt("signal parent", lambda: os.kill(cfg["parent"], 0))
attempt("open /dev/tty", lambda: read("/dev/tty"))
attempt("read home canary", lambda: read(cfg["home_canary"]))
attempt("read data canary", lambda: read(cfg["data_canary"]))
attempt("read original material", lambda: read(cfg["material"]))
attempt("read /etc/passwd", lambda: read("/etc/passwd"))
attempt("list home", lambda: os.listdir(cfg["home"]))
attempt("write outside", lambda: write(cfg["outside"]))
def semaphore():
    import multiprocessing
    multiprocessing.Semaphore(1)
def shm():
    from multiprocessing import shared_memory
    shared_memory.SharedMemory(create=True, size=16).unlink()
attempt("posix semaphore", semaphore)
attempt("posix shm", shm)
results["env"] = sorted(os.environ)
results["canary_env"] = "CANARY_ENV" in os.environ
print(json.dumps(results))
"""


@pytest.mark.ingest_host
def test_cross_process_probes(tmp_path: Path) -> None:
    """INV-INGEST-4 (profile edits): a confined child learns nothing about another confined process."""
    sandbox = need("reader.ingest.host.sandbox")
    setup = need("reader.ingest.host.setup")
    data = data_dir()
    root = Path(os.path.realpath(tmp_path))
    work_a, work_b = root / "a" / "w", root / "b" / "w"
    for w in (work_a, work_b):
        for sub in ("in", "home", "tmp"):
            (w / sub).mkdir(parents=True)
    probe = work_a / "cross_process_probe.py"
    probe.write_text((HERE / "cross_process_probe.py").read_text())
    venv = setup.venv_dir(data)

    def confined(work: Path, tail: list[str]) -> list[str]:
        argv = sandbox.child_argv(venv, setup.pyroot_of(venv), setup.models_dir(data), work)
        python_at = max(i for i, a in enumerate(argv) if a == "-D") + 2
        return [*argv[: python_at + 1], "-I", *tail]

    other = subprocess.Popen(
        confined(work_b, ["-c", "import time; time.sleep(30)", "--input", f"{work_b}/in/material.pdf"]),
        env=sandbox.child_env(work_b, 1),
        cwd=work_b,
        start_new_session=True,
    )
    try:
        time.sleep(1.5)
        cfg = {
            "target": other.pid,
            "work_b": str(work_b),
            "work_b_dev": os.stat(work_b).st_dev,
            "work_b_ino": os.stat(work_b).st_ino,
        }
        done = subprocess.run(
            confined(work_a, [str(probe), json.dumps(cfg)]),
            env=sandbox.child_env(work_a, 1),
            cwd=work_a,
            capture_output=True,
            timeout=60,
            check=False,
        )
        target_alive = other.poll() is None
    finally:
        other.kill()
        other.wait()
    assert done.returncode == 0, done.stderr[-500:]
    assert target_alive
    results = json.loads(done.stdout)
    assert set(results["other"]) == {
        "kern.procargs2",
        "kern.procargs",
        "kern.proc.pid",
        "kern.proc.pgrp",
        "kern.proc.tty",
        "kern.proc.uid",
        "kern.proc.ruid",
        "kern.proc.all",
        "proc_pidpath",
        "proc_name",
        "proc_pid_rusage",
        "proc_pidfdinfo",
        "PROC_PIDLISTFDS",
        "PROC_PIDTBSDINFO",
        "PROC_PIDVNODEPATHINFO",
        "proc_listallpids",
        "list WORK parent",
    }
    assert all(v == "denied:errno 1" for v in results["other"].values()), results["other"]
    controls = {"kern.procargs2", "kern.proc.pid", "proc_pidpath", "PROC_PIDVNODEPATHINFO"}
    assert set(results["self"]) == controls
    assert all(v.startswith("allowed") for v in results["self"].values()), results["self"]
    assert set(results["recorded"]) == {
        "fsgetpath of the other WORK",
        "chdir into the other WORK (path known)",
        "list the other WORK after chdir",
        "own kern.procargs2",
    }
    assert results["recorded"]["list the other WORK after chdir"] == "denied:errno 1"


@pytest.mark.ingest_host
def test_confinement_probes(tmp_path: Path) -> None:
    pilot_path = pilot()
    sandbox = need("reader.ingest.host.sandbox")
    setup = need("reader.ingest.host.setup")
    data = data_dir()
    work = Path(os.path.realpath(tmp_path / "w"))
    work.mkdir()
    (work / "probes.py").write_text(PROBES)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    home_canary = Path.home() / f".reader-probe-{os.getpid()}"
    home_canary.write_text("canary")
    data_canary = data / f"probe-canary-{os.getpid()}"
    data_canary.write_text("canary")
    outside = tmp_path / "outside.txt"
    venv = setup.venv_dir(data)
    argv = sandbox.child_argv(venv, setup.pyroot_of(venv), setup.models_dir(data), work)
    python_at = max(i for i, a in enumerate(argv) if a == "-D") + 2
    probe_argv = [
        *argv[:python_at],
        argv[python_at],
        "-I",
        str(work / "probes.py"),
        json.dumps(
            {
                "port": listener.getsockname()[1],
                "docker_sockets": [str(Path.home() / ".docker/run/docker.sock"), "/var/run/docker.sock"],
                "parent": os.getpid(),
                "home_canary": str(home_canary),
                "data_canary": str(data_canary),
                "material": str(pilot_path),
                "outside": str(outside),
                "home": str(Path.home()),
            }
        ),
    ]
    env = {**sandbox.child_env(work, os.cpu_count() or 1)}
    os.environ["CANARY_ENV"] = "s3cr3t"
    try:
        done = subprocess.run(
            probe_argv, env=env, cwd=work, capture_output=True, timeout=120, check=False
        )
        child = subprocess.Popen(
            [*argv[: python_at + 1], "-I", "-c", "import time; time.sleep(5)"],
            env=env,
            cwd=work,
        )
        time.sleep(1.5)
        listening = subprocess.run(
            ["/usr/sbin/lsof", "-a", "-p", str(child.pid), "-i"], capture_output=True, check=False
        )
        child.wait()
    finally:
        os.environ.pop("CANARY_ENV", None)
        home_canary.unlink()
        data_canary.unlink()
        listener.close()
    results = json.loads(done.stdout)
    allowed = sorted(k for k, v in results.items() if v == "allowed")
    assert allowed == [], allowed
    # Where the target exists, the refusal must be the sandbox's, not a missing file or a failed lookup.
    existing = [
        "read home canary",
        "read data canary",
        "read original material",
        "read /etc/passwd",
        "list home",
        "signal parent",
        "exec /usr/bin/security",
        "exec /bin/sh",
        "posix semaphore",
        "posix shm",
    ]
    refused = {name: results[name] for name in existing}
    assert all(kind == "PermissionError" for kind in refused.values()), refused
    assert results["env"] == sorted(env) and results["canary_env"] is False
    assert not outside.exists()
    assert listening.stdout.strip() == b""
