"""A real api process for INV-INGEST-9's kill-mid-body case (SRV-INGEST [db]): the test SIGKILLs it mid-body.

    python -I upload_server.py <reader.yaml> <blobs root> <port>   (database from READER_TEST_PG_* variables)
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


async def main(config_path: str, blobs: str, port: int) -> None:
    import uvicorn

    from reader.api.app import create_app
    from reader.auth.policy import settings_from_config
    from reader.config import Secret, load_config
    from reader.db import DbTarget, make_engine

    config = load_config(config_path)
    env = os.environ
    target = DbTarget(
        env["READER_TEST_PG_HOST"],
        int(env["READER_TEST_PG_PORT"]),
        env["READER_TEST_PG_USER"],
        env["READER_TEST_PG_DB"],
        Secret(env["READER_TEST_PG_PASSWORD"]),
    )
    app = create_app(settings_from_config(config), make_engine(target), run_cleanup_task=False)
    app.state.blobs_root = Path(blobs)
    app.state.ingest_materials = config.materials
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], int(sys.argv[3])))
