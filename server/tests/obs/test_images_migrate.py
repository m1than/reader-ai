"""SRV-OBS [image] (opt-in, READER_TEST_IMAGE_BUILD=1), phase 2's FIRST Red test: `mlflow-migrate`
(deploy/otel/mlflow-migrate.sh) under the service's own hardening, on every migrate and restore path of FR-OBS-29,
with named versions: N = MLflow 3.10.0 (the previous pin, by digest) and N+1 = reader-mlflow (MLflow 3.18.0, built by
id). Also FR-OBS-30's store.version carry-over through `reader backup` / `reader restore` (H2) and FR-CLI-24's
`restore-obs-premigrate` exit codes.

@trace FR-OBS-29 FR-OBS-30 FR-OBS-28 FR-CLI-24 FR-DEPLOY-15

Hardening of every migrate run: uid 10004, `--read-only` root filesystem, tmpfs /tmp, `--cap-drop ALL`,
`no-new-privileges`, `--network none`; the `obs_mlflow`-shaped volume is the only writable path. Zero credit.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from . import test_images as ti
from .conftest import REPO, require
from .test_images import sh

pytestmark = ti.pytestmark

N_IMAGE = "ghcr.io/mlflow/mlflow@sha256:f1e711271dd0c1efec7e896cb50d098acd4ba53781a5ab2a3a95167738a01e14"  # v3.10.0
N_VERSION, NEXT_VERSION = "3.10.0", "3.18.0"
SCRIPT = REPO / "deploy" / "otel" / "mlflow-migrate.sh"
DATA = "/var/lib/mlflow"
HARDEN = ["--user", "10004:10004", "--read-only", "--tmpfs", "/tmp:uid=10004", "--cap-drop", "ALL",
          "--security-opt", "no-new-privileges", "--network", "none", "-e", "HOME=/tmp"]
# mlflow-migrate's own exit codes (FR-OBS-29) and restore-obs-premigrate's (FR-CLI-24).
INCOMPLETE, RESTORED_OTHER, FAILED, FINISH_FAILED = 10, 11, 12, 13
RESTORED, STACK_UP, NO_BACKUP, COPY_FAILED, MARKER_FAILED, NOT_IN_PROGRESS = 0, 2, 3, 4, 5, 6


@pytest.fixture(scope="module")
def next_image() -> str:
    return ti.build_id("-f", str(REPO / "deploy/otel/mlflow.Dockerfile"), str(REPO))


@pytest.fixture
def volume() -> Iterator[str]:
    name = "reader-test-obsmig-" + secrets.token_hex(4)
    sh("docker", "volume", "create", name)
    # A fresh volume takes the ownership of the image's /var/lib/mlflow (uid 10004) on first mount: emulate it.
    sh("docker", "run", "--rm", "--network", "none", "--user", "0", "-v", f"{name}:{DATA}", "--entrypoint", "sh",
       N_IMAGE, "-c", f"chown 10004:10004 {DATA}")
    yield name
    sh("docker", "volume", "rm", "-f", name, check=False)


def _script() -> Path:
    assert SCRIPT.is_file(), f"{SCRIPT.relative_to(REPO)} is missing (FR-OBS-29, OPS-DEPLOY FR-DEPLOY-15)"
    return SCRIPT


def migrate(image: str, vol: str, *env: str, detach: bool = False) -> int | str:
    """One hardened `mlflow-migrate` run; its exit code (or the container id when detached)."""
    script = _script()
    cmd = ["docker", "run", *(["-d"] if detach else ["--rm"]), *HARDEN, *[a for e in env for a in ("-e", e)],
           "-v", f"{vol}:{DATA}", "-v", f"{script}:/opt/reader/mlflow-migrate.sh:ro", "--entrypoint", "sh", image,
           "/opt/reader/mlflow-migrate.sh"]
    if detach:
        return sh(*cmd).strip()
    proc = ti.subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=600)
    return proc.returncode


def in_volume(image: str, vol: str, script: str, *, root: bool = False, timeout: float = 300) -> str:
    """A hardened shell (or root, for file surgery the test needs) on the volume."""
    user = ["--user", "0", "--network", "none"] if root else HARDEN
    return sh("docker", "run", "--rm", *user, "-v", f"{vol}:{DATA}", "--entrypoint", "sh", image, "-c", script,
              timeout=timeout)


def read(image: str, vol: str, rel: str) -> str | None:
    out = in_volume(image, vol, f"test -f {DATA}/{rel} && cat {DATA}/{rel} || echo __absent__")
    return None if out.strip() == "__absent__" else out.strip()


def sha(image: str, vol: str, rel: str) -> str | None:
    out = in_volume(image, vol, f"test -f {DATA}/{rel} && sha256sum {DATA}/{rel} || echo __absent__")
    return None if "__absent__" in out else out.split()[0]


STORE = f"sqlite:///{DATA}/mlflow.db"
WRITE_OLD = (f"from mlflow.store.tracking.sqlalchemy_store import SqlAlchemyStore as S; "
             f"S('{STORE}', '{DATA}/artifacts').create_experiment('old')")
SERVE = f"""
(mlflow server --host 127.0.0.1 --port 5000 --workers 1 --backend-store-uri {STORE} \
 --artifacts-destination {DATA}/artifacts >/tmp/server.log 2>&1 &)
