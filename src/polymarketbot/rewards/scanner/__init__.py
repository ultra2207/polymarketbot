"""Dynamic Opportunity Scanner & Yield Optimization for Polymarket Liquidity Rewards.

Implements:
1. Dynamic REST querying across all available head reward pools.
2. Net Expected Marginal Yield for the capital **the caller allocates**:
       Y_net = Reward Payout(capital, v, s, D_comp)
               + E[Spread Capture] - E[Adverse Selection] - Gas Fees
3. Strict enforcement of the $1.00 daily payout floor (Y_gross < $1.00 => $0.00).
4. Real-time ranking of markets by net yield per dollar.

Every microstructure term in (2) is measured:

* P(fill) comes from ``data/derived/ctf_quote_fill_horizon.json`` (8,773,815 tape
  placements, cells keyed by quote lifetime, tick and required flow) through
  ``polymarketbot.rewards.fill_measurement.load_quote_fill_horizon`` - the same table
  the CTF maker gate prices with.
* Per-share adverse drift comes from ``data/derived/ctf_maker_markout.json``
  (44,068 measured fills) through ``load_maker_markout``.
* Trade rate / mean print size / realised volatility / jump frequency come from
  the venue's own prints in ``data/tape/clob_trades`` over a stated trailing
  window (``polymarketbot.rewards.tape_regime``).
* Capital comes from the caller. A market's (tick, flow, horizon) cell that the
  measurement does not cover is **missing data**: the market is refused, never
  priced on a fill-share guess.

The implementation is split into submodules:

* :mod:`~polymarketbot.rewards.scanner.state` - telemetry, TTL caches, session.
* :mod:`~polymarketbot.rewards.scanner.payload` - raw payload readers and the
  live-trading gate.
* :mod:`~polymarketbot.rewards.scanner.candidates` - ``MarketOpportunity`` and
  the candidate record ``_screen_lp_candidate`` builds.
* :mod:`~polymarketbot.rewards.scanner.pricing` - ``calculate_expected_yield``
  over the measured fill/markout tables.
* :mod:`~polymarketbot.rewards.scanner.venue` - the published tick and the live
  L2 book score.
* :mod:`~polymarketbot.rewards.scanner.screen` - ``OpportunityScanner``.
* :mod:`~polymarketbot.rewards.scanner.lp_screen` - ``find_optimal_lp_markets``.

Everything the single module exported is re-exported here, so
``from polymarketbot.rewards.scanner import X`` keeps working. Patch targets
belong to the submodule that reads the name (for example
``scanner.screen.measure_regimes_cached`` and
``scanner.lp_screen.get_real_l2_score``), because a re-export is not a live
binding.
"""


from __future__ import annotations

from typing import Any

from polymarketbot.rewards.tape_regime import MarketRegime, measure_regimes_cached

from . import candidates as candidates
from . import lp_screen as lp_screen
from . import payload as payload
from . import pricing as pricing
from . import screen as screen
from . import state as state
from . import venue as venue
from .candidates import MarketOpportunity, infer_market_category
from .lp_screen import find_optimal_lp_markets
from .payload import validate_market_for_live
from .pricing import calculate_expected_yield, reset_measured_tables
from .screen import OpportunityScanner
from .state import (
    _BOOK_CACHE as _BOOK_CACHE,
)
from .state import (
    _SCANNER_CACHE as _SCANNER_CACHE,
)
from .state import (
    CLOB_REST_URL,
    SCANNER_CACHE_TTL_SECONDS,
    ScannerTelemetry,
    get_scanner_telemetry,
    reset_scanner_telemetry,
)
from .state import (
    logger as logger,
)
from .venue import fetch_tick_size, get_real_l2_score

__all__ = [
    "CLOB_REST_URL",
    "MarketOpportunity",
    "MarketRegime",
    "OpportunityScanner",
    "SCANNER_CACHE_TTL_SECONDS",
    "ScannerTelemetry",
    "calculate_expected_yield",
    "fetch_tick_size",
    "find_optimal_lp_markets",
    "get_real_l2_score",
    "get_scanner_telemetry",
    "infer_market_category",
    "measure_regimes_cached",
    "reset_measured_tables",
    "reset_scanner_telemetry",
    "validate_market_for_live",
]


def __getattr__(name: str) -> Any:
    """Read the telemetry singleton live: ``reset_scanner_telemetry`` rebinds it."""
    if name == "GLOBAL_SCANNER_TELEMETRY":
        return state.GLOBAL_SCANNER_TELEMETRY
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
