"""Structured logging that refuses secrets (FR-CLI-19)."""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Final

from reader.config import Secret

LOGGER: Final = logging.getLogger("reader.cli")
LOG_MAX_BYTES: Final = 10 * 1024 * 1024  # structural cap, rotated once


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: object) -> None:
    for key, value in fields.items():
        if isinstance(value, Secret):
            raise TypeError(f"log field {key!r} holds a Secret")
    logger.log(level, "%s %s", event, " ".join(f"{k}={v}" for k, v in sorted(fields.items())))


def open_watcher_log(log_dir: Path) -> RotatingFileHandler:
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = log_dir / "watcher.log"
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600))
    path.chmod(0o600)
    handler = RotatingFileHandler(path, maxBytes=LOG_MAX_BYTES, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    return handler
