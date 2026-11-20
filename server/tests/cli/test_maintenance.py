"""Trust and untrust, backup and restore.

@trace FR-CLI-16, FR-CLI-17, FR-CLI-18
"""

import base64
import hashlib
import json
import plistlib
import stat
from pathlib import Path
from typing import Any

import pytest

from .conftest import Call, World, held_lock, make_world, rendered, stack, write_config

DER = b"\x30\x03\x02\x01\x01-fake-root"
PEM = (
    "-----BEGIN CERTIFICATE-----\n" + base64.b64encode(DER).decode() + "\n-----END CERTIFICATE-----\n"
).encode()
SHA1 = hashlib.sha1(DER).hexdigest().upper()  # noqa: S324 - the certificate's identity in the keychain, not security
IMAGE = "reader-server:local"


def _main(world: World, *argv: str) -> int:
    from reader.cli.app import main

    return int(main(list(argv), ctx=world.ctx))


# FR-CLI-16: trust -----------------------------------------------------------------------------------------


def _trust_world(tmp_path: Path) -> tuple[World, Path]:
    world = make_world(tmp_path)
    stack(world, rendered())
    world.runner.on("caddy", "cat", result=PEM)
    return world, write_config(tmp_path / "cfg")


def test_trust_keeps_a_host_copy_and_trusts_for_tls_only(tmp_path: Path) -> None:
    world, config = _trust_world(tmp_path)
    assert _main(world, "trust", "--config", str(config)) == 0
    pem = world.ctx.run_dir / "trusted-roots" / f"{SHA1}.pem"
    assert pem.read_bytes() == PEM
    (argv,) = world.runner.argvs("security", "add-trusted-cert")
    assert argv == [
        "security",
        "add-trusted-cert",
        "-r",
        "trustRoot",
        "-p",
        "ssl",
        "-k",
        str(world.home / "Library" / "Keychains" / "login.keychain-db"),
        str(pem),
    ]
    (export,) = world.runner.argvs("caddy", "cat")
    assert export[:4] == ["docker", "compose", "-p", "reader-vec"]


def _export(trusted: set[str]) -> Any:
    def handler(call: Call) -> Any:
        Path(call.argv[-1]).write_bytes(
            plistlib.dumps({"trustList": {k: {} for k in trusted}, "trustVersion": 1})
        )
        return 0

    return handler


def test_down_untrust_removes_every_recorded_root_and_verifies(tmp_path: Path) -> None:
    world, config = _trust_world(tmp_path)
    _main(world, "trust", "--config", str(config))
    world.runner.on("security", "trust-settings-export", handler=_export(set()))
    assert _main(world, "down", "--untrust", "--config", str(config)) == 0
    pem = str(world.ctx.run_dir / "trusted-roots" / f"{SHA1}.pem")
    assert world.runner.argvs("security", "remove-trusted-cert") == [["security", "remove-trusted-cert", pem]]
    assert world.runner.argvs("security", "delete-certificate") == [
        ["security", "delete-certificate", "-t", "-Z", SHA1]
    ]


def test_down_untrust_fails_when_a_root_remains(tmp_path: Path) -> None:
    world, config = _trust_world(tmp_path)
    _main(world, "trust", "--config", str(config))
    world.runner.on("security", "trust-settings-export", handler=_export({SHA1}))
    assert _main(world, "down", "--untrust", "--config", str(config)) != 0


def test_interactive_up_asks_before_trusting(tmp_path: Path) -> None:
    from reader.cli.commands import offer_trust

    world, _ = _trust_world(tmp_path)
    world.ctx.is_tty = True
    world.ctx.ask = lambda _prompt: False
    offer_trust(world.ctx, "reader-vec")
    assert world.runner.argvs("security") == []
    world.ctx.ask = lambda _prompt: True
    offer_trust(world.ctx, "reader-vec")
    assert len(world.runner.argvs("security", "add-trusted-cert")) == 1


# FR-CLI-17: backup ----------------------------------------------------------------------------------------


