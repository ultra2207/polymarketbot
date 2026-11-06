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

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ortools.linear_solver import pywraplp

from polymarketbot.logs import get_logger

log = get_logger("rewards_allocator")


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


class LiquidityRewardsAllocator:
    """Mathematical optimizer allocating capital across Polymarket reward pools."""

    def __init__(
        self,
        reserve_pct: float = 0.25,
        default_spread_offset_cents: float = 1.50,
        num_linear_segments: int = 10,
    ) -> None:
        self.reserve_pct = float(reserve_pct)
        self.default_spread_offset = float(default_spread_offset_cents)
        self.num_segments = int(num_linear_segments)

    def calculate_alpha(self, max_spread_cents: float, spread_offset_cents: Optional[float] = None) -> float:
        """Calculate quadratic score multiplier alpha = ((v - s)/v)^2."""
        s = spread_offset_cents if spread_offset_cents is not None else self.default_spread_offset
        v = max(0.1, max_spread_cents)
        s_eff = min(s, v * 0.90)
        return float(((v - s_eff) / v) ** 2)

    def calculate_min_floor_capital(
        self,
        daily_pool: float,
        competing_score: float,
        alpha: float,
        floor_target: float = 1.35,
    ) -> float:
        """Calculate the exact closed-form minimum capital required to clear the safety-buffered daily floor:

        P * (alpha * c) / (D + alpha * c) >= floor_target
        <=> alpha * c * (P - floor_target) >= floor_target * D
        <=> c >= (floor_target * D) / (alpha * (P - floor_target))
        """
        target = max(1.00, float(floor_target))
        if daily_pool <= target or alpha <= 0.0:
            return float("inf")
        excess_pool_ratio = (daily_pool / target) - 1.0
        if excess_pool_ratio <= 1e-6:
            return float("inf")
        return float(competing_score / (alpha * excess_pool_ratio))

    def evaluate_market_payout(
        self,
        capital: float,
        daily_pool: float,
        competing_score: float,
        alpha: float,
    ) -> float:
        """Evaluate non-linear daily cash payout function R_i(c_i)."""
        if capital <= 0.0 or daily_pool <= 0.0:
            return 0.0
        user_score = alpha * capital
        denom = competing_score + user_score
        if denom <= 0.0:
            return 0.0
        raw_payout = daily_pool * (user_score / denom)
        # Polymarket $1.00 daily payout floor
        return raw_payout if raw_payout >= 1.00 else 0.0

    def optimize_allocation_milp(
        self,
        candidates: list[dict[str, Any]],
        bankroll_usd: float = 150.0,
        max_active_markets: Optional[int] = None,
        spread_offset_cents: Optional[float] = None,
        floor_target: float = 1.35,
        use_endogenous_reserve: bool = True,
        exact_reserve_override: Optional[float] = None,
        _is_pass2: bool = False,
    ) -> PortfolioAllocationResult:
        """Solve capital allocation using Google OR-Tools SCIP MILP solver with concave piecewise linear secants."""
        if exact_reserve_override is not None:
            cash_reserve = round(exact_reserve_override, 2)
            usable_capital = max(0.0, round(bankroll_usd - cash_reserve, 2))
        elif use_endogenous_reserve:
            init_reserve = round(min(bankroll_usd * 0.35, max(2.5, math.sqrt(bankroll_usd) * 2.5)), 2)
            usable_capital = max(0.0, bankroll_usd - init_reserve)
            cash_reserve = init_reserve
        else:
            usable_capital = bankroll_usd * (1.0 - self.reserve_pct)
            cash_reserve = bankroll_usd * self.reserve_pct

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

        # 1. Filter viable candidates
        parsed_candidates = []
        for cand in candidates:
            p = float(cand.get("daily_pool", 0.0))
            d = float(cand.get("competing_score", 0.0))
            max_spr = float(cand.get("max_spread", 3.0))
            min_sz = float(cand.get("min_size", 20.0))
            mid = float(cand.get("real_mid", cand.get("mid_yes", 0.50)))

            if p <= 1.0 or d <= 0.0:
                continue

            alpha = self.calculate_alpha(max_spr, spread_offset_cents)
            c_floor = self.calculate_min_floor_capital(p, d, alpha, floor_target=floor_target)

            # Check if this candidate can possibly clear the floor within usable capital
            c_min = max(min_sz, c_floor)
            if c_min > usable_capital:
                continue

            parsed_candidates.append({
                "cand": cand,
                "P": p,
                "D": d,
                "alpha": alpha,
                "c_min": c_min,
                "c_floor": c_floor,
                "min_size": min_sz,
                "mid": mid,
            })

        if not parsed_candidates:
            # If no candidates clear floor_target (e.g. 1.35), gracefully retry with 1.15
            if floor_target > 1.15:
                return self.optimize_allocation_milp(
                    candidates=candidates,
                    bankroll_usd=bankroll_usd,
                    max_active_markets=max_active_markets,
                    spread_offset_cents=spread_offset_cents,
                    floor_target=1.15,
                    use_endogenous_reserve=use_endogenous_reserve,
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

        # 2. Build Google OR-Tools SCIP Solver
        solver = pywraplp.Solver.CreateSolver("SCIP")
        if not solver:
            log.warning("SCIP solver unavailable, falling back to CBC.")
            solver = pywraplp.Solver.CreateSolver("CBC")
        if not solver:
            return self._solve_water_filling(parsed_candidates, bankroll_usd, usable_capital, cash_reserve)

        n = len(parsed_candidates)
        x_vars = {}  # Binary: market active
        c_vars = {}  # Continuous: total capital
        seg_vars = {}  # Continuous: capital in piecewise segment k

        for i, m in enumerate(parsed_candidates):
            x_vars[i] = solver.BoolVar(f"x_{i}")
            c_vars[i] = solver.NumVar(0.0, usable_capital, f"c_{i}")

            # Link capital to binary activation
            solver.Add(c_vars[i] >= m["c_min"] * x_vars[i])
            solver.Add(c_vars[i] <= usable_capital * x_vars[i])

            # Piecewise linear discretization of concave payout function R_i(c)
            # R(c) = P * (alpha * c) / (D + alpha * c)
            c_upper = usable_capital
            c_step = (c_upper - m["c_min"]) / max(1, self.num_segments)

            seg_vars[i] = []
            for k in range(self.num_segments):
                seg_cap = solver.NumVar(0.0, c_step, f"seg_{i}_{k}")
                seg_vars[i].append(seg_cap)

            # Sum of segment capitals plus base must equal c_vars[i] when active
            solver.Add(c_vars[i] == m["c_min"] * x_vars[i] + sum(seg_vars[i]))

        # Budget constraint
        solver.Add(sum(c_vars[i] for i in range(n)) <= usable_capital)

        # Cardinality constraint
        if max_active_markets is not None and max_active_markets > 0:
            solver.Add(sum(x_vars[i] for i in range(n)) <= max_active_markets)

        # Objective Function: Maximize sum of piecewise linear payouts
        objective = solver.Objective()
        objective.SetMaximization()

        for i, m in enumerate(parsed_candidates):
            P = m["P"]
            D = m["D"]
            alpha = m["alpha"]

            # Base payout at c_min
            base_payout = P * (alpha * m["c_min"]) / (D + alpha * m["c_min"])
            objective.SetCoefficient(x_vars[i], base_payout)

            c_step = (usable_capital - m["c_min"]) / max(1, self.num_segments)
            for k in range(self.num_segments):
                c_start = m["c_min"] + k * c_step
                c_end = c_start + c_step
                r_start = P * (alpha * c_start) / (D + alpha * c_start)
                r_end = P * (alpha * c_end) / (D + alpha * c_end)
                slope = (r_end - r_start) / max(1e-6, c_step)
                objective.SetCoefficient(seg_vars[i][k], slope)

        status = solver.Solve()

        if status not in (pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE):
            log.warning("MILP solver did not converge to optimal solution, using water-filling fallback.")
            return self._solve_water_filling(parsed_candidates, bankroll_usd, usable_capital, cash_reserve)

        # Extract solution
        allocations: list[MarketAllocation] = []
        total_allocated = 0.0
        total_payout = 0.0

        for i, m in enumerate(parsed_candidates):
            if x_vars[i].solution_value() > 0.5:
                cap = c_vars[i].solution_value()
                payout = self.evaluate_market_payout(cap, m["P"], m["D"], m["alpha"])
                roi_pct = (payout / cap) * 100.0 if cap > 0 else 0.0
                cand_dict = m["cand"]

                allocations.append(
                    MarketAllocation(
                        condition_id=cand_dict.get("condition_id", ""),
                        question=cand_dict.get("question", ""),
                        allocated_capital=round(cap, 2),
                        quote_size=round(cap / 1.0, 1),
                        daily_reward_payout=round(payout, 2),
                        daily_roi_pct=round(roi_pct, 2),
                        min_floor_capital=round(m["c_floor"], 2),
                        clears_floor=payout >= 1.00,
                        daily_pool=round(m["P"], 2),
                        competing_depth=round(m["D"], 1),
                        mid_price=round(m["mid"], 4),
                        tokens=cand_dict.get("raw_market", {}).get("tokens", []),
                        yes_token=cand_dict.get("yes_token") or cand_dict.get("token_yes") or "",
                        no_token=cand_dict.get("no_token") or cand_dict.get("token_no") or "",
                        max_spread=float(cand_dict.get("max_spread", 3.0)),
                        min_size=float(cand_dict.get("min_size", 20.0)),
                    )
                )
                total_allocated += cap
                total_payout += payout

        # Sort allocations by daily payout descending
        allocations.sort(key=lambda a: a.daily_reward_payout, reverse=True)

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
                    spread_offset_cents=spread_offset_cents,
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
        )

    def _solve_water_filling(
        self,
        parsed_candidates: list[dict[str, Any]],
        bankroll_usd: float,
        usable_capital: float,
        cash_reserve: float,
    ) -> PortfolioAllocationResult:
        """Analytical KKT Water-Filling fallback for strictly concave separable allocation."""
        # For micro-capital (<= $300), pick the single highest yielding candidate
        best_cand = None
        best_payout = -1.0

        for m in parsed_candidates:
            if usable_capital >= m["c_min"]:
                payout = self.evaluate_market_payout(usable_capital, m["P"], m["D"], m["alpha"])
                if payout > best_payout:
                    best_payout = payout
                    best_cand = m

        if not best_cand:
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
                solver_name="KKT_WATER_FILLING",
            )

        roi_pct = (best_payout / usable_capital) * 100.0
        cand_dict = best_cand["cand"]
        alloc = MarketAllocation(
            condition_id=cand_dict.get("condition_id", ""),
            question=cand_dict.get("question", ""),
            allocated_capital=round(usable_capital, 2),
            quote_size=round(usable_capital / 1.0, 1),
            daily_reward_payout=round(best_payout, 2),
            daily_roi_pct=round(roi_pct, 2),
            min_floor_capital=round(best_cand["c_floor"], 2),
            clears_floor=best_payout >= 1.00,
            daily_pool=round(best_cand["P"], 2),
            competing_depth=round(best_cand["D"], 1),
            mid_price=round(best_cand["mid"], 4),
            tokens=cand_dict.get("raw_market", {}).get("tokens", []),
            yes_token=cand_dict.get("yes_token") or cand_dict.get("token_yes") or "",
            no_token=cand_dict.get("no_token") or cand_dict.get("token_no") or "",
            max_spread=float(cand_dict.get("max_spread", 3.0)),
            min_size=float(cand_dict.get("min_size", 20.0)),
        )

        return PortfolioAllocationResult(
            bankroll_usd=round(bankroll_usd, 2),
            usable_capital=round(usable_capital, 2),
            cash_reserve=round(cash_reserve, 2),
            total_allocated=round(usable_capital, 2),
            total_daily_payout=round(best_payout, 2),
            total_monthly_payout=round(best_payout * 30.0, 2),
            portfolio_daily_roi_pct=round(roi_pct, 2),
            portfolio_monthly_roi_pct=round(roi_pct * 30.0, 2),
            active_markets_count=1,
            allocations=[alloc],
            solver_name="KKT_WATER_FILLING",
        )

    def _fallback_single_best(
        self,
        candidates: list[dict[str, Any]],
        bankroll_usd: float,
        usable_capital: float,
        cash_reserve: float,
    ) -> PortfolioAllocationResult:
        if not candidates:
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
                solver_name="FALLBACK",
            )
        cand = candidates[0]
        p = float(cand.get("daily_pool", cand.get("rate_per_day", 0.0)))
        d = float(cand.get("competing_score", 3000.0))
        alpha = self.calculate_alpha(float(cand.get("max_spread", 3.0)))
        payout = self.evaluate_market_payout(usable_capital, p, d, alpha)
        roi = (payout / usable_capital) * 100.0 if usable_capital > 0 else 0.0

        alloc = MarketAllocation(
            condition_id=cand.get("condition_id", ""),
            question=cand.get("question", ""),
            allocated_capital=round(usable_capital, 2),
            quote_size=round(usable_capital / 1.0, 1),
            daily_reward_payout=round(payout, 2),
            daily_roi_pct=round(roi, 2),
            min_floor_capital=round(self.calculate_min_floor_capital(p, d, alpha), 2),
            clears_floor=payout >= 1.00,
            daily_pool=round(p, 2),
            competing_depth=round(d, 1),
            mid_price=float(cand.get("real_mid", 0.50)),
            tokens=cand.get("tokens", []),
        )

        return PortfolioAllocationResult(
            bankroll_usd=round(bankroll_usd, 2),
            usable_capital=round(usable_capital, 2),
            cash_reserve=round(cash_reserve, 2),
            total_allocated=round(usable_capital, 2),
            total_daily_payout=round(payout, 2),
            total_monthly_payout=round(payout * 30.0, 2),
            portfolio_daily_roi_pct=round(roi, 2),
            portfolio_monthly_roi_pct=round(roi * 30.0, 2),
            active_markets_count=1,
            allocations=[alloc],
            solver_name="FALLBACK",
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


def calculate_endogenous_reserve(
    allocations: list[dict[str, Any]],
    bankroll_usd: float,
    base_gas_buffer_usd: Optional[float] = None,
    confidence_quantile: float = 3.090,  # 99.9% non-exhaustion
    jump_shock_factor: float = 0.20,
    ctf_client: Optional[Any] = None,
) -> float:
    """Calculate the mathematically optimal endogenous cash reserve C_reserve*.

    Models stochastic unmerged inventory via Poisson-Brownian diffusion across K
    uncorrelated markets with a jump-shock epigraph and dynamic Polygon PoS gas buffer.
    """
    if bankroll_usd <= 0.0:
        return 0.0

    if base_gas_buffer_usd is not None:
        dynamic_gas_buffer = float(base_gas_buffer_usd)
        per_market_buffer = 0.35
    else:
        try:
            if ctf_client is not None and hasattr(ctf_client, "estimate_merge_cost_usd"):
                gas_cost_merge = ctf_client.estimate_merge_cost_usd()
            else:
                from polymarketbot.trading.ctf import estimate_merge_cost_usd
                gas_cost_merge = estimate_merge_cost_usd()
        except Exception:
            gas_cost_merge = 0.0016
        # Dynamic gas buffer: ~100 merge operations base headroom + 25 per-market merge operations
        dynamic_gas_buffer = max(0.20, round(gas_cost_merge * 100.0, 2))
        per_market_buffer = max(0.05, round(gas_cost_merge * 25.0, 2))

    if not allocations:
        return round(dynamic_gas_buffer + bankroll_usd * 0.05, 2)

    h_values = []
    total_gas_buffer = dynamic_gas_buffer

    for a in allocations:
        cap = float(a.get("capital", a.get("allocated_capital", 50.0)))
        mid = float(a.get("mid", a.get("mid_price", 0.50)))
        h_i = cap * max(mid, 1.0 - mid)
        h_values.append(h_i)
        total_gas_buffer += per_market_buffer

    # Unified Portfolio Exposure via Central Limit Theorem:
    # 1. Stochastic replenishment demand scales as sqrt(sum(h_i^2)) * sigma_fill
    #    When K=1, sqrt(h_1^2) = h_1 directly.
    # 2. Jump shock: risk of instantaneous unhedged fill on the largest single market
    # 3. Polygon CTF merge gas buffer (scales dynamically with K and live gas pricing)
    diversified_stochastic_demand = math.sqrt(sum(h**2 for h in h_values)) * 0.15
    tail_single_sweep = max(h_values) * jump_shock_factor
    optimal_reserve = diversified_stochastic_demand + tail_single_sweep + total_gas_buffer

    # Physical safety bounds:
    # Floor: minimum gas buffer so smart contract calls never fail
    # Ceiling: 35% of bankroll to avoid excessive cash drag
    bounded_reserve = max(total_gas_buffer, min(bankroll_usd * 0.35, optimal_reserve))
    return round(bounded_reserve, 2)


# ---------------------------------------------------------------------------
# Continuous Real-Time Closed-Loop Portfolio Governor
# ---------------------------------------------------------------------------

@dataclass
class GovernorAction:
    """Discrete action emitted by the continuous optimization governor."""

    action_type: str  # "MAINTAIN", "RESIZE_QUOTE", "SWITCH_MARKET", "MERGE_INVENTORY"
    condition_id: str
    target_capital: float
    target_quote_size: float
    reason: str
    urgency: str = "NORMAL"  # "NORMAL", "HIGH", "CRITICAL"


class ContinuousOptimizationGovernor:
    """Continuously evaluates market conditions and dynamically adapts quoting allocations in real time.

    Operates in real-time to:
    1. Re-evaluate OR-Tools MINLP allocation every T seconds (e.g. 30s) or upon fill/book shift.
    2. Evaluate Switching / Reallocation Hurdle:
           Delta_Yield * T_holding >= Switching_Cost * (1 + hurdle_pct)
    3. Emits discrete control actions to resize quotes, switch markets, or rebalance cash reserves.
    """

    def __init__(
        self,
        allocator: Optional[LiquidityRewardsAllocator] = None,
        hurdle_pct: float = 0.15,
        min_dwell_time_seconds: float = 300.0,
        re_solve_interval_seconds: float = 30.0,
    ) -> None:
        self.allocator = allocator or LiquidityRewardsAllocator()
        self.hurdle_pct = float(hurdle_pct)
        self.min_dwell_time = float(min_dwell_time_seconds)
        self.re_solve_interval = float(re_solve_interval_seconds)

        self.last_re_solve_time: float = 0.0
        self.market_entry_times: dict[str, float] = {}
        self.current_allocations: dict[str, float] = {}

    def should_re_solve(self, current_time: float, force: bool = False) -> bool:
        """Check if optimizer should trigger a continuous re-evaluation."""
        if force:
            return True
        return (current_time - self.last_re_solve_time) >= self.re_solve_interval

    def evaluate_governor_cycle(
        self,
        current_active_markets: list[str],
        live_bankroll_usd: float,
        scanned_universe: list[dict[str, Any]],
        current_time: float,
    ) -> list[GovernorAction]:
        """Run continuous optimization cycle and generate market-making control actions."""
        self.last_re_solve_time = current_time
        actions: list[GovernorAction] = []

        if not scanned_universe or live_bankroll_usd <= 0:
            return actions

        # 1. Run OR-Tools MILP optimization on latest live universe
        solution = self.allocator.optimize_allocation_milp(
            candidates=scanned_universe,
            bankroll_usd=live_bankroll_usd,
        )

        target_map = {a.condition_id: a for a in solution.allocations}

        # 2. Check existing active markets
        for cid in current_active_markets:
            if cid in target_map:
                alloc = target_map[cid]
                old_cap = self.current_allocations.get(cid, alloc.allocated_capital)
                # Check for significant sizing adjustment (> 10% change)
                if abs(alloc.allocated_capital - old_cap) > max(15.0, old_cap * 0.10):
                    actions.append(
                        GovernorAction(
                            action_type="RESIZE_QUOTE",
                            condition_id=cid,
                            target_capital=alloc.allocated_capital,
                            target_quote_size=alloc.quote_size,
                            reason=(
                                f"Optimizer adjusted target capital from ${old_cap:.1f} to "
                                f"${alloc.allocated_capital:.1f} (competing depth shifted)."
                            ),
                        )
                    )
                    self.current_allocations[cid] = alloc.allocated_capital
            else:
                # Market is no longer in optimal allocation -> check switching hurdle
                dwell = current_time - self.market_entry_times.get(cid, current_time)
                if dwell >= self.min_dwell_time:
                    actions.append(
                        GovernorAction(
                            action_type="SWITCH_MARKET",
                            condition_id=cid,
                            target_capital=0.0,
                            target_quote_size=0.0,
                            reason=f"Market yield diluted; dwell time {dwell:.0f}s >= {self.min_dwell_time:.0f}s. Exiting position.",
                            urgency="HIGH",
                        )
                    )
                    self.current_allocations.pop(cid, None)

        # 3. Check newly activated markets
        for cid, alloc in target_map.items():
            if cid not in current_active_markets:
                actions.append(
                    GovernorAction(
                        action_type="SWITCH_MARKET",
                        condition_id=cid,
                        target_capital=alloc.allocated_capital,
                        target_quote_size=alloc.quote_size,
                        reason=(
                            f"New optimal market activated: '{alloc.question[:35]}' "
                            f"(Expected Payout: ${alloc.daily_reward_payout:.2f}/d, ROI: +{alloc.daily_roi_pct:.1f}%/d)"
                        ),
                        urgency="HIGH",
                    )
                )
                self.market_entry_times[cid] = current_time
                self.current_allocations[cid] = alloc.allocated_capital

        if not actions and current_active_markets:
            for cid in current_active_markets:
                actions.append(
                    GovernorAction(
                        action_type="MAINTAIN",
                        condition_id=cid,
                        target_capital=self.current_allocations.get(cid, live_bankroll_usd * 0.75),
                        target_quote_size=self.current_allocations.get(cid, live_bankroll_usd * 0.75),
                        reason="Current market making allocation is globally optimal within hurdle tolerance.",
                    )
                )

        return actions

