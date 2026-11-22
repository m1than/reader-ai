"""`reader-ingest --setup`: the converter's own venv and its pinned models (SRV-INGEST FR-INGEST-3).

The venv holds the hash-pinned closure of Docling 2.151.1 (`requirements.lock`, generated with
`MACOSX_DEPLOYMENT_TARGET=14.0 uv pip compile <docling==2.151.1> -c <the ingest-confine spike's freeze>
--generate-hashes --python-version 3.14 --python-platform aarch64-apple-darwin`) and a non-editable `reader`
wheel installed with `--no-deps`, so the sandbox profile reads the converter's code from the venv, never from
`server/src`. The models are the commits `models.lock.json` names, verified file by file, then made read-only.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final

from reader.ingest import CONVERTER_VERSION
from reader.ingest.host.closure import SRC, source_sha256

HERE: Final = Path(__file__).resolve().parent
REQUIREMENTS_LOCK: Final = HERE / "requirements.lock"
MODELS_LOCK: Final = HERE / "models.lock.json"
SERVER: Final = SRC.parent  # the directory holding pyproject.toml
SETUP_RECORD: Final = "setup.json"


def venv_dir(data_dir: Path) -> Path:
    return data_dir / "ingest" / f"venv-{CONVERTER_VERSION}"


def models_dir(data_dir: Path) -> Path:
    return data_dir / "models" / "docling-2.151.1"


def pyroot_of(venv: Path) -> Path:
    """The CPython prefix the venv's `bin/python` resolves to (the profile's PYROOT)."""
    return Path(os.path.realpath(venv / "bin" / "python")).parents[1]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expected_record() -> dict[str, str]:
    return {
        "converter_version": CONVERTER_VERSION,
        "converter_source_sha256": source_sha256(),
        "requirements_lock_sha256": _sha256_file(REQUIREMENTS_LOCK),
        "models_lock_sha256": _sha256_file(MODELS_LOCK),
    }


def _models_problem(models: Path) -> str | None:
    lock = json.loads(MODELS_LOCK.read_text(encoding="utf-8"))
    for repo in lock["repos"]:
        for rel, digest in repo["files"].items():
            path = models / repo["folder"] / rel
            if not path.is_file():
                return "setup_missing"
            if _sha256_file(path) != digest:
                return "setup_stale"
    return None


def check_setup(data_dir: Path) -> str | None:
    """None when the converter venv and models match the checkout, else `setup_missing` or `setup_stale`."""
    venv = venv_dir(data_dir)
    record_path = venv / SETUP_RECORD
    if not (venv / "bin" / "python").exists() or not record_path.is_file():
        return "setup_missing"
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except ValueError:
        return "setup_stale"
    if record != expected_record():
        return "setup_stale"
    return _models_problem(models_dir(data_dir))


Run = Callable[[list[str], Mapping[str, str]], int]


def _run(argv: list[str], env: Mapping[str, str]) -> int:
    return subprocess.run(argv, env=dict(env), check=False).returncode  # noqa: S603 (argv built here)


# Model tree modes (FR-INGEST-3): directories 0555 and files 0444 once verified; writable while fetching.
_DIR_WRITABLE: Final = 0o755
_FILE_WRITABLE: Final = 0o644
_DIR_READ_ONLY: Final = 0o555
_FILE_READ_ONLY: Final = 0o444


def _chmod_tree(tree: Path, dir_mode: int, file_mode: int) -> None:
    for root, dirs, files in os.walk(tree, topdown=False):
        for name in files:
            path = os.path.join(root, name)
            if not os.path.islink(path):
                os.chmod(path, file_mode)
        for name in dirs:
            os.chmod(os.path.join(root, name), dir_mode)
    os.chmod(tree, dir_mode)


def _writable(tree: Path) -> None:
    for root, dirs, _ in os.walk(tree):  # top-down, so each directory is writable before its children
        os.chmod(root, _DIR_WRITABLE)
        for name in dirs:
            os.chmod(os.path.join(root, name), _DIR_WRITABLE)
    _chmod_tree(tree, _DIR_WRITABLE, _FILE_WRITABLE)


def _read_only(tree: Path) -> None:
    _chmod_tree(tree, _DIR_READ_ONLY, _FILE_READ_ONLY)


def run_setup(
    data_dir: Path, env: Mapping[str, str], report: Callable[[str], None], run: Run = _run
) -> str | None:
    """Build the venv, install the lock and the wheel, fetch and verify the models; None or an error code."""
    uv = shutil.which("uv", path=env.get("PATH"))
    if uv is None:
        return "uv_missing"
    venv = venv_dir(data_dir)
    if venv.exists():
        shutil.rmtree(venv)
    venv.parent.mkdir(parents=True, exist_ok=True)
    report("setup: creating the converter venv")
    if run([uv, "venv", "--python", "3.14", "--no-project", str(venv)], env) != 0:
        return "setup_venv_failed"
    python = str(venv / "bin" / "python")
    report("setup: installing the hash-pinned converter lock")
    install = [uv, "pip", "install", "--python", python, "--require-hashes", "--no-deps"]
    if run([*install, "-r", str(REQUIREMENTS_LOCK)], env) != 0:
        return "setup_install_failed"
    with tempfile.TemporaryDirectory(prefix="reader-ingest-wheel-") as tmp:
        report("setup: building and installing the reader wheel (non-editable)")
        if run([uv, "build", "--wheel", "--out-dir", tmp, str(SERVER)], env) != 0:
            return "setup_wheel_failed"
        wheels = sorted(Path(tmp).glob("reader-*.whl"))
        if len(wheels) != 1:
            return "setup_wheel_failed"
        if run([uv, "pip", "install", "--python", python, "--no-deps", str(wheels[0])], env) != 0:
            return "setup_wheel_failed"
    models = models_dir(data_dir)
    if _models_problem(models) is not None:
        report("setup: fetching the pinned models")
        models.mkdir(parents=True, exist_ok=True)
        _writable(models)
        fetch_env = {**env, "HF_HUB_DISABLE_TELEMETRY": "1"}
        fetch = [python, "-I", "-m", "reader.ingest.host.fetch", str(models), str(MODELS_LOCK)]
        if run(fetch, fetch_env) != 0:
            return "setup_fetch_failed"
        problem = _models_problem(models)
        if problem is not None:
            return "setup_models_mismatch"
    _read_only(models)
    record: dict[str, Any] = expected_record()
    (venv / SETUP_RECORD).write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    report("setup: done")
    return None
