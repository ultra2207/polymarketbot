"""Production-Grade MINLP / MILP Capital Allocator for Polymarket Liquidity Rewards.

Formulates and solves the optimal capital allocation across Polymarket reward pools:
    max sum_{i=1}^N P_i * (alpha_i * c_i) / (D_i + alpha_i * c_i)

Subject to:
    1. Budget Constraint:        sum_i c_i <= Bankroll * (1 - reserve_pct)
    2. Strict Floor Clearance:   c_i >= c_i^{min_floor} * x_i  where c_i^{min_floor} = D_i / (alpha_i * (P_i - 1.0))
    3. Minimum Order Size:       c_i >= min_size_i * x_i
    4. Max Active Markets:       sum_i x_i <= K_max
    5. Binary Market Activation: c_i <= M_usable * x_i,  x_i in {0, 1}

Features:
- Google OR-Tools SCIP/CBC solver with piece-wise linear concave secants (SOS2-free).
- Closed-form KKT Water-Filling fallback solver (< 1 ms latency).
- Strict enforcement of Polymarket's $1.00 daily payout floor per market.
"""

from __future__ import annotations

from typing import Any, Optional

from polymarketbot.logs import get_logger
from polymarketbot.strategy.reward.allocator_candidates import (
    parse_candidates,
    prune_dominated,
)
from polymarketbot.strategy.reward.governor import (
    ContinuousOptimizationGovernor,
    GovernorAction,
)
from polymarketbot.strategy.reward.marginal import (
    PROTOCOL_MIN_DAILY_PAYOUT_USD,
    allocation_for_capital,
    calculate_alpha,
    calculate_min_floor_capital,
    evaluate_market_payout,
    evaluate_market_total_yield,
    fill_side,
    marginal_payout,
    marginal_total_yield,
)
from polymarketbot.strategy.reward.milp import solve_milp
from polymarketbot.strategy.reward.models import (
    MarketAllocation,
    PortfolioAllocationResult,
)
from polymarketbot.strategy.reward.reserve import calculate_endogenous_reserve
from polymarketbot.strategy.reward.waterfill import solve_water_filling

log = get_logger("rewards_allocator")

__all__ = [
    "ContinuousOptimizationGovernor",
    "GovernorAction",
    "LiquidityRewardsAllocator",
    "MarketAllocation",
    "PROTOCOL_MIN_DAILY_PAYOUT_USD",
    "PortfolioAllocationResult",
    "calculate_endogenous_reserve",
]


