"""OPS-CLI FR-CLI-24 (paired SRV-OBS phase 2 change) [unit]: the doctor and status lines for mlflow, rotel and
jaeger from canned `docker inspect` / `docker system df -v`, the 80 % store warning, the backup and restore
command sequences in order, restore-obs-premigrate's exit codes and `reader up`'s message per mlflow-migrate
code.

@trace FR-CLI-24 FR-OBS-29 FR-OBS-30 FR-OBS-31 FR-OBS-32
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest

GIB = 1024**3
JOBS = {"obs.retention": "2026-11-25T03:00:00Z", "obs.setup": "2026-11-25T08:00:00Z"}


def _store() -> Any:
    try:
        return importlib.import_module("reader.cli.obs_store")
    except ModuleNotFoundError as e:
        pytest.fail(f"reader.cli.obs_store is not implemented yet: {e}")


def _inspect(name: str, running: bool = True, restarts: int = 0, health: str | None = None) -> dict[str, Any]:
    state: dict[str, Any] = {"Running": running, "Status": "running" if running else "exited"}
    if health:
        state["Health"] = {"Status": health}
    return {"Name": f"/reader-{name}-1", "Config": {"Image": f"img-{name}"}, "RestartCount": restarts,
            "State": state}


def _df(size: str) -> str:
    volumes = [{"Name": "reader_obs_mlflow", "Size": size}, {"Name": "reader_pg", "Size": "1GB"}]
    return json.dumps({"Volumes": volumes})


def test_doctor_lines_per_member() -> None:
    store = _store()
    inspect = [_inspect("mlflow", health="healthy"), _inspect("rotel", restarts=2), _inspect("jaeger", False)]
    lines = store.doctor_lines(inspect=inspect, df=_df("1.2GB"), journal_mode="wal", last_jobs=JOBS)
    assert lines[0].startswith("obs mlflow: img-mlflow running, restarts 0, health healthy")
    assert lines[1] == "obs rotel: img-rotel running, restarts 2"
    assert lines[2] == "obs jaeger: img-jaeger not running, restarts 0"
    assert "store 1.2GB" in lines[0]
    assert "journal wal" in lines[0]
    assert any("obs.retention 2026-11-25T03:00:00Z" in ln for ln in lines)
    assert not any("obs_store_large" in ln for ln in lines)


@pytest.mark.parametrize(("size", "warn"), [("3.2GB", False), ("3.5GB", True), ("4.1GB", True)])
def test_store_warning_at_80_percent_of_4_gib(size: str, warn: bool) -> None:
    store = _store()
    assert store.STORE_BYTES_MAX == 4 * GIB
    lines = store.doctor_lines(inspect=[_inspect("mlflow")], df=_df(size), journal_mode="wal", last_jobs={})
    assert any("obs_store_large" in ln for ln in lines) is warn  # 0.8 of 4 GiB = 3.436 GB


def test_a_fresh_store_line() -> None:
    store = _store()
    assert store.fresh_line("f4a9b2c7d1e6") == "obs mlflow: fresh store, schema f4a9b2c7d1e6"


def test_backup_argv_uses_the_backup_api_and_copies_store_version(tmp_path: Path) -> None:
    store = _store()
    steps = store.backup_steps(volume="reader_obs_mlflow", image="img", directory=tmp_path)
    joined = [" ".join(s) for s in steps]
    assert any(".backup(" in j and "obs_mlflow.db" in j for j in joined)  # SQLite's backup API, up or down
    assert any("artifacts" in j for j in joined)
    assert any("store.version" in j and "obs_mlflow.store.version" in j for j in joined)


def test_restore_order(tmp_path: Path) -> None:
    store = _store()
    raw = store.restore_steps(directory=tmp_path, volume="reader_obs_mlflow", image="img")
    steps = [" ".join(s) for s in raw]
    needles = ("rm -f /var/lib/mlflow/store.version", "/var/lib/mlflow/backup/in-progress", ".backup(",
               "> /var/lib/mlflow/store.version")
    order = [next(i for i, s in enumerate(steps) if needle in s) for needle in needles]
    assert order == sorted(order)
    rm = next(s for s in steps if "backup/in-progress" in s)
    assert "backup/fresh" in rm
    assert "backup/restored-for" in rm


def test_restore_premigrate_exit_codes() -> None:
    store = _store()
    assert store.PREMIGRATE_CODES == {"restored": 0, "stack_up": 2, "no_backup": 3, "copy_failed": 4,
                                      "marker_failed": 5, "not_in_progress": 6, "no_version": 7}


def test_up_reports_each_migrate_code() -> None:
    store = _store()
    msgs = {code: store.migrate_message(code, restored_for="3.10.0") for code in (10, 11, 12, 13)}
    prefixes = {10: "obs_migration_incomplete", 11: "obs_restored_for_other_version",
                12: "obs_migration_failed", 13: "obs_migration_finish_failed"}
    for code, prefix in prefixes.items():
        assert msgs[code].startswith(prefix), code
    for code in (10, 12, 13):
        assert "restore-obs-premigrate" in msgs[code], code
    assert "3.10.0" in msgs[11]
    assert "pin" in msgs[11]
    assert set(msgs) & set(store.PREMIGRATE_CODES.values()) == set()  # never overlap 0..7


# --- the copy order (B3): copy into a temp file first, then drop -wal/-shm, then move into place ---


def _in_order(script: str, *needles: str) -> bool:
    at = [script.index(n) for n in needles]
    return at == sorted(at)


def test_restore_swap_copies_before_it_removes_the_wal(tmp_path: Path) -> None:
    store = _store()
    (swap,) = [s[-1] for s in store.restore_steps(directory=tmp_path, volume="v", image="img")
               if "mlflow.db.restore" in s[-1]]
    assert _in_order(swap, ".backup(", "mlflow.db-wal", "mv /var/lib/mlflow/mlflow.db.restore")


def test_premigrate_refuses_without_a_version_then_copies_before_it_removes_the_wal() -> None:
    script = _store()._PREMIGRATE_SH
    assert "echo unknown" not in script  # code review round 1: never a restore for an unknown version
    assert _in_order(script, "exit 7", ".backup(", "rm -f $D/mlflow.db-wal", "mv $D/mlflow.db.restore")


# --- command level: cmd_restore, cmd_backup, cmd_up and cmd_status with the obs volume ---

MLFLOW_IMAGE = "reader-mlflow:local"
OBS_FILES = {"obs_mlflow.db": b"OBS-DB", "obs_mlflow.store.version": b"3.10.0\n"}


def _obs_model() -> dict[str, Any]:
    from .conftest import rendered

    model = rendered()
    mount = {"type": "volume", "source": "obs_mlflow", "target": "/var/lib/mlflow"}
    model["services"]["mlflow"] = {"image": MLFLOW_IMAGE, "user": "10004:10004", "volumes": [mount]}
    labels = {"org.reader-ai.backup": "include"}
    model["volumes"]["obs_mlflow"] = {"name": "reader-vec_obs_mlflow", "labels": labels}
    return model


def _obs_world(tmp_path: Path) -> tuple[Any, Path]:
    from .conftest import make_world, stack, write_config

    world = make_world(tmp_path)
    stack(world, _obs_model(), volume_exists=True)
    world.runner.on("psql", result=b"0\n")
    world.runner.on("pg_dump", result=b"DUMP")
    world.runner.on("postgres", "-c", "SHOW server_version", result=b"18.7\n")
    return world, write_config(tmp_path / "cfg")


def _restore(world: Any, config: Path, tmp_path: Path) -> int:
    from reader.cli.app import main

    from .conftest import held_lock
    from .test_maintenance import FILES, _archive

    archive = _archive(tmp_path / "b", {**FILES, **OBS_FILES})
    with held_lock(world.ctx.run_dir):
        return int(main(["restore", str(archive), "--config", str(config)], ctx=world.ctx))


def _index(world: Any, *tokens: str) -> int:
    from .conftest import _subsequence

    return next(i for i, c in enumerate(world.runner.calls) if _subsequence(tokens, c.argv))


def test_restore_runs_mlflow_migrate_before_mlflow_starts(tmp_path: Path) -> None:
    """O8: without the migrate run this fails; the migrate is a fresh one-shot, after the store's copy."""
    world, config = _obs_world(tmp_path)
    assert _restore(world, config, tmp_path) == 0
    (migrate,) = world.runner.argvs("compose", "run", "--rm", "mlflow-migrate")
    assert "observability" in migrate
    swap = next(i for i, c in enumerate(world.runner.calls) if "mlflow.db.restore" in c.argv[-1])
    version = next(i for i, c in enumerate(world.runner.calls)
                   if "> /var/lib/mlflow/store.version" in c.argv[-1])
    ran = _index(world, "compose", "run", "--rm", "mlflow-migrate")
    (start,) = world.runner.argvs("compose", "-p", "reader-vec", "start")
    assert swap < version < ran < _index(world, "compose", "-p", "reader-vec", "start")
    assert "mlflow" in start[5:]
    swap_script = world.runner.calls[swap].argv[-1]
    assert _in_order(swap_script, ".backup(", "mlflow.db-wal", "mv ")  # O10: copy first


