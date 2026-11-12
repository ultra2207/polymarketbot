"""Result statements the reward allocator returns."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MarketAllocation:
    """Individual market capital allocation and expected performance."""

    condition_id: str
    question: str
    allocated_capital: float
    quote_size: float
    daily_reward_payout: float
    daily_roi_pct: float
    min_floor_capital: float
    clears_floor: bool
    daily_pool: float
    competing_depth: float
    mid_price: float
    tokens: list[dict[str, Any]] = field(default_factory=list)
    yes_token: str = ""
    no_token: str = ""
    max_spread: float = 3.0
    min_size: float = 20.0
    expected_fill_yield_usd_per_day: float = 0.0
    """Fill side of the yield, USD/day: ``p_fill``-weighted and net of the
    measured adverse markout. Negative when a fill costs more than it earns."""
    expected_total_yield_usd_per_day: float = 0.0
    """``daily_reward_payout * stay_fraction + expected_fill_yield``: the number
    the optimiser actually maximised. ``daily_reward_payout`` stays the accrual
    alone because that is what the engine credits as cash."""
    p_fill: float | None = None
    """Measured per-horizon fill probability behind the fill term; ``None``
    when the candidate's fill side was not priced."""

    competing_samples: int = 0
    """Venue-cadence readings behind ``competing_depth``. The venue divides by
    the mean of up to 1,440 samples per epoch, so a ranking built on one
    snapshot is labelled rather than presented as the epoch rate."""
    competing_low_confidence: bool = False
    """True when ``competing_depth`` came from a single snapshot."""

    @property
    def token_yes(self) -> str:
        if self.yes_token:
            return self.yes_token
        for t in self.tokens:
            if str(t.get("outcome", "")).upper() == "YES":
                return t.get("token_id", "")
        return f"{self.condition_id}_YES"

    @property
    def token_no(self) -> str:
        if self.no_token:
            return self.no_token
        for t in self.tokens:
            if str(t.get("outcome", "")).upper() == "NO":
                return t.get("token_id", "")
        return f"{self.condition_id}_NO"


@dataclass
class PortfolioAllocationResult:
    """Aggregated portfolio allocation statement."""

    bankroll_usd: float
    usable_capital: float
    cash_reserve: float
    total_allocated: float
    total_daily_payout: float
    total_monthly_payout: float
    portfolio_daily_roi_pct: float
    portfolio_monthly_roi_pct: float
    active_markets_count: int
    allocations: list[MarketAllocation]
    solver_name: str
    #: KKT price of capital at the optimum, USD per USD per day: the marginal
    #: reward yield the last funded market earns. Any other use of the same
    #: dollar (a CTF parity lock, a settlement buy) must beat it or the capital
    #: is better left on the reward book. ``None`` when no allocation was
    #: solved, so a caller cannot mistake "unmeasured" for "zero hurdle".
    shadow_yield_floor_usd_per_usd_per_day: float | None = None
