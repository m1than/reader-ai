"""The host command `reader-ingest` with fake subprocesses (SRV-INGEST FR-INGEST-1..4, -16; INV-INGEST-2, -5).

@trace FR-INGEST-1 FR-INGEST-2 FR-INGEST-3 FR-INGEST-4 FR-INGEST-16 INV-INGEST-2 INV-INGEST-5
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import textwrap
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from .conftest import CANARY, FIXTURES, build_artifact, need

PDF = b"%PDF-1.7 synthetic bytes for the fake converter " + CANARY.encode()
SHA = hashlib.sha256(PDF).hexdigest()
CONTAINER = "c0ffee" * 10 + "abcd"


def config(tmp: Path, sha: str = SHA) -> Path:
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "m.pdf").write_bytes(PDF)
    path = tmp / "reader.yaml"
    path.write_text(
        textwrap.dedent(
            f"""\
            version: 1
            instance: {{ url: "https://localhost", data_dir: ./data }}
            users:
              - {{ handle: ivan, role: owner }}
            materials:
              - {{ id: m1, file: ./m.pdf, sha256: "{sha}", license: personal-use, language: ru }}
            """
        ),
        encoding="utf-8",
    )
    return path


@dataclass
class FakeRunner:
    status_exit: int = 0
    import_status: str = "none"
    receive_out: bytes = b'{"result":"queued","artifact_id":"x"}'
    receive_exit: int = 0
    containers: str = CONTAINER
    calls: list[tuple[list[str], dict[str, str], bytes | None]] = field(default_factory=list)

    def run(
        self, argv: list[str], env: dict[str, str], timeout: float, stdin: bytes | None = None
    ) -> tuple[int, bytes]:
        self.calls.append((argv, env, stdin))
        joined = " ".join(argv)
        if argv[-2:] == ["status", "--json"]:
            body = {"project": "reader", "watcher": "running", "services": []}
            return self.status_exit, json.dumps(body).encode()
        if argv[:2] == ["docker", "ps"]:
            return 0, (self.containers + "\n").encode() if self.containers else b""
        if argv[:2] == ["docker", "inspect"]:
            labels = {
                "com.docker.compose.project": "reader",
                "com.docker.compose.service": "worker",
                "com.docker.compose.oneoff": "False",
            }
            body = [{"Id": argv[2], "State": {"Running": True}, "Config": {"Labels": labels}}]
            return 0, json.dumps(body).encode()
        if "reader.ingest.status" in joined:
            reply = {"status": self.import_status, "converter_version": "docling-2.151.1+r1"}
            return 0, json.dumps(reply).encode()
        if "reader.ingest.receive" in joined:
            return self.receive_exit, self.receive_out
        if "reader.ingest.requeue" in joined:
            return 0, b'{"result":"queued"}'
        raise AssertionError(f"unexpected command {argv}")


@dataclass
class FakeChild:
    pid: int
    lines: list[bytes]
    code: int

    def stdout_lines(self) -> Iterator[bytes]:
        yield from self.lines

    def wait(self, timeout: float | None = None) -> int:
        return self.code

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass


@dataclass
class FakeSpawn:
    write_artifact: bool = True
    code: int = 0
    noise: bool = False
    started: list[tuple[list[str], dict[str, str], Path]] = field(default_factory=list)

    def __call__(self, argv: list[str], env: dict[str, str], cwd: Path) -> FakeChild:
        self.started.append((argv, env, cwd))
        out = Path(argv[argv.index("--out") + 1])
        if self.write_artifact:
            out.mkdir(parents=True, exist_ok=True)
            for name, body in build_artifact(material_sha256=SHA).items():
                (out / name).write_bytes(body)
        lines = [b'{"pages_done":2,"pages_total":2}\n']
        if self.noise:
            lines = [CANARY.encode() + b"\n", *lines, b"stderr? " + CANARY.encode()]
        return FakeChild(4242, lines, self.code)


@dataclass
class FakeProbe:
    alive_pids: dict[int, tuple[str, str]] = field(default_factory=dict)  # pid -> (start, cwd)
    killed: list[int] = field(default_factory=list)

    def alive(self, pid: int) -> bool:
        return pid in self.alive_pids

    def start_time(self, pid: int) -> str | None:
        return self.alive_pids.get(pid, (None, None))[0]

    def cwd(self, pid: int) -> str | None:
        return self.alive_pids.get(pid, (None, None))[1]

    def killpg(self, pid: int) -> None:
        self.killed.append(pid)


def deps(runner: FakeRunner, spawn: FakeSpawn, probe: FakeProbe | None = None) -> Any:
    cli = need("reader.ingest.host.cli")
    return cli.Deps(
        runner=runner,
        spawn=spawn,
        probe=probe or FakeProbe(),
        check_setup=lambda data_dir: None,
        venv=Path("/opt/venv"),
        pyroot=Path("/opt/py"),
        models=Path("/opt/models"),
        cpus=4,
        start_time=lambda pid: "Sun Nov 22 10:00:00 2026",
    )


def run(tmp: Path, runner: FakeRunner, spawn: FakeSpawn, *extra: str, probe: FakeProbe | None = None) -> Any:
    cli = need("reader.ingest.host.cli")
    out, err = io.StringIO(), io.StringIO()
    code = cli.ingest(["m1", "--config", str(config(tmp)), *extra], deps(runner, spawn, probe), out, err)
    return code, out.getvalue(), err.getvalue()


def data(tmp: Path) -> Path:
    return tmp / "data" / "ingest"


# ---- FR-INGEST-1 ----


def test_happy_path_converts_copy_and_sends_tar(tmp_path: Path) -> None:
    runner, spawn = FakeRunner(), FakeSpawn()
    code, out, err = run(tmp_path, runner, spawn)
    assert code == 0, err
    argv, _, cwd = spawn.started[0]
    work = Path(argv[argv.index("--out") + 1]).parent
    assert cwd == work
    assert argv[argv.index("--input") + 1] == str(work / "in" / "material.pdf")
    receive = [c for c in runner.calls if "reader.ingest.receive" in " ".join(c[0])]
    assert len(receive) == 1 and receive[0][2] is not None and receive[0][2][257:263] == b"ustar\x00"
    assert receive[0][0][:3] == ["docker", "exec", "-i"] and CONTAINER in receive[0][0]
    assert "-u" not in receive[0][0]
    assert not work.exists()
    assert list((data(tmp_path) / "run").iterdir()) == []
    assert "page 2/2" in out


def test_unknown_material_and_sha_mismatch(tmp_path: Path) -> None:
    cli = need("reader.ingest.host.cli")
    err = io.StringIO()
    argv = ["zz", "--config", str(config(tmp_path))]
    code = cli.ingest(argv, deps(FakeRunner(), FakeSpawn()), io.StringIO(), err)
    assert code == 2 and "unknown_material" in err.getvalue()
    bad = tmp_path / "bad"
    bad.mkdir()
    spawn = FakeSpawn()
    err = io.StringIO()
    argv = ["m1", "--config", str(config(bad, sha="f" * 64))]
    code = cli.ingest(argv, deps(FakeRunner(), spawn), io.StringIO(), err)
    assert code == 2 and "sha256_mismatch" in err.getvalue() and spawn.started == []


def test_symlinked_material_is_refused(tmp_path: Path) -> None:
    path = config(tmp_path)
    (tmp_path / "real.pdf").write_bytes(PDF)
    (tmp_path / "m.pdf").unlink()
    os.symlink(tmp_path / "real.pdf", tmp_path / "m.pdf")
    cli = need("reader.ingest.host.cli")
    spawn, err = FakeSpawn(), io.StringIO()
    code = cli.ingest(["m1", "--config", str(path)], deps(FakeRunner(), spawn), io.StringIO(), err)
    assert code == 2 and spawn.started == []


def test_material_lock_makes_a_second_run_busy(tmp_path: Path) -> None:
    import fcntl

    path = config(tmp_path)
    locks = data(tmp_path) / "locks"
    locks.mkdir(parents=True)
    with (locks / f"{SHA}.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        cli = need("reader.ingest.host.cli")
        spawn, err = FakeSpawn(), io.StringIO()
        code = cli.ingest(["m1", "--config", str(path)], deps(FakeRunner(), spawn), io.StringIO(), err)
    assert code == 3 and "busy" in err.getvalue() and spawn.started == []


def test_run_record_is_written_before_the_work_directory(tmp_path: Path) -> None:
    records = need("reader.ingest.host.records")
    run_dir, work_root = tmp_path / "run", tmp_path / "work"
    name = records.new_run(run_dir, work_root, SHA)
    record = json.loads((run_dir / f"{name}.json").read_text())
    assert record == {"material_sha256": SHA, "work": str(work_root / name)}
    assert (work_root / name).is_dir()
    assert oct((run_dir / f"{name}.json").stat().st_mode & 0o777) == "0o600"
    assert oct((work_root / name).stat().st_mode & 0o777) == "0o700"


# ---- FR-INGEST-2, INV-INGEST-2 ----


@pytest.mark.parametrize(("status", "code"), [("succeeded", 0), ("queued", 0), ("running", 0), ("dead", 4)])
def test_import_status_skips_conversion(tmp_path: Path, status: str, code: int) -> None:
    spawn = FakeSpawn()
    got, _, err = run(tmp_path, FakeRunner(import_status=status), spawn)
    assert got == code and spawn.started == []
    if status == "dead":
        assert "import_dead" in err and "--requeue" in err


def test_unhealthy_stack_stops_before_conversion(tmp_path: Path) -> None:
    spawn = FakeSpawn()
    got, _, err = run(tmp_path, FakeRunner(status_exit=1), spawn)
    assert got == 3 and "stack_unhealthy" in err and spawn.started == []
    spawn = FakeSpawn()
    got, _, err = run(tmp_path / "x", FakeRunner(containers=""), spawn)
    assert got == 3 and spawn.started == []


def test_every_subprocess_gets_an_allow_listed_environment(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("CANARY_SECRET", "s3cr3t")
    monkeypatch.setenv("DOCKER_HOST", "unix:///x.sock")
    runner = FakeRunner()
    run(tmp_path, runner, FakeSpawn())
    for _, env, _ in runner.calls:
        assert set(env) <= {"PATH", "HOME", "LANG", "DOCKER_HOST"}
        assert "s3cr3t" not in env.values()


def test_receive_errors_map_to_exit_codes(tmp_path: Path) -> None:
    got, _, err = run(tmp_path, FakeRunner(receive_out=b'{"error":"schema"}', receive_exit=1), FakeSpawn())
    assert got == 5 and "schema" in err
    dead = FakeRunner(receive_out=b'{"result":"dead","artifact_id":"x"}')
    got, _, _ = run(tmp_path / "d", dead, FakeSpawn())
    assert got == 4


def test_failed_conversion_is_convert_failed(tmp_path: Path) -> None:
    got, _, err = run(tmp_path, FakeRunner(), FakeSpawn(code=1))
    assert got == 5 and "convert_failed" in err
    got, _, err = run(tmp_path / "b", FakeRunner(), FakeSpawn(write_artifact=False))
    assert got == 5 and "convert_failed" in err


def test_requeue_runs_without_conversion(tmp_path: Path) -> None:
    runner, spawn = FakeRunner(import_status="dead"), FakeSpawn()
    got, _, err = run(tmp_path, runner, spawn, "--requeue")
    assert got == 0, err
    assert spawn.started == []
    assert any("reader.ingest.requeue" in " ".join(c[0]) for c in runner.calls)


# ---- INV-INGEST-5 (unit): nothing from the child reaches the terminal ----


def test_child_output_never_reaches_the_terminal(tmp_path: Path) -> None:
    got, out, err = run(tmp_path, FakeRunner(), FakeSpawn(noise=True))
    assert got == 0
    assert CANARY not in out and CANARY not in err


# ---- FR-INGEST-3: setup checks ----


def test_setup_check_reports_missing_and_stale(tmp_path: Path) -> None:
    setup = need("reader.ingest.host.setup")
    assert setup.check_setup(tmp_path) == "setup_missing"
    venv = setup.venv_dir(tmp_path)
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("")
    record = setup.expected_record()
    (venv / "setup.json").write_text(json.dumps({**record, "converter_source_sha256": "0" * 64}))
    assert setup.check_setup(tmp_path) == "setup_stale"
    (venv / "setup.json").write_text(json.dumps(record))
    assert setup.check_setup(tmp_path) == "setup_missing"  # the locked model files are still absent
    lock = json.loads(setup.MODELS_LOCK.read_text())
    models = setup.models_dir(tmp_path)
    for repo in lock["repos"]:
        for rel in repo["files"]:
            target = models / repo["folder"] / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x")
    assert setup.check_setup(tmp_path) == "setup_stale"  # wrong hashes


def test_models_lock_pins_commit_revisions_and_hashes() -> None:
    setup = need("reader.ingest.host.setup")
    lock = json.loads(setup.MODELS_LOCK.read_text())
    folders = {r["folder"] for r in lock["repos"]}
    assert folders == {
        "docling-project--docling-layout-heron",
        "docling-project--docling-layout-heron-onnx",
        "docling-project--docling-models",
        "docling-project--CodeFormulaV2",
    }
    for repo in lock["repos"]:
        assert repo["folder"] == repo["repo_id"].replace("/", "--")
        assert re.fullmatch(r"[0-9a-f]{40}", repo["revision"])
        assert repo["files"] and all(re.fullmatch(r"[0-9a-f]{64}", d) for d in repo["files"].values())


def test_requirements_lock_is_hash_pinned_and_covers_the_converter_imports() -> None:
    setup = need("reader.ingest.host.setup")
    closure = need("reader.ingest.host.closure")
    text = setup.REQUIREMENTS_LOCK.read_text()
    pins = dict(re.findall(r"^([A-Za-z0-9_.\-]+)==([^\s\\]+)", text, flags=re.M))
    assert pins.get("docling") == "2.151.1"
    assert text.count("--hash=sha256:") >= len(pins)
    names = {re.sub(r"[-_.]+", "-", n).lower() for n in pins}
    found = closure.import_closure()
    assert found.third_party, "the converter imports Docling"
    for module in found.third_party:
        assert re.sub(r"[-_.]+", "-", module).lower() in names, module


def test_source_hash_comes_from_the_import_closure() -> None:
    closure = need("reader.ingest.host.closure")
    found = closure.import_closure()
    rels = {p.as_posix().split("/src/", 1)[1] for p in found.files}
    assert "reader/ingest/host/convert.py" in rels
    assert "reader/ingest/artifact.py" in rels
    assert not any(r.startswith(("reader/jobs/", "reader/api/", "reader/db/")) for r in rels)
    assert re.fullmatch(r"[0-9a-f]{64}", closure.source_sha256())


# ---- FR-INGEST-4: argv, environment, profile ----


def test_child_argv_and_environment(tmp_path: Path) -> None:
    sandbox = need("reader.ingest.host.sandbox")
    work = tmp_path / "w"
    work.mkdir()
    argv = sandbox.child_argv(Path("/opt/venv"), Path("/opt/py"), Path("/opt/models"), work)
    assert argv[:3] == ["/usr/bin/sandbox-exec", "-f", str(sandbox.PROFILE)]
    params = dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "-D")
    assert set(params) == {"PYROOT", "VENV", "MODELS", "WORK"}
    assert params["WORK"] == os.path.realpath(work)
    last_param = max(i for i, a in enumerate(argv) if a == "-D")
    tail = argv[last_param + 2 :]
    assert tail[:4] == [params["VENV"] + "/bin/python", "-I", "-m", "reader.ingest.host.convert"]
    env = sandbox.child_env(work, 8)
    assert env == {
        "PATH": "/usr/bin:/bin",
        "HOME": str(work / "home"),
        "TMPDIR": str(work / "tmp"),
        "LANG": "en_US.UTF-8",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "OMP_NUM_THREADS": "8",
    }


def test_profile_equals_the_spike_profile_minus_scripts() -> None:
    sandbox = need("reader.ingest.host.sandbox")
    ours = sandbox.profile_rules(sandbox.PROFILE.read_text())
    spike = sandbox.profile_rules((FIXTURES / "spike-converter.sb").read_text())
    without_scripts = [r.replace(' (subpath (param "SCRIPTS"))', "") for r in spike]
    assert ours == without_scripts
    assert all("network" not in r and "mach-lookup" not in r and "iokit" not in r for r in ours)


# ---- FR-INGEST-16: stale cleanup ----


def make_record(tmp: Path, sha: str, sid: int, start: str) -> tuple[Path, Path]:
    records = need("reader.ingest.host.records")
    run_dir, work_root = tmp / "run", tmp / "work"
    name = records.new_run(run_dir, work_root, sha)
    records.set_child(run_dir, name, sid, start)
    return run_dir / f"{name}.json", work_root / name


def test_cleanup_kills_only_a_verified_child(tmp_path: Path) -> None:
    records = need("reader.ingest.host.records")
    rec, work = make_record(tmp_path, SHA, 101, "T1")
    probe = FakeProbe({101: ("T1", os.path.realpath(work))})
    records.cleanup_stale(tmp_path / "run", tmp_path / "work", SHA, probe)
    assert probe.killed == [101]
    assert not rec.exists() and not work.exists()


def test_cleanup_never_signals_a_reused_id(tmp_path: Path) -> None:
    records = need("reader.ingest.host.records")
    rec1, work1 = make_record(tmp_path, SHA, 101, "T1")
    rec2, work2 = make_record(tmp_path, SHA, 102, "T2")
    probe = FakeProbe({101: ("T9", os.path.realpath(work1)), 102: ("T2", "/somewhere/else")})
    records.cleanup_stale(tmp_path / "run", tmp_path / "work", SHA, probe)
    assert probe.killed == []
    assert not rec1.exists() and not work1.exists() and not rec2.exists() and not work2.exists()


def test_cleanup_leaves_other_materials_alone(tmp_path: Path) -> None:
    records = need("reader.ingest.host.records")
    other = "e" * 64
    rec, work = make_record(tmp_path, other, 103, "T3")
    probe = FakeProbe({103: ("T3", os.path.realpath(work))})
    records.cleanup_stale(tmp_path / "run", tmp_path / "work", SHA, probe)
    assert probe.killed == [] and rec.exists() and work.exists()


def test_cleanup_never_removes_outside_the_work_root(tmp_path: Path) -> None:
    records = need("reader.ingest.host.records")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    outside = tmp_path / "precious"
    outside.mkdir()
    (run_dir / "x.json").write_text(json.dumps({"material_sha256": SHA, "work": str(outside)}))
    records.cleanup_stale(run_dir, tmp_path / "work", SHA, FakeProbe())
    assert outside.is_dir() and not (run_dir / "x.json").exists()
