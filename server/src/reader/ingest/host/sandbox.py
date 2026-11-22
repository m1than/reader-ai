"""The converter child's command line, environment and profile (SRV-INGEST FR-INGEST-4)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Final

PROFILE: Final = Path(__file__).resolve().parent / "converter.sb"
SANDBOX_EXEC: Final = "/usr/bin/sandbox-exec"


def _real(path: Path) -> str:
    """The profile matches resolved paths (measured: WORK=/tmp/x denied a write to /private/tmp/x/f)."""
    return os.path.realpath(path)


def child_argv(venv: Path, pyroot: Path, models: Path, work: Path) -> list[str]:
    real_venv, real_work, real_models = _real(venv), _real(work), _real(models)
    return [
        SANDBOX_EXEC,
        "-f",
        str(PROFILE),
        "-D",
        f"PYROOT={_real(pyroot)}",
        "-D",
        f"VENV={real_venv}",
        "-D",
        f"MODELS={real_models}",
        "-D",
        f"WORK={real_work}",
        f"{real_venv}/bin/python",
        "-I",
        "-m",
        "reader.ingest.host.convert",
        "--input",
        f"{real_work}/in/material.pdf",
        "--models",
        real_models,
        "--out",
        f"{real_work}/out",
    ]


def child_env(work: Path, cpus: int) -> dict[str, str]:
    """Exactly these variables (`env -i` semantics); HOME and TMPDIR inside WORK."""
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(work / "home"),
        "TMPDIR": str(work / "tmp"),
        "LANG": "en_US.UTF-8",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "OMP_NUM_THREADS": str(cpus),
    }


def profile_rules(text: str) -> list[str]:
    """The top-level forms of a profile, without comments and with whitespace collapsed (rule-for-rule)."""
    body = re.sub(r";[^\n]*", "", text)
    rules: list[str] = []
    depth, start = 0, 0
    for index, char in enumerate(body):
        if char == "(":
            if depth == 0:
                start = index
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                rules.append(" ".join(body[start : index + 1].split()).replace("( ", "(").replace(" )", ")"))
    return rules