python - <<'PY'
import json, time, urllib.request
for _ in range(120):
    try:
        if urllib.request.urlopen("http://127.0.0.1:5000/health", timeout=2).status == 200:
            break
    except Exception:
        time.sleep(1)
def get(p):
    try:
        return json.load(urllib.request.urlopen("http://127.0.0.1:5000" + p, timeout=10))
    except Exception as e:
        return {{"error": str(e)[:80]}}
exps = get("/api/2.0/mlflow/experiments/get-by-name?experiment_name=old")
req = urllib.request.Request("http://127.0.0.1:5000/ajax-api/3.0/mlflow/traces/search",
    data=json.dumps({{"locations": [{{"type": "MLFLOW_EXPERIMENT", "mlflow_experiment": {{"experiment_id": "0"}}}}],
                      "max_results": 50}}).encode(), headers={{"Content-Type": "application/json"}})
try:
    traces = len(json.load(urllib.request.urlopen(req, timeout=10)).get("traces", []))
except Exception:
    traces = -1
print(json.dumps({{"old": "experiment" in exps, "traces": traces, "health": True}}))
PY
"""


def serve(image: str, vol: str) -> dict[str, object]:
    out = in_volume(image, vol, SERVE)
    return json.loads(out.strip().splitlines()[-1])


def n_store_with_data(vol: str) -> None:
    """A store as the previous pin leaves it: migrated by N's mlflow-migrate (store.version = N), holding `old`."""
    assert migrate(N_IMAGE, vol) == 0
    assert read(N_IMAGE, vol, "store.version") == N_VERSION
    in_volume(N_IMAGE, vol, f'python -c "{WRITE_OLD}"')


# --- the upgrade path ---


def test_n_to_next_upgrades_with_one_backup(volume: str, next_image: str) -> None:
    n_store_with_data(volume)
    assert migrate(next_image, volume) == 0
    assert read(next_image, volume, "store.version") == NEXT_VERSION
    assert read(next_image, volume, "backup/pre-migrate.version") == N_VERSION
    count = "select count(*) from experiments"  # a constant: no value is interpolated
    opened = in_volume(next_image, volume, f"python -c \"import sqlite3; c = sqlite3.connect('file:{DATA}/backup/"
                       f"pre-migrate.db?mode=ro', uri=True); print(c.execute('{count}').fetchone()[0])\"")
    assert int(opened.strip()) >= 2  # Default and `old`: the backup opens and holds N's data
    for marker in ("backup/in-progress", "backup/fresh", "backup/restored-for"):
        assert read(next_image, volume, marker) is None, marker
    mode = in_volume(next_image, volume, f"python -c \"import sqlite3; print(sqlite3.connect('{DATA}/mlflow.db')"
                     ".execute('pragma journal_mode').fetchone()[0])\"")
    assert mode.strip() == "wal"  # FR-OBS-28
    assert serve(next_image, volume)["old"] is True