@pytest.mark.parametrize("code", [10, 11, 12, 13, 1])
def test_a_failed_migrate_leaves_mlflow_stopped(tmp_path: Path, code: int) -> None:
    """O9: mlflow is started again only when mlflow-migrate exited 0; the rest of the stack starts."""
    world, config = _obs_world(tmp_path)
    world.runner.on("compose", "run", "--rm", "mlflow-migrate", result=code)
    assert _restore(world, config, tmp_path) != 0
    assert "restore_failed" in world.err
    if code in _store().MIGRATE_CODES:
        assert _store().MIGRATE_CODES[code] in world.err
    (start,) = world.runner.argvs("compose", "-p", "reader-vec", "start")
    assert "mlflow" not in start[5:]
    assert {"api", "worker", "caddy"} <= set(start[5:])


def test_backup_writes_the_three_obs_files(tmp_path: Path) -> None:
    from reader.cli.app import main

    from .conftest import Call, held_lock

    world, config = _obs_world(tmp_path)

    def write(call: Call) -> None:
        (out,) = [a.rsplit(":/out", 1)[0] for a in call.argv if a.endswith(":/out")]
        script = call.argv[-1]
        name = ("obs_mlflow.db" if ".backup(" in script else
                "obs_mlflow.artifacts.tar" if "artifacts" in script else "obs_mlflow.store.version")
        Path(out, name).write_bytes(b"x-" + name.encode())

    world.runner.on("docker", "run", "--entrypoint", "sh", MLFLOW_IMAGE, handler=write)
    dest = tmp_path / "backup"
    with held_lock(world.ctx.run_dir):
        assert main(["backup", str(dest), "--config", str(config)], ctx=world.ctx) == 0
    names = {f["name"] for f in json.loads((dest / "manifest.json").read_text())["files"]}
    assert {"obs_mlflow.db", "obs_mlflow.artifacts.tar", "obs_mlflow.store.version"} <= names
    assert (dest / "obs_mlflow.store.version").read_bytes() == b"x-obs_mlflow.store.version"
    assert "obs_mlflow.tar" not in names  # never as a volume tar (FR-OBS-30)