def test_volume_user_comes_from_the_mounting_services() -> None:
    from reader.cli.stack import Rendered, volume_user

    model = Rendered.parse(rendered())
    assert volume_user(model, "blobs") == "10001:10001"
    assert volume_user(model, "caddy_config") == "0:0"


@pytest.mark.parametrize("worker_user", ["10002:10002", None])
def test_volume_user_refuses_disagreeing_services(worker_user: str | None) -> None:
    from reader.cli.errors import CliError
    from reader.cli.stack import Rendered, volume_user

    data = rendered()
    data["services"]["worker"] = {k: v for k, v in data["services"]["worker"].items() if k != "user"}
    if worker_user:
        data["services"]["worker"]["user"] = worker_user
    with pytest.raises(CliError) as caught:
        volume_user(Rendered.parse(data), "blobs")
    assert "blobs" in str(caught.value)


def _backup_world(tmp_path: Path) -> tuple[World, Path]:
    world = make_world(tmp_path)
    stack(world, rendered())
    world.runner.on("pg_dump", result=b"DUMP")
    world.runner.on("docker", "run", "reader-vec_blobs:/v:ro", result=b"TAR-blobs")
    world.runner.on("docker", "run", "reader-vec_caddy_config:/v:ro", result=b"TAR-caddy")
    world.runner.on("postgres", "-c", "SHOW server_version", result=b"18.7\n")
    return world, write_config(tmp_path / "cfg")


def _tar_argv(user: str, volume: str) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--user",
        user,
        "-v",
        f"{volume}:/v:ro",
        IMAGE,
        "tar",
        "-C",
        "/v",
        "-cf",
        "-",
        ".",
    ]


def test_backup_writes_dump_tars_and_manifest(tmp_path: Path) -> None:
    world, config = _backup_world(tmp_path)
    out = tmp_path / "backup"
    with held_lock(world.ctx.run_dir):
        assert _main(world, "backup", str(out), "--config", str(config)) == 0
    files = {"db.dump": b"DUMP", "blobs.tar": b"TAR-blobs", "caddy_config.tar": b"TAR-caddy"}
    for name, content in files.items():
        assert (out / name).read_bytes() == content
        assert stat.S_IMODE((out / name).stat().st_mode) == 0o600
    manifest = json.loads((out / "manifest.json").read_text())
    assert stat.S_IMODE((out / "manifest.json").stat().st_mode) == 0o600
    assert {f["name"]: f["sha256"] for f in manifest["files"]} == {
        n: hashlib.sha256(c).hexdigest() for n, c in files.items()
    }
    assert {f["name"]: f["bytes"] for f in manifest["files"]} == {n: len(c) for n, c in files.items()}
    assert manifest["postgres"] == "18.7"
    assert {"created", "reader", "postgres", "files"} <= set(manifest)
    assert world.runner.argvs("pg_dump") == [
        [
            "docker",
            "compose",
            "-p",
            "reader-vec",
            "exec",
            "-T",
            "postgres",
            "pg_dump",
            "-Fc",
            "-U",
            "postgres",
            "postgres",
        ]
    ]
    assert _tar_argv("10001:10001", "reader-vec_blobs") in world.runner.argvs("docker", "run")
    assert _tar_argv("0:0", "reader-vec_caddy_config") in world.runner.argvs("docker", "run")
    assert not any("caddy_data" in " ".join(c.argv) for c in world.runner.calls)
    timeouts = {
        c.argv[7] if c.argv[1] == "compose" else c.argv[-6]: c.timeout
        for c in world.runner.calls
        if "pg_dump" in c.argv or "tar" in c.argv or "psql" in c.argv
    }
    assert timeouts == {"pg_dump": 600, "tar": 120, "psql": 120}


def test_backup_restarts_the_writers_after_a_partial_stop(tmp_path: Path) -> None:
    world, config = _backup_world(tmp_path)
    world.runner.on("docker", "compose", "stop", result=(1, b"", b"worker: timeout"))
    with held_lock(world.ctx.run_dir):
        assert _main(world, "backup", str(tmp_path / "b"), "--config", str(config)) != 0
    (start,) = world.runner.argvs("start")
    assert set(start[5:]) == {"api", "worker"}
    assert world.runner.argvs("pg_dump") == []


