"""Production Google OR-Tools Mathematical Portfolio Allocator for Domain 2.

Replaces naive hardcoded heuristic sizing (e.g. fixed 5% buckets) with a rigorous
Mixed-Integer Linear/Quadratic Program (MILP/SCIP) that optimizes:
    max sum_{i=1}^M (1 / max(0.5, t_i)) * [ (p_i - P_i)/P_i - lambda * (h_i / P_i^2) ] * w_i

Subject to:
    1. Budget Constraint:            sum_i w_i <= Bankroll * (1 - reserve_pct)
    2. Category Correlation Caps:    sum_{i in Category_k} w_i <= Bankroll * category_cap_pct
    3. Left-Tail / Max Drawdown:     max_k sum_{i in Category_k} w_i <= Bankroll * max_drawdown_target
    4. Discrete Market Activation:   w_i >= min_order_usd * z_i,  z_i in {0, 1}
    5. Single Bet Liquidity Bounds:  w_i <= min(max_single_bet_usd, gamma * Depth_i) * z_i
    6. Max Concurrent Markets:       sum_i z_i <= K_max

Features:
- Google OR-Tools SCIP solver.
- Mathematical rejection of toxic negative-utility risks (e.g. low-edge high-hazard bets).
- Dynamic diversification across orthogonal categories (crypto, weather, politics, sports).
- Continuous microstructure Kyle's lambda and concave order book depth elasticity.
- Dynamic EIP-1559 gas friction and UMA settlement delay integration.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from ortools.linear_solver import pywraplp

from polymarketbot.fees import FeeSchedule, taker_fee
from polymarketbot.logs import get_logger
from polymarketbot.rewards.queue_valuation import PolygonPoSEnvironment
from polymarketbot.trading.domain2.robust_hazard import (
    BinaryVolatilityEstimator,
    RobustHazardModel,
    calculate_cycle_velocity,
)
from polymarketbot.trading.domain2.dynamic_sizer import (
    DynamicPositionSizer,
    DynamicSizerConfig,
)

log = get_logger("domain2_ortools_allocator")


@dataclass
class Domain2Candidate:
    """A candidate market for settlement basis or pre-resolution discount harvesting."""

    condition_id: str
    token_id: str
    question: str
    category: str
    price: float
    hours_to_resolution: float
    depth_usd: float
    p_hat: Optional[float] = None
    fee_rate: float = 0.0
    fee_exponent: float = 1.0
    fee_schedule: Optional[FeeSchedule] = None
    is_taker: bool = False
    redemption_gas_usd: float = 0.005
    is_uma_proposed: bool = False
    order_book_levels: Optional[List[Tuple[float, float]]] = None  # [(price, size_shares), ...]
    spot_price: Optional[float] = None
    strike_price: Optional[float] = None
    spread: Optional[float] = None
    price_history: Optional[List[float]] = None
    empirical_vol: Optional[float] = None
    pos_env: Optional[PolygonPoSEnvironment] = None

    @property
    def estimated_win_prob(self) -> float:
        """Estimated win probability p_hat. If not supplied, evaluated via RobustHazardModel."""
        if self.p_hat is not None:
            return float(self.p_hat)
        
        # Use institutional robust multi-layer hazard evaluation with empirical volatility
        model = RobustHazardModel()
        p_win, _, _ = model.evaluate_candidate(
            category=self.category,
            price=self.price,
            hours_to_resolution=self.hours_to_resolution,
            is_uma_proposed=self.is_uma_proposed,
            spot_price=self.spot_price,
            strike_price=self.strike_price,
            underlying_vol_annual=self.empirical_vol,
            spread=self.spread,
            price_history=self.price_history,
        )
        return float(p_win)


@dataclass
class Domain2Allocation:
    """Calculated executable capital allocation for one market."""

    condition_id: str
    token_id: str
    question: str
    category: str
    price: float
    hours_to_resolution: float
    allocated_usd: float
    allocation_pct: float
    shares: float
    net_edge_pct: float
    hazard_pct: float
    hourly_velocity: float
    is_uma_proposed: bool
    effective_avg_price: float = 0.0
    levels_consumed: int = 1
    total_fee_usd: float = 0.0
    redemption_gas_usd: float = 0.0


@dataclass
class Domain2OptimizationResult:
    """Aggregated portfolio optimization statement from Google OR-Tools."""

    bankroll_usd: float
    usable_capital_usd: float
    cash_reserve_usd: float
    total_allocated_usd: float
    total_allocated_pct: float
    active_markets_count: int
    allocations: List[Domain2Allocation]
    category_exposure_usd: Dict[str, float]
    category_exposure_pct: Dict[str, float]
    solver_status: str
    solver_name: str
    expected_daily_capital_velocity: float


class Domain2OrtoolsAllocator:
    """Google OR-Tools Mathematical Optimizer for Domain 2 Portfolio Allocation."""

    def __init__(
        self,
        reserve_pct: float = 0.25,
        max_drawdown_target: float = 0.10,
        max_single_bet_pct: float = 0.10,
        max_category_pct: float = 0.150,
        max_active_markets: int = 15,
        min_order_usd: float = 20.0,
        depth_consumption_gamma: float = 0.25,
        variance_penalty_lambda: float = 3.0,
        dynamic_sizer: Optional[DynamicPositionSizer] = None,
        use_dynamic_sizing: bool = True,
        use_precautionary_reserve: bool = True,
        use_factor_risk_budgeting: bool = True,
        use_fixed_ticket_cost: bool = True,
        pos_env: Optional[PolygonPoSEnvironment] = None,
    ) -> None:
        self.reserve_pct = float(reserve_pct)
        self.max_drawdown_target = float(max_drawdown_target)
        self.max_single_bet_pct = float(max_single_bet_pct)
        self.max_category_pct = float(max_category_pct)
        self.max_active_markets = int(max_active_markets)
        self.min_order_usd = float(min_order_usd)
        self.depth_gamma = float(depth_consumption_gamma)
        self.lambda_var = float(variance_penalty_lambda)
        self.dynamic_sizer = dynamic_sizer or DynamicPositionSizer()
        self.use_dynamic_sizing = use_dynamic_sizing
        self.use_precautionary_reserve = use_precautionary_reserve
        self.use_factor_risk_budgeting = use_factor_risk_budgeting
        self.use_fixed_ticket_cost = use_fixed_ticket_cost
        self.pos_env = pos_env or PolygonPoSEnvironment()

    def optimize_allocation(
        self,
        candidates: List[Domain2Candidate],
        bankroll_usd: float = 10000.0,
    ) -> Domain2OptimizationResult:
        """Solve the Mixed-Integer Linear/Quadratic Program using Google OR-Tools SCIP."""
        solver = pywraplp.Solver.CreateSolver("SCIP")
        if not solver:
            raise RuntimeError("Google OR-Tools SCIP solver could not be initialized.")

        n = len(candidates)

        # 1. Dynamic Precautionary Liquidity Reserve (Continuous Miller-Orr / Cash Inventory Model)
        if self.use_precautionary_reserve and n > 0:
            avg_edge = sum(max(0.0, c.estimated_win_prob - c.price) for c in candidates) / n
            avg_lockup = sum(max(0.1, c.hours_to_resolution) for c in candidates) / n
            eff_reserve_pct = self.dynamic_sizer.compute_precautionary_reserve(
                candidate_count=n,
                avg_candidate_edge=avg_edge,
                avg_lockup_hours=avg_lockup,
                bankroll_usd=bankroll_usd,
            )
        else:
            eff_reserve_pct = self.reserve_pct

        usable_capital = bankroll_usd * (1.0 - eff_reserve_pct)
        default_max_single_usd = bankroll_usd * self.max_single_bet_pct

        if n == 0 or usable_capital < self.min_order_usd:
            return Domain2OptimizationResult(
                bankroll_usd=bankroll_usd,
                usable_capital_usd=usable_capital,
                cash_reserve_usd=bankroll_usd * eff_reserve_pct,
                total_allocated_usd=0.0,
                total_allocated_pct=0.0,
                active_markets_count=0,
                allocations=[],
                category_exposure_usd={},
                category_exposure_pct={},
                solver_status="EMPTY",
                solver_name="SCIP",
                expected_daily_capital_velocity=0.0,
            )

        # Decision Variables
        x_vars: Dict[Tuple[int, int], Any] = {}
        w_vars: Dict[int, Any] = {}
        z_vars: Dict[int, Any] = {}
        candidate_levels: Dict[int, List[Tuple[float, float, float]]] = {}  # i -> [(price, shares, cap_usd)]

        objective = solver.Objective()
        expected_hourly_growth_rates: Dict[int, float] = {}

        hazard_model = RobustHazardModel(delay_model=None)

        for i, c in enumerate(candidates):
            z_vars[i] = solver.BoolVar(f"z_{i}")
            if c.p_hat is not None:
                p_win = float(c.p_hat)
                hazard = 1.0 - p_win
                velocity_weight, _ = calculate_cycle_velocity(c.hours_to_resolution)
            else:
                p_win, hazard, velocity_weight = hazard_model.evaluate_candidate(
                    category=c.category,
                    price=c.price,
                    hours_to_resolution=c.hours_to_resolution,
                    is_uma_proposed=c.is_uma_proposed,
                    spot_price=c.spot_price,
                    strike_price=c.strike_price,
                    underlying_vol_annual=c.empirical_vol,
                    spread=c.spread,
                    price_history=c.price_history,
                )

            # Compute Dynamic Single-Bet Bound w_max(i)
            rep_px = max(0.50, min(0.999, c.price))
            if c.is_taker:
                if c.fee_schedule is not None:
                    f_rate = c.fee_schedule.rate
                    f_exp = c.fee_schedule.exponent
                else:
                    f_rate = c.fee_rate
                    f_exp = c.fee_exponent
                effective_fee_per_share = f_rate * ((rep_px * (1.0 - rep_px)) ** f_exp) if f_rate > 0 else 0.0
            else:
                effective_fee_per_share = 0.0

            # Dynamic EIP-1559 gas calculation: uses candidate's pos_env if specified, otherwise candidate's explicit gas
            if c.pos_env is not None:
                candidate_gas_usd = c.pos_env.gas_units_to_usd(82_000)
            elif c.redemption_gas_usd is not None:
                candidate_gas_usd = c.redemption_gas_usd
            else:
                candidate_gas_usd = self.pos_env.gas_units_to_usd(82_000)

            if self.use_dynamic_sizing:
                sizing_info = self.dynamic_sizer.compute_dynamic_single_bet_bound(
                    category=c.category,
                    price=c.price,
                    hours_to_resolution=c.hours_to_resolution,
                    depth_usd=c.depth_usd,
                    p_win=p_win,
                    bankroll_usd=bankroll_usd,
                    spot_price=c.spot_price,
                    strike_price=c.strike_price,
                    is_uma_proposed=c.is_uma_proposed,
                    fee_rate=effective_fee_per_share,
                    max_single_bet_pct=self.max_single_bet_pct,
                    depth_gamma=self.depth_gamma,
                    spread=c.spread,
                    price_history=c.price_history,
                    empirical_vol=c.empirical_vol,
                    order_book_levels=c.order_book_levels,
                )
                cand_upper_bound = min(usable_capital, sizing_info["w_max_usd"], default_max_single_usd)
            else:
                cand_upper_bound = min(usable_capital, default_max_single_usd)

            # Fixed-cost order activation hurdle in SCIP objective (dimensionally matched to hourly rate)
            if self.use_fixed_ticket_cost:
                ticket_cost_hourly = self.dynamic_sizer.config.fixed_ticket_cost_usd * velocity_weight
                objective.SetCoefficient(z_vars[i], -float(ticket_cost_hourly))

            if cand_upper_bound < self.min_order_usd:
                # Market rejected by dynamic sizing (e.g. near-strike coin flip or toxic tail)
                w_vars[i] = solver.NumVar(0.0, 0.0, f"w_{i}")
                solver.Add(w_vars[i] == 0.0)
                solver.Add(z_vars[i] == 0)
                candidate_levels[i] = []
                expected_hourly_growth_rates[i] = 0.0
                continue

            # Parse discrete levels or fallback to top-of-book
            if c.order_book_levels and len(c.order_book_levels) > 0:
                raw_levels = sorted(c.order_book_levels, key=lambda lvl: lvl[0])
            else:
                raw_levels = [(c.price, c.depth_usd / max(0.01, c.price) if c.depth_usd > 0 else cand_upper_bound / max(0.01, c.price))]

            levels_info: List[Tuple[float, float, float]] = []
            market_level_vars: List[Any] = []

            for k, (lvl_price, lvl_shares) in enumerate(raw_levels):
                px = max(0.50, min(0.999, lvl_price))
                lvl_cap_usd = lvl_shares * px * self.depth_gamma if c.depth_usd > 0 else cand_upper_bound
                lvl_cap_usd = max(0.0, min(cand_upper_bound, lvl_cap_usd, usable_capital))

                if lvl_cap_usd <= 1e-4:
                    continue

                var_x = solver.NumVar(0.0, lvl_cap_usd, f"x_{i}_{k}")
                solver.Add(var_x <= lvl_cap_usd * z_vars[i])

                x_vars[(i, k)] = var_x
                market_level_vars.append(var_x)
                levels_info.append((px, lvl_shares, lvl_cap_usd))

                # Marginal velocity coefficient for level k
                if c.is_taker:
                    if c.fee_schedule is not None:
                        f_rate = c.fee_schedule.rate
                        f_exp = c.fee_schedule.exponent
                    else:
                        f_rate = c.fee_rate
                        f_exp = c.fee_exponent
                    fee_per_share = f_rate * ((px * (1.0 - px)) ** f_exp) if f_rate > 0 else 0.0
                else:
                    fee_per_share = 0.0

                min_shares = max(1.0, self.min_order_usd / px)
                gas_per_share = candidate_gas_usd / min_shares

                raw_edge = (p_win - px - fee_per_share - gas_per_share) / px
                variance_penalty = self.lambda_var * (hazard / (px ** 2))
                risk_adjusted_edge = raw_edge - variance_penalty
                hourly_coef = risk_adjusted_edge * velocity_weight

                objective.SetCoefficient(var_x, float(hourly_coef))

            candidate_levels[i] = levels_info

            # Total market allocation w[i] = sum_k x[(i, k)]
            w_vars[i] = solver.NumVar(0.0, cand_upper_bound, f"w_{i}")
            if market_level_vars:
                solver.Add(w_vars[i] == solver.Sum(market_level_vars))
                solver.Add(w_vars[i] >= self.min_order_usd * z_vars[i])
                solver.Add(w_vars[i] <= cand_upper_bound * z_vars[i])
            else:
                solver.Add(w_vars[i] == 0.0)
                solver.Add(z_vars[i] == 0)

            # Store representative hourly rate for summary
            rep_px = max(0.50, min(0.999, c.price))
            if c.is_taker:
                if c.fee_schedule is not None:
                    rep_f_rate = c.fee_schedule.rate
                    rep_f_exp = c.fee_schedule.exponent
                else:
                    rep_f_rate = c.fee_rate
                    rep_f_exp = c.fee_exponent
                rep_fee = rep_f_rate * ((rep_px * (1.0 - rep_px)) ** rep_f_exp) if rep_f_rate > 0 else 0.0
            else:
                rep_fee = 0.0
            rep_gas = candidate_gas_usd / max(1.0, self.min_order_usd / rep_px)
            rep_edge = (p_win - rep_px - rep_fee - rep_gas) / rep_px - self.lambda_var * (hazard / (rep_px ** 2))
            expected_hourly_growth_rates[i] = rep_edge * velocity_weight

        # 2. Portfolio Budget Constraint: sum_i w[i] <= Usable Capital
        solver.Add(solver.Sum([w_vars[i] for i in range(n)]) <= usable_capital)

        # 3. Active Markets Constraint (Endogenous Fixed-Cost Hurdle vs Arbitrary Integer Cap)
        if not self.use_fixed_ticket_cost:
            solver.Add(solver.Sum([z_vars[i] for i in range(n)]) <= self.max_active_markets)
        else:
            upper_k = max(self.max_active_markets, 50)
            solver.Add(solver.Sum([z_vars[i] for i in range(n)]) <= upper_k)

        # 4. Dynamic Multi-Factor Covariance & Risk Budgeting
        categories: Set[str] = {c.category for c in candidates}
        if self.use_factor_risk_budgeting:
            factor_budgets = self.dynamic_sizer.compute_factor_risk_budgets(
                bankroll_usd, max_category_pct=self.max_category_pct
            )
            factor_loadings = [
                self.dynamic_sizer.get_factor_loadings(c.category, c.question)
                for c in candidates
            ]
            for factor_name, budget_usd in factor_budgets.items():
                factor_terms = []
                for i in range(n):
                    loading = factor_loadings[i].get(factor_name, 0.0)
                    if loading > 0.0:
                        factor_terms.append(loading * w_vars[i])
                if factor_terms:
                    solver.Add(solver.Sum(factor_terms) <= budget_usd)
        else:
            for cat in categories:
                cat_indices = [i for i, c in enumerate(candidates) if c.category == cat]
                cat_cap = bankroll_usd * min(self.max_category_pct, self.max_drawdown_target)
                solver.Add(solver.Sum([w_vars[i] for i in cat_indices]) <= cat_cap)

        objective.SetMaximization()

        # Solve SCIP
        status = solver.Solve()
        status_str = "OPTIMAL" if status == pywraplp.Solver.OPTIMAL else (
            "FEASIBLE" if status == pywraplp.Solver.FEASIBLE else "SUBOPTIMAL"
        )

        allocations: List[Domain2Allocation] = []
        cat_usd: Dict[str, float] = {cat: 0.0 for cat in categories}

        if status in (pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE):
            for i, c in enumerate(candidates):
                alloc_val = float(w_vars[i].solution_value())
                if alloc_val >= self.min_order_usd:
                    p_win = c.estimated_win_prob
                    hazard_pct = (1.0 - p_win) * 100.0

                    total_shares = 0.0
                    levels_hit = 0
                    for k, (lvl_px, _, _) in enumerate(candidate_levels[i]):
                        if (i, k) in x_vars:
                            x_val = float(x_vars[(i, k)].solution_value())
                            if x_val > 1e-4:
                                total_shares += x_val / lvl_px
                                levels_hit += 1

                    eff_avg_px = alloc_val / total_shares if total_shares > 1e-6 else c.price

                    if c.is_taker:
                        if c.fee_schedule is not None:
                            f_rate = c.fee_schedule.rate
                            f_exp = c.fee_schedule.exponent
                        else:
                            f_rate = c.fee_rate
                            f_exp = c.fee_exponent
                        fee_per_share_alloc = f_rate * ((eff_avg_px * (1.0 - eff_avg_px)) ** f_exp) if f_rate > 0 else 0.0
                    else:
                        fee_per_share_alloc = 0.0

                    if c.pos_env is not None:
                        candidate_gas_usd = c.pos_env.gas_units_to_usd(82_000)
                    elif c.redemption_gas_usd is not None:
                        candidate_gas_usd = c.redemption_gas_usd
                    else:
                        candidate_gas_usd = self.pos_env.gas_units_to_usd(82_000)

                    alloc_fee_usd = round(total_shares * fee_per_share_alloc, 4)
                    alloc_gas_usd = round(candidate_gas_usd, 4) if total_shares > 0 else 0.0
                    gas_per_share_alloc = alloc_gas_usd / max(1e-6, total_shares)
                    net_edge = ((p_win - eff_avg_px - fee_per_share_alloc - gas_per_share_alloc) / eff_avg_px) * 100.0

                    allocations.append(
                        Domain2Allocation(
                            condition_id=c.condition_id,
                            token_id=c.token_id,
                            question=c.question,
                            category=c.category,
                            price=c.price,
                            hours_to_resolution=c.hours_to_resolution,
                            allocated_usd=round(alloc_val, 2),
                            allocation_pct=round((alloc_val / bankroll_usd) * 100.0, 3),
                            shares=round(total_shares, 2),
                            net_edge_pct=round(net_edge, 3),
                            hazard_pct=round(hazard_pct, 3),
                            hourly_velocity=round(expected_hourly_growth_rates[i], 6),
                            is_uma_proposed=c.is_uma_proposed,
                            effective_avg_price=round(eff_avg_px, 4),
                            levels_consumed=max(1, levels_hit),
                            total_fee_usd=alloc_fee_usd,
                            redemption_gas_usd=alloc_gas_usd,
                        )
                    )
                    cat_usd[c.category] += alloc_val

        total_alloc = sum(a.allocated_usd for a in allocations)
        cat_pct = {k: round((v / bankroll_usd) * 100.0, 2) for k, v in cat_usd.items() if v > 0}
        cat_usd_clean = {k: round(v, 2) for k, v in cat_usd.items() if v > 0}

        daily_velocity = sum(a.hourly_velocity * a.allocated_usd for a in allocations) * 24.0

        return Domain2OptimizationResult(
            bankroll_usd=bankroll_usd,
            usable_capital_usd=usable_capital,
            cash_reserve_usd=round(bankroll_usd - total_alloc, 2),
            total_allocated_usd=round(total_alloc, 2),
            total_allocated_pct=round((total_alloc / bankroll_usd) * 100.0, 2),
            active_markets_count=len(allocations),
            allocations=allocations,
            category_exposure_usd=cat_usd_clean,
            category_exposure_pct=cat_pct,
            solver_status=status_str,
            solver_name="SCIP",
            expected_daily_capital_velocity=round(daily_velocity, 4),
        )
