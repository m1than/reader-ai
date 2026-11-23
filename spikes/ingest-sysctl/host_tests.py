"""Spike ingest-sysctl: SRV-INGEST's [host] and [local] tests under a spike profile, without changing the product.

    cd server && READER_TEST_INGEST_HOST=1 READER_TEST_INGEST_DATA=<data_dir> READER_TEST_PILOT_PDF=<pilot.pdf> \
      uv run python -I ../spikes/ingest-sysctl/host_tests.py ../spikes/ingest-sysctl/converter-v2.sb

It points `reader.ingest.host.sandbox.PROFILE` at the given file for this process only, then runs the converter
tests: the synthetic PDF, the 20 pilot pages (VEC-INGEST-3), the pilot outline and the confinement probes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from reader.ingest.host import sandbox

TESTS = [
    "tests/ingest/test_ingest_hostlevel.py::test_synthetic_pdf_converts_as_expected",
    "tests/ingest/test_ingest_hostlevel.py::test_real_child_output_never_reaches_the_terminal",
    "tests/ingest/test_ingest_hostlevel.py::test_pilot_page_expectations",
    "tests/ingest/test_ingest_hostlevel.py::test_pilot_outline",
    "tests/ingest/test_ingest_hostlevel.py::test_confinement_probes",
]


def main() -> int:
    sandbox.PROFILE = Path(sys.argv[1]).resolve()  # type: ignore[misc]
    return pytest.main(["-q", "-p", "no:cacheprovider", *TESTS])


if __name__ == "__main__":
    raise SystemExit(main())