def test_two_migrates_at_once_serialise_on_flock(volume: str, next_image: str) -> None:
    n_store_with_data(volume)
    first = str(migrate(next_image, volume, detach=True))
    second = str(migrate(next_image, volume, detach=True))
    codes = [int(sh("docker", "wait", c, timeout=600).strip()) for c in (first, second)]
    for c in (first, second):
        sh("docker", "rm", "-f", c, check=False)
    assert codes == [0, 0]
    assert read(next_image, volume, "store.version") == NEXT_VERSION
    assert read(next_image, volume, "backup/pre-migrate.version") == N_VERSION  # one backup, of N's store
    assert serve(next_image, volume)["old"] is True


def test_a_matching_store_version_skips_backup_and_markers(volume: str, next_image: str) -> None:
    n_store_with_data(volume)
    assert migrate(next_image, volume) == 0
    in_volume(next_image, volume, f"rm {DATA}/backup/pre-migrate.db")
    assert migrate(next_image, volume) == 0  # a normal start: store.version equals the image's
    assert read(next_image, volume, "backup/pre-migrate.db") is None
    assert read(next_image, volume, "backup/in-progress") is None


# --- a failed migration, the restore, the pin ---


def test_failed_upgrade_then_rerun_then_restore_then_pins(volume: str, next_image: str) -> None:
    n_store_with_data(volume)
    in_volume(next_image, volume, f"chmod 0444 {DATA}/mlflow.db", root=True)  # step (3) cannot write
    assert migrate(next_image, volume) == FAILED
    assert read(next_image, volume, "backup/in-progress") is not None
    before = sha(next_image, volume, "backup/pre-migrate.db")
    assert before is not None
    in_volume(next_image, volume, f"chmod 0644 {DATA}/mlflow.db", root=True)
    assert migrate(next_image, volume) == INCOMPLETE  # stops at (0): the backup is never overwritten
    assert sha(next_image, volume, "backup/pre-migrate.db") == before
    store = require("reader.cli.obs_store")
    code, out = store.restore_premigrate(volume=volume, image=next_image, force=False)
    assert code == RESTORED and N_VERSION in out
    assert read(next_image, volume, "backup/restored-for") == N_VERSION
    assert read(next_image, volume, "store.version") == N_VERSION
    assert read(next_image, volume, "backup/in-progress") is None
    assert migrate(next_image, volume) == RESTORED_OTHER  # N+1 refuses the restored file
    assert migrate(N_IMAGE, volume) == 0  # the owner reverted the pin: N starts
    assert read(N_IMAGE, volume, "backup/restored-for") is None
    assert serve(N_IMAGE, volume)["old"] is True


def test_restore_without_in_progress_needs_force(volume: str, next_image: str) -> None:
    n_store_with_data(volume)
    assert migrate(next_image, volume) == 0
    store = require("reader.cli.obs_store")
    code, _ = store.restore_premigrate(volume=volume, image=next_image, force=False)
    assert code == NOT_IN_PROGRESS
    code, out = store.restore_premigrate(volume=volume, image=next_image, force=True)
    assert code == RESTORED and N_VERSION in out  # --force prints the version it rolls back to


def test_restore_codes_for_a_missing_backup(volume: str, next_image: str) -> None:
    assert migrate(next_image, volume) == 0  # fresh: no pre-migrate.db
    store = require("reader.cli.obs_store")
    code, _ = store.restore_premigrate(volume=volume, image=next_image, force=True)
    assert code == NO_BACKUP


# --- fresh volumes ---


def test_fresh_volume_via_db_upgrade(volume: str, next_image: str) -> None:
    assert migrate(next_image, volume) == 0
    assert read(next_image, volume, "store.version") == NEXT_VERSION
    assert read(next_image, volume, "backup/fresh") is None
    assert read(next_image, volume, "backup/pre-migrate.db") is None  # nothing to back up
    served = serve(next_image, volume)
    assert served["health"] is True and served["traces"] == 0


def test_fresh_volume_via_the_fallback(volume: str, next_image: str) -> None:
    assert migrate(next_image, volume, "READER_MLFLOW_MIGRATE_FALLBACK=1") == 0  # db upgrade forced off
    assert read(next_image, volume, "store.version") == NEXT_VERSION
    assert read(next_image, volume, "backup/fresh") is None  # cleared before the server's first start (H1)
    assert serve(next_image, volume)["health"] is True