def test_backup_restarts_the_writers_after_a_hung_dump(tmp_path: Path) -> None:
    from reader.cli.runner import CommandTimeout

    def hang(_call: Any) -> Any:
        raise CommandTimeout()

    world, config = _backup_world(tmp_path)
    world.runner.on("pg_dump", handler=hang)
    with held_lock(world.ctx.run_dir):
        assert _main(world, "backup", str(tmp_path / "b"), "--config", str(config)) != 0
    assert len(world.runner.argvs("start")) == 1
    assert "backup_failed" in world.err


def test_backup_stops_and_restarts_the_writers(tmp_path: Path) -> None:
    world, config = _backup_world(tmp_path)
    with held_lock(world.ctx.run_dir):
        _main(world, "backup", str(tmp_path / "b"), "--config", str(config))
    (stop,) = world.runner.argvs("stop")
    (start,) = world.runner.argvs("start")
    assert stop[:5] == ["docker", "compose", "-p", "reader-vec", "stop"]
    assert set(stop[5:]) == {"api", "worker"}
    assert set(start[5:]) == {"api", "worker"}
    calls = [c.argv for c in world.runner.calls]
    assert calls.index(stop) < calls.index(world.runner.argvs("pg_dump")[0]) < calls.index(start)


def test_backup_refuses_a_directory_in_a_git_work_tree(tmp_path: Path) -> None:
    world, config = _backup_world(tmp_path)
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    with held_lock(world.ctx.run_dir):
        assert _main(world, "backup", str(tmp_path / "repo" / "b"), "--config", str(config)) != 0
    assert world.runner.argvs("pg_dump") == []
    assert world.runner.argvs("stop") == []


def test_backup_refuses_without_a_running_watcher(tmp_path: Path) -> None:
    world, config = _backup_world(tmp_path)
    assert _main(world, "backup", str(tmp_path / "b"), "--config", str(config)) != 0
    assert world.runner.argvs("stop") == []


# FR-CLI-18: restore ---------------------------------------------------------------------------------------


def _archive(directory: Path, files: dict[str, bytes], *, corrupt: str | None = None) -> Path:
    directory.mkdir(parents=True)
    for name, content in files.items():
        (directory / name).write_bytes(content)
    entries = [
        {"name": n, "sha256": hashlib.sha256(b"X" if n == corrupt else c).hexdigest(), "bytes": len(c)}
        for n, c in files.items()
    ]
    (directory / "manifest.json").write_text(
        json.dumps(
            {"created": "2026-11-20T10:00:00Z", "reader": "0.0.0", "postgres": "18.7", "files": entries}
        )
    )
    return directory


FILES = {"db.dump": b"DUMP", "blobs.tar": b"TAR-blobs", "caddy_config.tar": b"TAR-caddy"}
RESTORE_SCRIPT = "find /v -mindepth 1 -delete && tar -C /v -xf -"
EMPTY_CHECK = "find /v -mindepth 1 -print -quit"


def _restore_world(tmp_path: Path, *, rows: bytes = b"0\n", blobs: bytes = b"") -> tuple[World, Path]:
    world = make_world(tmp_path)
    stack(world, rendered())
    world.runner.on("psql", result=rows)
    world.runner.on("docker", "run", "find /v -mindepth 1 -print -quit", result=blobs)
    return world, write_config(tmp_path / "cfg")


