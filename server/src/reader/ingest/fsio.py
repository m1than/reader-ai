"""Small file-system helpers shared by the ingest commands (no inbox path here: INV-INGEST-8's scan)."""

from __future__ import annotations

import os
from pathlib import Path


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
