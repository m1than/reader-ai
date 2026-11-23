"""Download the pinned model revisions (SRV-INGEST FR-INGEST-3). Runs in the converter venv, with network.

    <converter venv>/bin/python -I -m reader.ingest.host.fetch <models dir> <models.lock.json>

Each repository goes to `<models dir>/<repo_id with / as -->` at the locked commit, the layout
`docling-tools models download` produces and `artifacts_path` expects. Hashes are verified by the caller.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        sys.stderr.write("usage: fetch <models dir> <models.lock.json>\n")
        return 2
    from huggingface_hub import snapshot_download

    models, lock = Path(args[0]), json.loads(Path(args[1]).read_text(encoding="utf-8"))
    for repo in lock["repos"]:
        snapshot_download(
            repo_id=repo["repo_id"],
            revision=repo["revision"],
            local_dir=models / repo["folder"],
            allow_patterns=sorted(repo["files"]),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
