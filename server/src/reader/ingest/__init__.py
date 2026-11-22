"""Ingestion (SRV-INGEST, slice): a declared PDF becomes ordered spans and an outline (ADR-0021, ADR-0027).

Stdlib only: the confined converter imports this package from its own venv, which holds Docling and the
`reader` wheel installed without dependencies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

# FR-INGEST-10: the `r` number rises with any change to FR-INGEST-5..9.
CONVERTER_VERSION: Final = "docling-2.151.1+r1"
# FR-INGEST-11: the mount path of the `blobs` volume in OPS-DEPLOY's compose.yaml.
BLOBS_DIR: Final = Path("/var/lib/reader/blobs")
MIGRATIONS: Final = Path(__file__).resolve().parent / "migrations"
