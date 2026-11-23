"""SRV-PLAN constants with their sources (FR-PLAN-8, VEC-PLAN-2, `bounds`)."""

from __future__ import annotations

from decimal import Decimal
from typing import Final

# Compile cost basis, one measured lesson (REF-PLAN-4).
L02_LOW_USD: Final = Decimal("1.2587")  # converged product run, docs/progress.yaml V.live 2026-11-23
L02_HIGH_USD: Final = Decimal("2.9714")  # dearest spike run, one loop, not converged (spikes/compile-cost)
L02_KCHARS: Final = Decimal("58.938")  # L02's span characters (spikes/study-rate step 1b)
RUN_CAP_USD: Final = Decimal("8.00")  # SRV-PIPELINE bounds.run_cap_usd
ESTIMATE_BAND: Final = (Decimal("0.5"), Decimal("2"))  # chosen: outside 0.5x to 2x of L02 is an extrapolation

# Study rate (spikes/study-rate step 1b: the owner's 81 theory hours over 1,841,091 span characters).
RATE_DEFAULT: Final = Decimal("2.64")
RATE_MIN: Final = Decimal("0.10")
RATE_MAX: Final = Decimal("20.00")
TODAY_SKEW_DAYS: Final = 1