def test_restore_argv_and_stdin(tmp_path: Path) -> None:
    world, config = _restore_world(tmp_path)
    archive = _archive(tmp_path / "b", FILES)
    with held_lock(world.ctx.run_dir):
        assert _main(world, "restore", str(archive), "--config", str(config)) == 0
    (pg,) = [c for c in world.runner.calls if "pg_restore" in c.argv]
    assert pg.argv == [
        "docker",
        "compose",
        "-p",
        "reader-vec",
        "exec",
        "-T",
        "postgres",
        "pg_restore",
        "--clean",
        "--if-exists",
        "-U",
        "postgres",
        "-d",
        "postgres",
    ]
    assert pg.stdin == b"DUMP"
    untar = {c.argv[c.argv.index("-v") + 1]: c for c in world.runner.calls if RESTORE_SCRIPT in c.argv}
    assert set(untar) == {"reader-vec_blobs:/v", "reader-vec_caddy_config:/v"}
    assert untar["reader-vec_blobs:/v"].argv == [
        "docker",
        "run",
        "--rm",
        "-i",
        "--entrypoint",
        "",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--user",
        "10001:10001",
        "-v",
        "reader-vec_blobs:/v",
        IMAGE,
        "sh",
        "-c",
        RESTORE_SCRIPT,
    ]
    assert untar["reader-vec_blobs:/v"].stdin == b"TAR-blobs"
    assert untar["reader-vec_caddy_config:/v"].stdin == b"TAR-caddy"
    assert "0:0" in untar["reader-vec_caddy_config:/v"].argv
    assert pg.timeout == 600
    assert {c.timeout for c in untar.values()} == {120}
    (stop,) = world.runner.argvs("stop")
    assert set(stop[5:]) == {"api", "worker", "caddy"}
    (check,) = [c for c in world.runner.calls if EMPTY_CHECK in c.argv]
    assert check.argv == [
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--user",
        "10001:10001",
        "-v",
        "reader-vec_blobs:/v:ro",
        IMAGE,
        "sh",
        "-c",
        EMPTY_CHECK,
    ]
    assert check.timeout == 120
    (rows,) = [c for c in world.runner.calls if "psql" in c.argv]
    assert rows.timeout == 120
    assert "'alembic_version'" in rows.argv[-1]
    assert "information_schema" in rows.argv[-1]


def test_restore_restarts_the_services_after_a_failed_pg_restore(tmp_path: Path) -> None:
    world, config = _restore_world(tmp_path)
    world.runner.on("pg_restore", result=(1, b"", b"error"))
    archive = _archive(tmp_path / "b", FILES)
    with held_lock(world.ctx.run_dir):
        assert _main(world, "restore", str(archive), "--config", str(config)) != 0
    (start,) = world.runner.argvs("start")
    assert set(start[5:]) == {"api", "worker", "caddy"}
    assert "restore_failed" in world.err


def test_restore_failed_check_reports_restore_failed(tmp_path: Path) -> None:
    world, config = _restore_world(tmp_path)
    world.runner.on("psql", result=(2, b"", b"psql: error"))
    archive = _archive(tmp_path / "b", FILES)
    with held_lock(world.ctx.run_dir):
        assert _main(world, "restore", str(archive), "--config", str(config)) == 1
    assert "restore_failed" in world.err
    assert world.runner.argvs("stop") == []


@pytest.mark.parametrize("manifest", ["not json", '{"files": [{"name": "db.dump"}]}', '{"files": 3}', "[]"])
def test_restore_malformed_manifest_is_bad_backup(tmp_path: Path, manifest: str) -> None:
    world, config = _restore_world(tmp_path)
    archive = _archive(tmp_path / "b", FILES)
    (archive / "manifest.json").write_text(manifest)
    with held_lock(world.ctx.run_dir):
        assert _main(world, "restore", str(archive), "--config", str(config)) == 1
    assert "bad_backup" in world.err
    assert "Traceback" not in world.err
    assert world.runner.argvs("stop") == []


@pytest.mark.parametrize("problem", ["sha", "rows", "blobs", "watcher"])
def test_restore_refuses_and_changes_nothing(tmp_path: Path, problem: str) -> None:
    world, config = _restore_world(
        tmp_path, rows=b"3\n" if problem == "rows" else b"0\n", blobs=b"/v/x\n" if problem == "blobs" else b""
    )
    archive = _archive(tmp_path / "b", FILES, corrupt="blobs.tar" if problem == "sha" else None)
    if problem == "watcher":
        code = _main(world, "restore", str(archive), "--config", str(config))
    else:
        with held_lock(world.ctx.run_dir):
            code = _main(world, "restore", str(archive), "--config", str(config))
    assert code != 0
    assert world.runner.argvs("stop") == []
    assert world.runner.argvs("pg_restore") == []
    assert not any(RESTORE_SCRIPT in c.argv for c in world.runner.calls)
