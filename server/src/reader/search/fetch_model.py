"""Build-time model download (SRV-SEARCH FR-SEARCH-2, ADR-0028): exactly the files of models.lock.json.

Run by deploy/images/embed.Dockerfile: `python -I -m reader.search.fetch_model <dest>`. Each file comes from
huggingface.co at the lock's pinned revision and is checked against its sha256 and size; any mismatch exits 1
and fails the build. Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any, Final

LOCK_FILE: Final = Path(__file__).resolve().parent / "models.lock.json"
BASE: Final = "https://huggingface.co"
CHUNK: Final = 1 << 20


def fetch(dest: Path, lock: dict[str, Any]) -> None:
    for name, meta in sorted(lock["files"].items()):
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        url = f"{BASE}/{lock['repo']}/resolve/{lock['revision']}/{name}"
        digest = hashlib.sha256()
        size = 0
        with urllib.request.urlopen(url, timeout=120) as response, target.open("wb") as out:  # noqa: S310
            while block := response.read(CHUNK):
                digest.update(block)
                size += len(block)
                out.write(block)
        if digest.hexdigest() != meta["sha256"] or size != meta["size"]:
            target.unlink()
            raise SystemExit(f"model file mismatch: {name}")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -I -m reader.search.fetch_model <dest>", file=sys.stderr)
        return 2
    fetch(Path(args[0]), json.loads(LOCK_FILE.read_text(encoding="utf-8")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
