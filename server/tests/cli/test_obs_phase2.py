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
                                      "marker_failed": 5, "not_in_progress": 6}


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
    assert set(msgs) & set(store.PREMIGRATE_CODES.values()) == set()  # never overlap 0..6