def test_up_reports_the_migrate_code_when_up_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from .conftest import stack
    from .test_observability import _cfg
    from .test_profiles import _up, _world

    world = _world(tmp_path, monkeypatch, agents_in_model=False)
    stack(world, _obs_model(), volume_exists=True)
    world.runner.on("up", "-d", result=(1, b"", b"service mlflow-migrate didn't complete successfully"))
    world.runner.on("docker", "inspect", "--format", "{{.State.ExitCode}}", result=b"12\n")
    assert _up(world, _cfg(tmp_path, True)) != 0
    assert "obs_migration_failed" in world.err
    assert "restore-obs-premigrate" in world.err


def test_status_and_doctor_json_carry_the_obs_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from reader.cli import maintenance
    from reader.cli.app import main

    from .conftest import held_lock
    from .test_observability import _cfg

    world, _ = _obs_world(tmp_path)
    config = _cfg(tmp_path, True)
    world.ctx.run_dir.mkdir(parents=True, exist_ok=True)
    (world.ctx.run_dir / "profiles").write_text("observability\n")
    world.runner.on("docker", "inspect", "reader-vec-mlflow-1",
                    result=json.dumps([_inspect("mlflow", health="healthy")]).encode())
    with held_lock(world.ctx.run_dir):
        main(["status", "--config", str(config)], ctx=world.ctx)
    assert "obs mlflow: img-mlflow running" in world.out
    world.ctx.out.seek(0)
    world.ctx.out.truncate()
    with held_lock(world.ctx.run_dir):
        main(["status", "--json", "--config", str(config)], ctx=world.ctx)
    assert any(x.startswith("obs mlflow:") for x in json.loads(world.out)["obs"])
    world.ctx.out.seek(0)
    world.ctx.out.truncate()
    monkeypatch.setattr(maintenance, "gate_status", lambda _url, _roots: 401)
    maintenance.cmd_doctor(world.ctx, config, True)
    listed = json.loads(world.out.strip().splitlines()[-1])
    obs = [c for c in listed if c["name"] == "obs"]
    assert any(c["detail"].startswith("obs mlflow:") and c["ok"] is True for c in obs)
