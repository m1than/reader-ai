"""Ingestion (SRV-INGEST, slice): a declared PDF becomes ordered spans and an outline (ADR-0021, ADR-0027).

Stdlib only: the confined converter imports this package from its own venv, which holds Docling and the
`reader` wheel installed without dependencies.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

# FR-INGEST-10: the `r` number rises with any change to FR-INGEST-5..9 or the parse config; r3 is revision 3's
# artifact format 2 (locators, quote hashes, parse_config, pack_id; FR-INGEST-40 step 1).
CONVERTER_VERSION: Final = "docling-2.151.1+r3"

# FR-INGEST-35: the parse config of each converter version, pinned literals in converter-version order.
_PIPELINE: Final = {"do_ocr": False, "do_table_structure": True, "do_formula_enrichment": True,
                    "do_code_enrichment": False, "device": "cpu"}
# sha256 of host/converter.sb as committed at the r2 profile edit (47aa55a), unchanged since.
_PROFILE_SHA256: Final = "d7a0ee528c62746bf86f6b813d4a8464b98bbb333c1dbb4fffb9d56433ce2bcb"
PARSE_CONFIGS: Final[Mapping[str, Mapping[str, Any]]] = {
    version: {"converter_version": version, "docling": "2.151.1", "pipeline": _PIPELINE, "batch_pages": 50,
              "profile_sha256": _PROFILE_SHA256}
    for version in ("docling-2.151.1+r2", "docling-2.151.1+r3")
}
# FR-INGEST-39: versions imported before revision 3 (their spans were cited by `<sha>:<seq>`).
LEGACY_VERSIONS: Final = ("docling-2.151.1+r2",)
# FR-INGEST-11: the mount path of the `blobs` volume in OPS-DEPLOY's compose.yaml.
BLOBS_DIR: Final = Path("/var/lib/reader/blobs")
MIGRATIONS: Final = Path(__file__).resolve().parent / "migrations"