@pytest.mark.parametrize("fallback", [False, True])
def test_a_killed_first_fresh_migrate_recovers(volume: str, next_image: str, fallback: bool) -> None:
    env = ("READER_MLFLOW_MIGRATE_FALLBACK=1",) if fallback else ()
    cid = str(migrate(next_image, volume, *env, detach=True))
    time.sleep(1.0)
    running = sh("docker", "inspect", "--format", "{{.State.Running}}", cid).strip()
    sh("docker", "kill", cid, check=False)
    sh("docker", "rm", "-f", cid, check=False)
    assert running == "true", "the first migrate finished before the kill: the case was not exercised"
    assert migrate(next_image, volume, *env) == 0  # G1: step (0) recovers from backup/fresh
    assert read(next_image, volume, "backup/fresh") is None
    assert serve(next_image, volume)["health"] is True


def test_a_served_fallback_store_keeps_its_trace(volume: str, next_image: str) -> None:
    assert migrate(next_image, volume, "READER_MLFLOW_MIGRATE_FALLBACK=1") == 0
    in_volume(next_image, volume, f"python -c \"import mlflow; mlflow.set_tracking_uri('{STORE}'); "
              "s = mlflow.start_span_no_context('kept'); s.end()\"")  # 3.18: start_span is a context manager
    assert serve(next_image, volume)["traces"] == 1
    assert migrate(next_image, volume) == 0  # H1: a served store is never wiped
    assert serve(next_image, volume)["traces"] == 1


# --- H2: a backup from N, restored under N+1 ---


def test_an_n_backup_restored_under_next_migrates(volume: str, next_image: str, tmp_path: Path) -> None:
    n_store_with_data(volume)
    store = require("reader.cli.obs_store")
    store.backup(volume=volume, image=N_IMAGE, directory=tmp_path)
    assert (tmp_path / "obs_mlflow.db").is_file()
    assert (tmp_path / "obs_mlflow.store.version").read_text().strip() == N_VERSION
    assert migrate(next_image, volume) == 0  # the live volume moves on to N+1
    in_volume(next_image, volume, f"echo stale > {DATA}/backup/in-progress")
    store.restore(directory=tmp_path, volume=volume, image=next_image)
    assert read(next_image, volume, "store.version") == N_VERSION  # carried back, so (2)-(3) run
    assert read(next_image, volume, "backup/in-progress") is None  # the replaced store's markers are gone
    assert migrate(next_image, volume) == 0
    assert read(next_image, volume, "backup/pre-migrate.version") == N_VERSION
    assert read(next_image, volume, "store.version") == NEXT_VERSION
    assert serve(next_image, volume)["old"] is True


def test_a_backup_without_store_version_restores_without_one(volume: str, next_image: str, tmp_path: Path) -> None:
    in_volume(N_IMAGE, volume, f'python -c "{WRITE_OLD}"')  # a store made before store.version existed
    store = require("reader.cli.obs_store")
    store.backup(volume=volume, image=next_image, directory=tmp_path)
    assert not (tmp_path / "obs_mlflow.store.version").exists()
    assert migrate(next_image, volume) == 0
    store.restore(directory=tmp_path, volume=volume, image=next_image)
    assert read(next_image, volume, "store.version") is None
    assert migrate(next_image, volume) == 0
    assert read(next_image, volume, "backup/pre-migrate.version") == "3.18.0"  # FR-OBS-29: the declared legacy value


def test_the_backup_is_byte_stable_under_reruns(volume: str, next_image: str) -> None:
    """FR-OBS-29: a store at the image's version is never backed up again, so pre-migrate.db keeps N's bytes."""
    n_store_with_data(volume)
    assert migrate(next_image, volume) == 0
    first = sha(next_image, volume, "backup/pre-migrate.db")
    assert migrate(next_image, volume) == 0
    assert sha(next_image, volume, "backup/pre-migrate.db") == first
    assert hashlib.sha256(b"").hexdigest() != first
