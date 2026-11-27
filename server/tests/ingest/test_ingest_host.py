"""The host command `reader-ingest` with fake subprocesses (SRV-INGEST FR-INGEST-1..4, -16; INV-INGEST-2, -5).

@trace FR-INGEST-1 FR-INGEST-2 FR-INGEST-3 FR-INGEST-4 FR-INGEST-16 INV-INGEST-2 INV-INGEST-5 FR-INGEST-41
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
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
    migrate_exit: int = 0
    migrate_out: bytes = b'{"result":"migrated","rows":{}}'
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
            reply = {"status": self.import_status, "converter_version": "docling-2.151.1+r2"}
            return 0, json.dumps(reply).encode()
        if "reader.ingest.receive" in joined:
            return self.receive_exit, self.receive_out
        if "reader.ingest.requeue" in joined:
            return 0, b'{"result":"queued"}'
        if "reader.ingest.migrate_citations" in joined:
            return self.migrate_exit, self.migrate_out
        raise AssertionError(f"unexpected command {argv}")


@dataclass
class FakeChild:
    pid: int
    lines: list[bytes]
    code: int
    events: list[str] = field(default_factory=list)
    raise_in_lines: BaseException | None = None

    def stdout_lines(self) -> Iterator[bytes]:
        yield from self.lines
        if self.raise_in_lines is not None:
            raise self.raise_in_lines

    def wait(self, timeout: float | None = None) -> int:
        self.events.append("wait")
        return self.code

    def terminate(self) -> None:
        self.events.append("terminate")

    def kill(self) -> None:
        self.events.append("kill")


@dataclass
class FakeSpawn:
    write_artifact: bool = True
    code: int = 0
    noise: bool = False
    hostile: str | None = None  # symlink member, fifo member, out symlink
    raise_in_lines: BaseException | None = None
    started: list[tuple[list[str], dict[str, str], Path]] = field(default_factory=list)
    children: list[FakeChild] = field(default_factory=list)

    def __call__(self, argv: list[str], env: dict[str, str], cwd: Path) -> FakeChild:
        self.started.append((argv, env, cwd))
        out = Path(argv[argv.index("--out") + 1])
        if self.write_artifact:
            target = out
            if self.hostile == "out symlink":  # the child replaces `out` with a link to a valid artifact
                target = out.parent / "elsewhere"
                out.symlink_to(target)
            target.mkdir(parents=True, exist_ok=True)
            for name, body in build_artifact(material_sha256=SHA).items():
                (target / name).write_bytes(body)
            if self.hostile == "symlink member":
                real = out.parent / "real-spans"
                os.replace(out / "spans.jsonl", real)
                (out / "spans.jsonl").symlink_to(real)
            if self.hostile == "fifo member":
                (out / "outline.json").unlink()
                os.mkfifo(out / "outline.json")
        lines = [b'{"pages_done":2,"pages_total":2}\n']
        if self.noise:
            lines = [CANARY.encode() + b"\n", *lines, b"stderr? " + CANARY.encode()]
        child = FakeChild(4242, lines, self.code, raise_in_lines=self.raise_in_lines)
        self.children.append(child)
        return child


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


def test_convert_lock_held_gives_busy_before_any_run_record(tmp_path: Path) -> None:
    """FR-INGEST-1, one converter at a time: a held convert.lock (another run's) makes this run busy."""
    import fcntl

    path = config(tmp_path)
    locks = data(tmp_path) / "locks"
    locks.mkdir(parents=True)
    with (locks / "convert.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        cli = need("reader.ingest.host.cli")
        spawn, err = FakeSpawn(), io.StringIO()
        code = cli.ingest(["m1", "--config", str(path)], deps(FakeRunner(), spawn), io.StringIO(), err)
    assert code == 3 and "busy" in err.getvalue() and "another conversion" in err.getvalue()
    assert spawn.started == []
    assert not (data(tmp_path) / "run").exists() or not any((data(tmp_path) / "run").iterdir())
    assert not (data(tmp_path) / "work").exists() or not any((data(tmp_path) / "work").iterdir())


def test_requeue_does_not_take_the_convert_lock(tmp_path: Path) -> None:
    import fcntl

    path = config(tmp_path)
    locks = data(tmp_path) / "locks"
    locks.mkdir(parents=True)
    with (locks / "convert.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        cli = need("reader.ingest.host.cli")
        err, runner = io.StringIO(), FakeRunner()
        argv = ["m1", "--requeue", "--config", str(path)]
        code = cli.ingest(argv, deps(runner, FakeSpawn()), io.StringIO(), err)
    assert code == 0, err.getvalue()
    assert "another conversion" not in err.getvalue()
    assert any("reader.ingest.requeue" in " ".join(c[0]) for c in runner.calls)


def test_requeue_cleans_no_record(tmp_path: Path) -> None:
    """FR-INGEST-16 (code review round 1): a watch-mode run of an upload holds no material lock, so --requeue
    leaves every record, its own material's included."""
    records = need("reader.ingest.host.records")
    cli = need("reader.ingest.host.cli")
    path = config(tmp_path)
    run_dir, work_root = data(tmp_path) / "run", data(tmp_path) / "work"
    name = records.new_run(run_dir, work_root, SHA, upload_id="6f1c2a3b-4d5e-4f60-8a7b-9c0d1e2f3a4b")
    argv = ["m1", "--requeue", "--config", str(path)]
    cli.ingest(argv, deps(FakeRunner(), FakeSpawn()), io.StringIO(), io.StringIO())
    assert (run_dir / f"{name}.json").exists()
    assert (work_root / name).is_dir()


def test_convert_lock_fd_is_close_on_exec_and_not_inherited(tmp_path: Path) -> None:
    import fcntl

    cli = need("reader.ingest.host.cli")
    with cli.convert_lock(tmp_path) as fd:
        assert fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
        script = (
            "import fcntl, os, sys\n"
            "fds = sorted(int(n) for n in os.listdir('/dev/fd'))\n"
            "print(' '.join(map(str, fds)))\n"
        )
        child = cli.system_spawn([sys.executable, "-I", "-c", script], {"PATH": "/usr/bin:/bin"}, tmp_path)
        listed = b"".join(child.stdout_lines()).decode().split()
        child.wait(30)
        assert str(fd) not in listed
    # once released, a child can take it (nothing else holds it)
    probe = (
        "import fcntl, os, sys\n"
        f"fd = os.open({str(tmp_path / 'ingest' / 'locks' / 'convert.lock')!r}, os.O_RDWR)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
    )
    done = subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True, check=False)
    assert done.returncode == 0


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


def test_migrate_citations_runs_the_health_check_then_the_command(tmp_path: Path) -> None:
    """FR-INGEST-41: the health check, then exactly the in-container command; exit 4 maps to `unmapped`."""
    cli = need("reader.ingest.host.cli")
    argv = ["--migrate-citations", "--config", str(config(tmp_path))]
    for exit_code, body, want, code_text in ((0, b'{"result":"migrated","rows":{"t":2}}', 0, ""),
                                             (4, b'{"result":"unmapped","unmapped":{}}', 4, "unmapped")):
        runner, spawn = FakeRunner(migrate_exit=exit_code, migrate_out=body), FakeSpawn()
        err = io.StringIO()
        assert cli.ingest(argv, deps(runner, spawn, None), io.StringIO(), err) == want
        assert code_text in err.getvalue()
        assert spawn.started == []
        assert runner.calls[0][0][-2:] == ["status", "--json"]
        execs = [c[0] for c in runner.calls if c[0][:2] == ["docker", "exec"]]
        assert execs == [["docker", "exec", "-i", CONTAINER, "python", "-I", "-m",
                          "reader.ingest.migrate_citations"]]
    runner = FakeRunner(status_exit=1)
    assert cli.ingest(argv, deps(runner, FakeSpawn(), None), io.StringIO(), io.StringIO()) == 3
    assert not any("migrate_citations" in " ".join(c[0]) for c in runner.calls)


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
    """The spike profile minus SCRIPTS, `(allow sysctl-read)` replaced by the measured edits (FR-INGEST-4)."""
    sandbox = need("reader.ingest.host.sandbox")
    ours = sandbox.profile_rules(sandbox.PROFILE.read_text())
    spike = sandbox.profile_rules((FIXTURES / "spike-converter.sb").read_text())
    edits = sandbox.profile_rules((FIXTURES / "profile-edits.sb").read_text())
    without_scripts = [r.replace(' (subpath (param "SCRIPTS"))', "") for r in spike]
    assert without_scripts.count("(allow sysctl-read)") == 1
    at = without_scripts.index("(allow sysctl-read)")
    assert ours == [*without_scripts[:at], *edits, *without_scripts[at + 1 :]]
    assert "(allow sysctl-read)" not in ours
    assert all("network" not in r and "mach-lookup" not in r and "iokit" not in r for r in ours)
    assert not any("kern.proc" in r for r in ours)
    assert ours[-2:] == ["(deny process-info*)", "(allow process-info* (target self))"]
    text = sandbox.PROFILE.read_text()
    prefixes = re.findall(r'\(sysctl-name-prefix "([^"]*)"\)', text)
    names = re.findall(r'\(sysctl-name "([^"]*)"\)', text)
    assert prefixes and names
    for guarded in ("kern.procargs2", "kern.proc.pid"):
        assert not any(guarded.startswith(p) for p in prefixes), (guarded, prefixes)
    assert not any(n.startswith("kern.proc") for n in names), names


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


def test_cleanup_under_the_convert_lock_cleans_every_material(tmp_path: Path) -> None:
    """FR-INGEST-16 as amended: a converter run holding convert.lock cleans every record, any material."""
    records = need("reader.ingest.host.records")
    other = "e" * 64
    rec, work = make_record(tmp_path, other, 103, "T3")
    probe = FakeProbe({103: ("T3", os.path.realpath(work))})
    records.cleanup_stale(tmp_path / "run", tmp_path / "work", None, probe)
    assert probe.killed == [103] and not rec.exists() and not work.exists()


def test_cleanup_never_removes_outside_the_work_root(tmp_path: Path) -> None:
    records = need("reader.ingest.host.records")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    outside = tmp_path / "precious"
    outside.mkdir()
    (run_dir / "x.json").write_text(json.dumps({"material_sha256": SHA, "work": str(outside)}))
    records.cleanup_stale(run_dir, tmp_path / "work", SHA, FakeProbe())
    assert outside.is_dir() and not (run_dir / "x.json").exists()


def test_run_record_comes_before_the_work_directory(tmp_path: Path, monkeypatch: Any) -> None:
    records = need("reader.ingest.host.records")

    def refuse(path: Path, record: dict[str, Any]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(records, "_write", refuse)
    with pytest.raises(OSError, match="disk full"):
        records.new_run(tmp_path / "run", tmp_path / "work", SHA)
    assert list((tmp_path / "work").iterdir()) == []  # no anonymous work directory


def test_probe_subprocesses_get_an_allow_listed_environment() -> None:
    records = need("reader.ingest.host.records")
    assert set(records.PROBE_ENV) == {"PATH", "LANG"}
    assert os.getpid() and records.SystemProbe().start_time(os.getpid())


# ---- FR-INGEST-10: members read once, hostile outputs, the session killed ----


@pytest.mark.parametrize("hostile", ["symlink member", "fifo member", "out symlink"])
def test_hostile_child_outputs_are_refused(tmp_path: Path, hostile: str) -> None:
    spawn = FakeSpawn(hostile=hostile)
    code, _, err = run(tmp_path, FakeRunner(), spawn)
    assert code == 5 and "convert_failed" in err, err


def test_bytes_sent_are_the_bytes_validated(tmp_path: Path, monkeypatch: Any) -> None:
    artifact = need("reader.ingest.artifact")
    original = artifact.validate_members
    swapped: list[Path] = []

    def validate_then_swap(members: dict[str, bytes]) -> dict[str, Any]:
        meta = original(members)
        out = next(tmp_path.rglob("out"))
        (out / "spans.jsonl").write_bytes(b"swapped after validation\n")  # the child's last word
        swapped.append(out)
        return meta

    monkeypatch.setattr(artifact, "validate_members", validate_then_swap)
    runner = FakeRunner()
    code, _, err = run(tmp_path, runner, FakeSpawn())
    assert code == 0, err
    assert swapped
    (sent,) = [c[2] for c in runner.calls if "reader.ingest.receive" in " ".join(c[0])]
    assert sent is not None and b"swapped after validation" not in sent
    assert build_artifact(material_sha256=SHA)["spans.jsonl"] in sent


def test_child_session_is_killed_after_every_exit(tmp_path: Path) -> None:
    for code, spawn in ((0, FakeSpawn()), (5, FakeSpawn(code=1))):
        got, _, _ = run(tmp_path / str(code), FakeRunner(), spawn)
        assert got == code
        assert spawn.children[0].events[-1] == "kill"


def test_interrupt_terminates_then_kills_and_cleans_up(tmp_path: Path) -> None:
    cli = need("reader.ingest.host.cli")
    spawn = FakeSpawn(raise_in_lines=cli.Stop("interrupted", 143))
    got, _, err = run(tmp_path, FakeRunner(), spawn)
    assert got == 143 and "interrupted" in err
    assert spawn.children[0].events[:1] == ["terminate"] and spawn.children[0].events[-1] == "kill"
    assert list((data(tmp_path) / "work").iterdir()) == []
    assert list((data(tmp_path) / "run").iterdir()) == []


def test_child_timeout_kills_the_session(tmp_path: Path, monkeypatch: Any) -> None:
    import threading

    cli = need("reader.ingest.host.cli")
    monkeypatch.setattr(cli, "CHILD_TIMEOUT_S", 0.05)
    released = threading.Event()

    @dataclass
    class HangingChild(FakeChild):
        def stdout_lines(self) -> Iterator[bytes]:
            released.wait(5)  # blocks until the timer kills the session
            yield from ()

        def kill(self) -> None:
            self.events.append("kill")
            released.set()

    hanging = HangingChild(4242, [], -9)

    def spawn(argv: list[str], env: dict[str, str], cwd: Path) -> FakeChild:
        return hanging

    got, _, err = run(tmp_path, FakeRunner(), spawn)  # type: ignore[arg-type]
    assert got == 5 and "convert_failed" in err
    assert released.is_set() and "kill" in hanging.events


def test_system_spawn_runs_a_session_reads_lines_and_counts_stderr(tmp_path: Path) -> None:
    import sys

    cli = need("reader.ingest.host.cli")
    script = (
        "import sys, time; time.sleep(3); sys.stderr.write('x' * 10); "
        "print('{\"pages_done\":1,\"pages_total\":1}'); print('y' * 10000)"
    )
    child = cli.system_spawn([sys.executable, "-I", "-c", script], {"PATH": "/usr/bin:/bin"}, tmp_path)
    assert os.getsid(child.pid) == child.pid  # alive (sleeping), and the leader of its own session
    lines = list(child.stdout_lines())
    assert child.wait(10) == 0
    assert lines[0].strip() == b'{"pages_done":1,"pages_total":1}'
    assert all(len(line) <= cli.LINE_LIMIT for line in lines)  # a long line arrives in bounded pieces
    child.kill()  # a finished session: no error