class LiquidityRewardsAllocator:
    """Mathematical optimizer allocating capital across Polymarket reward pools."""

    def __init__(
        self,
        reserve_pct: Optional[float] = None,
        num_linear_segments: int = 10,
    ) -> None:
        # ``reserve_pct`` is only consulted when a caller explicitly disables
        # the endogenous reserve; it has no default because a reserve fraction
        # is a capital decision, not a constant. The attribute is only set when
        # a caller supplied one, so a reader that wants a fallback still sees
        # its own default rather than an invented number from here.
        if reserve_pct is not None:
            self.reserve_pct = float(reserve_pct)
        self.num_segments = int(num_linear_segments)

    def calculate_alpha(self, max_spread_cents: float, spread_offset_cents: float) -> float:
        """Delegate to the module-level marginal maths."""
        return calculate_alpha(max_spread_cents, spread_offset_cents)

    def calculate_min_floor_capital(
        self,
        daily_pool: float,
        competing_score: float,
        alpha: float,
        floor_target: float = PROTOCOL_MIN_DAILY_PAYOUT_USD,
    ) -> float:
        """Delegate to the module-level marginal maths."""
        return calculate_min_floor_capital(
            daily_pool, competing_score, alpha, floor_target=floor_target
        )

    def evaluate_market_payout(
        self,
        capital: float,
        daily_pool: Optional[float],
        competing_score: Optional[float],
        alpha: float,
    ) -> Optional[float]:
        """Delegate to the module-level marginal maths."""
        return evaluate_market_payout(capital, daily_pool, competing_score, alpha)

    def marginal_payout(self, capital: float, daily_pool: float,
                        competing_score: float, alpha: float) -> float:
        """Delegate to the module-level marginal maths."""
        return marginal_payout(capital, daily_pool, competing_score, alpha)

    def evaluate_market_total_yield(
        self,
        capital: float,
        daily_pool: Optional[float],
        competing_score: Optional[float],
        alpha: float,
        stay_fraction: float,
        fill_yield_usd_per_usd_per_day: float,
    ) -> Optional[float]:
        """Delegate to the module-level marginal maths."""
        return evaluate_market_total_yield(
            capital,
            daily_pool,
            competing_score,
            alpha,
            stay_fraction,
            fill_yield_usd_per_usd_per_day,
        )

    def marginal_total_yield(
        self,
        capital: float,
        daily_pool: float,
        competing_score: float,
        alpha: float,
        stay_fraction: float,
        fill_yield_usd_per_usd_per_day: float,
    ) -> float:
        """Delegate to the module-level marginal maths."""
        return marginal_total_yield(
            capital,
            daily_pool,
            competing_score,
            alpha,
            stay_fraction,
            fill_yield_usd_per_usd_per_day,
        )

    @staticmethod
    def _fill_side(cand: dict[str, Any]) -> Optional[tuple[float, float]]:
        """Delegate to the module-level marginal maths."""
        return fill_side(cand)

    def optimize_allocation_milp(
        self,
        candidates: list[dict[str, Any]],
        bankroll_usd: float = 150.0,
        max_active_markets: Optional[int] = None,
        floor_target: float = PROTOCOL_MIN_DAILY_PAYOUT_USD,
        use_endogenous_reserve: bool = True,
        exact_reserve_override: Optional[float] = None,
        _is_pass2: bool = False,
    ) -> PortfolioAllocationResult:
        """Solve capital allocation using Google OR-Tools SCIP MILP solver with concave piecewise linear secants."""
        if exact_reserve_override is not None:
            cash_reserve = round(exact_reserve_override, 2)
            usable_capital = max(0.0, round(bankroll_usd - cash_reserve, 2))
        elif use_endogenous_reserve:
            # Reserve = one measured venue-minimum activation lot: the smallest
            # rewards_min_size (shares) x mid (USD/share) in the supplied universe,
            # i.e. the cash needed to open one more qualifying position. No
            # hand-set reserve fraction or sqrt multiple.
            activation_lots: list[float] = []
            for cand in candidates:
                try:
                    lot_shares = float(cand.get("min_size", 0.0))
                    lot_mid = float(cand.get("real_mid", cand.get("mid_yes", 0.0)))
                except (TypeError, ValueError):
                    continue
                if lot_shares > 0.0 and 0.0 < lot_mid < 1.0:
                    activation_lots.append(lot_shares * lot_mid)
            init_reserve = round(min(bankroll_usd, min(activation_lots, default=0.0)), 2)
            usable_capital = max(0.0, bankroll_usd - init_reserve)
            cash_reserve = init_reserve
        else:
            reserve_pct = getattr(self, "reserve_pct", None)
            if reserve_pct is None:
                raise ValueError(
                    "use_endogenous_reserve=False requires an explicit reserve_pct: "
                    "a reserve fraction is a capital decision, not a default."
                )
            usable_capital = bankroll_usd * (1.0 - reserve_pct)
            cash_reserve = bankroll_usd * reserve_pct

        if usable_capital <= 0.0 or not candidates:
            return PortfolioAllocationResult(
                bankroll_usd=bankroll_usd,
                usable_capital=usable_capital,
                cash_reserve=cash_reserve,
                total_allocated=0.0,
                total_daily_payout=0.0,
                total_monthly_payout=0.0,
                portfolio_daily_roi_pct=0.0,
                portfolio_monthly_roi_pct=0.0,
                active_markets_count=0,
                allocations=[],
                solver_name="OR_TOOLS_SCIP",
            )

        parsed_candidates, unmeasured = parse_candidates(
            candidates, usable_capital, floor_target
        )

        if not parsed_candidates:
            log.info(
                f"allocator: no candidate clears the ${floor_target:.2f}/day payout floor "
                f"inside ${usable_capital:.2f} usable capital ({len(candidates)} candidates, "
                f"{unmeasured} refused with an unmeasured pool or competing score)"
            )
            return PortfolioAllocationResult(
                bankroll_usd=bankroll_usd,
                usable_capital=usable_capital,
                cash_reserve=cash_reserve,
                total_allocated=0.0,
                total_daily_payout=0.0,
                total_monthly_payout=0.0,
                portfolio_daily_roi_pct=0.0,
                portfolio_monthly_roi_pct=0.0,
                active_markets_count=0,
                allocations=[],
                solver_name="NO_VIABLE_MARKETS",
            )
        parsed_candidates, shadow_floor = prune_dominated(
            parsed_candidates, usable_capital
        )

        funded = solve_milp(
            parsed_candidates,
            usable_capital,
            self.num_segments,
            max_active_markets,
        )
        if funded is None:
            return self._solve_water_filling(parsed_candidates, bankroll_usd, usable_capital, cash_reserve)

        # Extract solution
        allocations: list[MarketAllocation] = []
        total_allocated = 0.0
        total_payout = 0.0

        for m, cap in funded:
            priced = allocation_for_capital(m, cap)
            if priced is None:
                continue
            allocation, payout = priced
            allocations.append(allocation)
            total_allocated += cap
            total_payout += payout


        # Sort allocations by expected total yield descending: the accrual alone
        # ranks the emptiest book first, which is the selection this exists to
        # fix.
        allocations.sort(key=lambda a: a.expected_total_yield_usd_per_day, reverse=True)

        if use_endogenous_reserve and allocations and not _is_pass2:
            exact_reserve = calculate_endogenous_reserve(
                allocations=[a.__dict__ for a in allocations],
                bankroll_usd=bankroll_usd,
            )
            # 2-Pass Refinement: Re-solve with the exact portfolio-derived risk reserve
            if abs(exact_reserve - cash_reserve) >= 1.0:
                return self.optimize_allocation_milp(
                    candidates=candidates,
                    bankroll_usd=bankroll_usd,
                    max_active_markets=max_active_markets,
                    floor_target=floor_target,
                    use_endogenous_reserve=True,
                    exact_reserve_override=exact_reserve,
                    _is_pass2=True,
                )
            cash_reserve = round(exact_reserve, 2)
            usable_capital = round(bankroll_usd - cash_reserve, 2)
        elif _is_pass2 and exact_reserve_override is not None:
            cash_reserve = round(exact_reserve_override, 2)
            usable_capital = round(bankroll_usd - cash_reserve, 2)

        daily_roi = (total_payout / total_allocated) * 100.0 if total_allocated > 0 else 0.0
        monthly_roi = daily_roi * 30.0

        return PortfolioAllocationResult(
            bankroll_usd=round(bankroll_usd, 2),
            usable_capital=round(usable_capital, 2),
            cash_reserve=round(cash_reserve, 2),
            total_allocated=round(total_allocated, 2),
            total_daily_payout=round(total_payout, 2),
            total_monthly_payout=round(total_payout * 30.0, 2),
            portfolio_daily_roi_pct=round(daily_roi, 2),
            portfolio_monthly_roi_pct=round(monthly_roi, 2),
            active_markets_count=len(allocations),
            allocations=allocations,
            solver_name="OR_TOOLS_SCIP",
            shadow_yield_floor_usd_per_usd_per_day=shadow_floor,
        )

    def _solve_water_filling(
        self,
        parsed_candidates: list[dict[str, Any]],
        bankroll_usd: float,
        usable_capital: float,
        cash_reserve: float,
    ) -> PortfolioAllocationResult:
        """Delegate to the exact KKT water-filling solver."""
        return solve_water_filling(
            parsed_candidates, bankroll_usd, usable_capital, cash_reserve
        )

    def calculate_endogenous_reserve(
        self,
        allocations: list[dict[str, Any]],
        bankroll_usd: float,
        base_gas_buffer_usd: Optional[float] = None,
        ctf_client: Optional[Any] = None,
    ) -> float:
        """Delegate to module-level calculate_endogenous_reserve."""
        return calculate_endogenous_reserve(
            allocations=allocations,
            bankroll_usd=bankroll_usd,
            base_gas_buffer_usd=base_gas_buffer_usd,
            ctf_client=ctf_client,
        )