"""Production Marginal Capital Allocator for Domain 2 Settlement Harvester.

Formulates and solves the non-linear orderbook consumption problem for pre-settlement
par discount harvesting:
1. Candidate Profit Function:
       Profit(C) = Shares(C) * 1.000 - Cost(C) - Gas(C)
   where Shares(C) is piecewise-linear and concave over the CLOB ask ladder.
2. Annualized Velocity / Capital Turnover:
       V(C) = (Profit(C) / C) * (Sec_Per_Year / Duration_Sec)
   Total Annualized Value:
       U(C) = V(C) * C = Profit(C) * (Sec_Per_Year / Duration_Sec)
3. Marginal Velocity:
       mu(C) = d(V(C) * C) / dC = (Sec_Per_Year / Duration_Sec) * ((1.000 - P_marginal(C)) / P_marginal(C))
   which is strictly decreasing in capital deployed C as ask prices escalate up the ladder.
4. Multi-Market Portfolio Optimization:
   Equalizes marginal velocity mu_i(C_i) = lambda* across all active funded markets
   subject to portfolio budget, single-market concentration caps, minimum ticket hurdles,
   and open slot bounds.

Implements both:
- Google OR-Tools SCIP Mixed-Integer Linear Program (MILP) solver.
- Exact analytical Greedy Water-Filling Priority Queue solver.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ortools.linear_solver import pywraplp

from polymarketbot.logs import get_logger

log = get_logger("domain2_settlement_allocator")

SECONDS_PER_YEAR: float = 31_536_000.0  # 365 days * 86,400 seconds
DEFAULT_SETTLEMENT_GAS_USD: float = 0.00145


@dataclass
class SettlementCandidate:
    """A resolving market candidate with CLOB ask ladder and settlement horizon."""

    condition_id: str
    token_id: str
    question: str
    target_settle_sec: float
    ask_ladder: List[Tuple[float, float]] = field(default_factory=list)  # [(ask_price, shares_size), ...]
    gas_usd: float = DEFAULT_SETTLEMENT_GAS_USD
    fee_rate: float = 0.0  # Taker fee per dollar or schedule rate
    category: str = "settlement"
    max_market_pct: Optional[float] = None
    min_ticket_usd: Optional[float] = None

    def __post_init__(self) -> None:
        """Sanitize ladder and sort ascending by ask price."""
        self.target_settle_sec = max(1.0, float(self.target_settle_sec))
        clean_ladder: List[Tuple[float, float]] = []
        for px, sz in self.ask_ladder:
            p = float(px)
            s = float(sz)
            if 0.0 < p < 1.0 and s > 0.0:
                clean_ladder.append((p, s))
        clean_ladder.sort(key=lambda x: x[0])
        self.ask_ladder = clean_ladder

    @property
    def best_ask(self) -> float:
        """Lowest available ask price."""
        if self.ask_ladder:
            return self.ask_ladder[0][0]
        return 1.0

    @property
    def total_depth_shares(self) -> float:
        """Total shares available across all ask levels."""
        return sum(sz for _, sz in self.ask_ladder)

    @property
    def total_depth_usd(self) -> float:
        """Total capital capacity in USD across all ask levels."""
        return sum(px * sz for px, sz in self.ask_ladder)

    @property
    def initial_discount(self) -> float:
        """Discount yield at best ask: (1 - P) / P."""
        p = self.best_ask
        return (1.000 - p) / max(0.001, p)

    @property
    def initial_marginal_velocity(self) -> float:
        """Initial marginal velocity d(V*C)/dC at C = 0."""
        p = self.best_ask
        if p >= 1.000 or p <= 0.0:
            return 0.0
        edge = (1.000 - p - self.fee_rate) / p
        return edge * (SECONDS_PER_YEAR / self.target_settle_sec)


@dataclass
class SettlementAllocation:
    """Executable capital allocation for a settlement candidate."""

    condition_id: str
    token_id: str
    question: str
    allocated_usd: float
    shares: float
    effective_avg_price: float
    marginal_price: float
    marginal_velocity: float
    levels_consumed: int
    net_profit_usd: float
    annualized_arr: float
    duration_sec: float
    gas_fee_usd: float
    level_fills: List[Tuple[float, float, float]] = field(default_factory=list)  # [(price, shares, cost_usd)]


@dataclass
class SettlementOptimizationResult:
    """Aggregated portfolio optimization statement for Strategy 2."""

    total_allocated_usd: float
    usable_capital_usd: float
    cash_reserve_usd: float
    active_markets_count: int
    allocations: List[SettlementAllocation]
    expected_portfolio_arr: float
    expected_portfolio_profit_usd: float
    equalized_marginal_velocity: float
    solver_name: str


class MarginalSettlementAllocator:
    """Marginal Capital Allocator for Settlement Harvester (Strategy 2).

    Mathematical Foundation:
    - Orderbook ladder consumption: each level k at price p_k provides shares s_k at cost p_k * s_k.
    - Marginal return of level k: mu_k = ((1.0 - p_k) / p_k) * (Sec_Per_Year / Duration_Sec).
    - Concave return: mu_1 >= mu_2 >= ... >= mu_K.
    - Water-filling / KKT: equalizes mu_i(C_i) = lambda* across all active markets.
    """

    def __init__(
        self,
        min_ticket_usd: float = 25.0,
        max_market_pct: float = 0.25,
        max_open_slots: int = 5,
        default_gas_usd: float = DEFAULT_SETTLEMENT_GAS_USD,
        solver_backend: str = "auto",  # 'scip', 'water_filling', or 'auto'
    ) -> None:
        self.min_ticket_usd = float(min_ticket_usd)
        self.max_market_pct = float(max_market_pct)
        self.max_open_slots = int(max_open_slots)
        self.default_gas_usd = float(default_gas_usd)
        self.solver_backend = solver_backend.lower().strip()

    # =========================================================================
    # Closed-Form Mathematical Primitives
    # =========================================================================

    @staticmethod
    def compute_shares_and_cost(
        ask_ladder: Sequence[Tuple[float, float]],
        capital: float,
    ) -> Tuple[float, float, float, float, int, List[Tuple[float, float, float]]]:
        """Calculates exact orderbook ladder consumption for a given capital amount.

        Returns:
            Tuple: (shares, cost_usd, effective_avg_price, marginal_price, levels_consumed, level_fills)
        """
        if capital <= 0 or not ask_ladder:
            return 0.0, 0.0, 0.0, 0.0, 0, []

        rem_capital = float(capital)
        total_shares = 0.0
        total_cost = 0.0
        levels_consumed = 0
        marginal_px = ask_ladder[0][0]
        level_fills: List[Tuple[float, float, float]] = []

        for px, sz in ask_ladder:
            if rem_capital <= 1e-6:
                break
            level_cap = px * sz
            marginal_px = px
            if rem_capital >= level_cap:
                total_shares += sz
                total_cost += level_cap
                rem_capital -= level_cap
                levels_consumed += 1
                level_fills.append((px, sz, level_cap))
            else:
                shs = rem_capital / px
                total_shares += shs
                total_cost += rem_capital
                level_fills.append((px, shs, rem_capital))
                rem_capital = 0.0
                levels_consumed += 1
                break

        eff_px = total_cost / total_shares if total_shares > 1e-6 else marginal_px
        return total_shares, total_cost, eff_px, marginal_px, levels_consumed, level_fills

    @classmethod
    def marginal_velocity(
        cls,
        price: float,
        duration_sec: float,
        fee_rate: float = 0.0,
    ) -> float:
        """Calculates instantaneous marginal velocity d(V*C)/dC at price P.

        Formula:
            mu(P) = ((1.000 - P - fee) / P) * (Sec_Per_Year / Duration_Sec)
        """
        p = max(0.001, min(0.9999, float(price)))
        tau = max(1.0, float(duration_sec))
        net_edge = (1.000 - p - fee_rate) / p
        return net_edge * (SECONDS_PER_YEAR / tau)

    @classmethod
    def candidate_profit(
        cls,
        candidate: SettlementCandidate,
        capital: float,
    ) -> Tuple[float, float, float]:
        """Calculates (net_profit_usd, annualized_arr, marginal_velocity) for a candidate given capital C.

        Formula:
            Profit(C) = Shares(C) * 1.000 - Cost(C) - Gas
            ARR(C) = (Profit(C) / C) * (Sec_Per_Year / Duration_Sec)
        """
        if capital <= 0 or not candidate.ask_ladder:
            return 0.0, 0.0, 0.0

        shs, cost, eff_px, marg_px, _, _ = cls.compute_shares_and_cost(candidate.ask_ladder, capital)
        gas = candidate.gas_usd if candidate.gas_usd is not None else DEFAULT_SETTLEMENT_GAS_USD
        gross_pnl = shs * 1.000 - cost
        fee = shs * candidate.fee_rate
        net_profit = gross_pnl - fee - gas

        arr = (net_profit / cost) * (SECONDS_PER_YEAR / candidate.target_settle_sec) if cost > 0 else 0.0
        marg_vel = cls.marginal_velocity(marg_px, candidate.target_settle_sec, candidate.fee_rate)
        return round(net_profit, 5), round(arr, 4), round(marg_vel, 4)

    # =========================================================================
    # High-Performance Water-Filling Priority Queue Solver
    # =========================================================================

    def optimize_water_filling(
        self,
        candidates: Sequence[SettlementCandidate],
        usable_capital: float,
        bankroll_usd: float,
        open_slots: Optional[int] = None,
    ) -> SettlementOptimizationResult:
        """Exact Analytical Water-Filling Priority Queue Allocator.

        Discretizes candidate ask ladders into marginal tranches and continuously
        consumes the highest marginal velocity slice until usable capital is exhausted,
        enforcing single-market concentration caps, slot limits, and minimum ticket hurdles.
        """
        usable = float(usable_capital)
        bankroll = max(usable, float(bankroll_usd))
        slots_limit = open_slots if open_slots is not None else self.max_open_slots
        slots_limit = max(1, min(slots_limit, len(candidates)))

        if usable < self.min_ticket_usd or not candidates or slots_limit <= 0:
            return SettlementOptimizationResult(
                total_allocated_usd=0.0,
                usable_capital_usd=usable,
                cash_reserve_usd=usable,
                active_markets_count=0,
                allocations=[],
                expected_portfolio_arr=0.0,
                expected_portfolio_profit_usd=0.0,
                equalized_marginal_velocity=0.0,
                solver_name="WATER_FILLING",
            )

        # 1. Prepare candidate tranche slices
        # Each slice: (-marginal_vel, cand_idx, level_idx, price, shares, tranche_cost)
        max_market_cap = bankroll * self.max_market_pct
        cand_caps: Dict[int, float] = {}
        cand_allocated: Dict[int, float] = {i: 0.0 for i in range(len(candidates))}
        cand_shares: Dict[int, float] = {i: 0.0 for i in range(len(candidates))}
        cand_fills: Dict[int, List[Tuple[float, float, float]]] = {i: [] for i in range(len(candidates))}

        # Heap of available tranches sorted by marginal velocity descending (negated for min-heap)
        heap: List[Tuple[float, int, int, float, float, float]] = []

        for i, cand in enumerate(candidates):
            c_cap = min(usable, max_market_cap)
            if cand.max_market_pct is not None:
                c_cap = min(c_cap, bankroll * cand.max_market_pct)
            cand_caps[i] = c_cap

            if not cand.ask_ladder or c_cap < self.min_ticket_usd:
                continue

            # Push level 0 into heap
            px0, sz0 = cand.ask_ladder[0]
            cost0 = min(px0 * sz0, c_cap)
            if cost0 > 0:
                vel0 = self.marginal_velocity(px0, cand.target_settle_sec, cand.fee_rate)
                if vel0 > 0:
                    heapq.heappush(heap, (-vel0, i, 0, px0, sz0, cost0))

        active_indices: set[int] = set()
        rem_budget = usable
        last_marginal_velocity = 0.0

        while heap and rem_budget >= 1e-4:
            neg_vel, cand_idx, lvl_idx, px, sz, tranche_cap = heapq.heappop(heap)
            current_vel = -neg_vel

            # Check market activation slots constraint
            if cand_idx not in active_indices and len(active_indices) >= slots_limit:
                continue

            current_market_alloc = cand_allocated[cand_idx]
            allowed_for_market = max(0.0, cand_caps[cand_idx] - current_market_alloc)
            if allowed_for_market <= 1e-4:
                continue

            deploy = min(rem_budget, tranche_cap, allowed_for_market)
            if deploy <= 1e-4:
                continue

            shs_bought = deploy / px
            cand_allocated[cand_idx] += deploy
            cand_shares[cand_idx] += shs_bought
            cand_fills[cand_idx].append((px, shs_bought, deploy))
            rem_budget -= deploy
            active_indices.add(cand_idx)
            last_marginal_velocity = current_vel

            # If tranche had leftover capacity that was bounded by rem_budget, we're done
            leftover_in_tranche = tranche_cap - deploy
            if leftover_in_tranche > 1e-4 and rem_budget > 1e-4 and (cand_allocated[cand_idx] < cand_caps[cand_idx]):
                heapq.heappush(heap, (-current_vel, cand_idx, lvl_idx, px, leftover_in_tranche / px, leftover_in_tranche))
                continue

            # If tranche was fully absorbed, push next level of this candidate
            cand = candidates[cand_idx]
            next_lvl = lvl_idx + 1
            if next_lvl < len(cand.ask_ladder) and (cand_allocated[cand_idx] < cand_caps[cand_idx]):
                next_px, next_sz = cand.ask_ladder[next_lvl]
                next_cost = next_px * next_sz
                next_vel = self.marginal_velocity(next_px, cand.target_settle_sec, cand.fee_rate)
                if next_vel > 0:
                    heapq.heappush(heap, (-next_vel, cand_idx, next_lvl, next_px, next_sz, next_cost))

        # 2. Minimum ticket size enforcement
        final_allocations: List[SettlementAllocation] = []
        for i in active_indices:
            alloc_val = cand_allocated[i]
            cand = candidates[i]
            ticket_floor = cand.min_ticket_usd or self.min_ticket_usd

            if alloc_val < ticket_floor:
                # Prune sub-ticket allocation if cannot meet floor
                rem_budget += alloc_val
                continue

            total_shs = cand_shares[i]
            eff_px = alloc_val / total_shs if total_shs > 0 else cand.best_ask
            fills = cand_fills[i]
            marg_px = fills[-1][0] if fills else cand.best_ask
            marg_vel = self.marginal_velocity(marg_px, cand.target_settle_sec, cand.fee_rate)

            gas = cand.gas_usd if cand.gas_usd is not None else DEFAULT_SETTLEMENT_GAS_USD
            net_profit = (total_shs * 1.000) - alloc_val - gas
            arr = (net_profit / alloc_val) * (SECONDS_PER_YEAR / cand.target_settle_sec) if alloc_val > 0 else 0.0

            final_allocations.append(
                SettlementAllocation(
                    condition_id=cand.condition_id,
                    token_id=cand.token_id,
                    question=cand.question,
                    allocated_usd=round(alloc_val, 2),
                    shares=round(total_shs, 2),
                    effective_avg_price=round(eff_px, 4),
                    marginal_price=round(marg_px, 4),
                    marginal_velocity=round(marg_vel, 4),
                    levels_consumed=len(fills),
                    net_profit_usd=round(net_profit, 4),
                    annualized_arr=round(arr, 4),
                    duration_sec=cand.target_settle_sec,
                    gas_fee_usd=gas,
                    level_fills=fills,
                )
            )

        # Sort allocations by ARR descending
        final_allocations.sort(key=lambda a: a.annualized_arr, reverse=True)

        tot_alloc = sum(a.allocated_usd for a in final_allocations)
        tot_profit = sum(a.net_profit_usd for a in final_allocations)
        port_arr = (
            sum(a.annualized_arr * a.allocated_usd for a in final_allocations) / tot_alloc
            if tot_alloc > 0
            else 0.0
        )

        return SettlementOptimizationResult(
            total_allocated_usd=round(tot_alloc, 2),
            usable_capital_usd=round(usable, 2),
            cash_reserve_usd=round(usable - tot_alloc, 2),
            active_markets_count=len(final_allocations),
            allocations=final_allocations,
            expected_portfolio_arr=round(port_arr, 4),
            expected_portfolio_profit_usd=round(tot_profit, 4),
            equalized_marginal_velocity=round(last_marginal_velocity, 4),
            solver_name="WATER_FILLING",
        )

    # =========================================================================
    # Google OR-Tools SCIP MILP Solver
    # =========================================================================

    def optimize_scip(
        self,
        candidates: Sequence[SettlementCandidate],
        usable_capital: float,
        bankroll_usd: float,
        open_slots: Optional[int] = None,
    ) -> SettlementOptimizationResult:
        """Google OR-Tools SCIP Mixed-Integer Linear Program (MILP).

        Solves:
            max sum_{i, k} mu_{i, k} * x_{i, k} - sum_i (AnnualizedGas_i * z_i)
        Subject to:
            sum_{i, k} x_{i, k} <= UsableCapital
            sum_k x_{i, k} >= min_ticket * z_i
            sum_k x_{i, k} <= max_market_cap * z_i
            x_{i, k} <= LevelCap_{i, k} * z_i
            sum_i z_i <= OpenSlots
            z_i in {0, 1}
        """
        solver = pywraplp.Solver.CreateSolver("SCIP")
        if not solver:
            log.warning("Google OR-Tools SCIP solver unavailable; falling back to Water-Filling.")
            return self.optimize_water_filling(candidates, usable_capital, bankroll_usd, open_slots)

        usable = float(usable_capital)
        bankroll = max(usable, float(bankroll_usd))
        slots_limit = open_slots if open_slots is not None else self.max_open_slots
        slots_limit = max(1, min(slots_limit, len(candidates)))

        if usable < self.min_ticket_usd or not candidates:
            return SettlementOptimizationResult(
                total_allocated_usd=0.0,
                usable_capital_usd=usable,
                cash_reserve_usd=usable,
                active_markets_count=0,
                allocations=[],
                expected_portfolio_arr=0.0,
                expected_portfolio_profit_usd=0.0,
                equalized_marginal_velocity=0.0,
                solver_name="SCIP",
            )

        n = len(candidates)
        x_vars: Dict[Tuple[int, int], Any] = {}
        w_vars: Dict[int, Any] = {}
        z_vars: Dict[int, Any] = {}
        market_level_caps: Dict[int, List[Tuple[float, float, float, float]]] = {}

        objective = solver.Objective()
        max_market_cap = bankroll * self.max_market_pct

        for i, cand in enumerate(candidates):
            z_vars[i] = solver.BoolVar(f"z_{i}")
            cand_cap = min(usable, max_market_cap)
            if cand.max_market_pct is not None:
                cand_cap = min(cand_cap, bankroll * cand.max_market_pct)

            level_info: List[Tuple[float, float, float, float]] = []
            cand_level_vars: List[Any] = []

            # Gas hurdle annualized to match ARR objective units
            ann_gas = (cand.gas_usd * SECONDS_PER_YEAR) / cand.target_settle_sec
            objective.SetCoefficient(z_vars[i], -float(ann_gas))

            for k, (px, sz) in enumerate(cand.ask_ladder):
                level_cap = min(cand_cap, px * sz)
                if level_cap <= 1e-4:
                    continue

                var_x = solver.NumVar(0.0, level_cap, f"x_{i}_{k}")
                solver.Add(var_x <= level_cap * z_vars[i])

                vel = self.marginal_velocity(px, cand.target_settle_sec, cand.fee_rate)
                objective.SetCoefficient(var_x, float(vel))

                x_vars[(i, k)] = var_x
                cand_level_vars.append(var_x)
                level_info.append((px, sz, level_cap, vel))

            market_level_caps[i] = level_info
            w_vars[i] = solver.NumVar(0.0, cand_cap, f"w_{i}")

            ticket_floor = cand.min_ticket_usd or self.min_ticket_usd
            if cand_level_vars:
                solver.Add(w_vars[i] == solver.Sum(cand_level_vars))
                solver.Add(w_vars[i] >= ticket_floor * z_vars[i])
                solver.Add(w_vars[i] <= cand_cap * z_vars[i])
            else:
                solver.Add(w_vars[i] == 0.0)
                solver.Add(z_vars[i] == 0)

        # Budget constraint
        solver.Add(solver.Sum([w_vars[i] for i in range(n)]) <= usable)

        # Slots constraint
        solver.Add(solver.Sum([z_vars[i] for i in range(n)]) <= slots_limit)

        objective.SetMaximization()
        status = solver.Solve()

        if status not in (pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE):
            log.warning("SCIP solver returned infeasible or suboptimal; falling back to Water-Filling.")
            return self.optimize_water_filling(candidates, usable_capital, bankroll_usd, open_slots)

        allocations: List[SettlementAllocation] = []
        for i, cand in enumerate(candidates):
            alloc_val = float(w_vars[i].solution_value())
            ticket_floor = cand.min_ticket_usd or self.min_ticket_usd

            if alloc_val >= ticket_floor:
                total_shs = 0.0
                fills: List[Tuple[float, float, float]] = []
                for k, (px, sz, cap, _) in enumerate(market_level_caps[i]):
                    if (i, k) in x_vars:
                        xv = float(x_vars[(i, k)].solution_value())
                        if xv > 1e-4:
                            shs_k = xv / px
                            total_shs += shs_k
                            fills.append((px, shs_k, xv))

                eff_px = alloc_val / total_shs if total_shs > 0 else cand.best_ask
                marg_px = fills[-1][0] if fills else cand.best_ask
                marg_vel = self.marginal_velocity(marg_px, cand.target_settle_sec, cand.fee_rate)
                gas = cand.gas_usd if cand.gas_usd is not None else DEFAULT_SETTLEMENT_GAS_USD
                net_profit = (total_shs * 1.000) - alloc_val - gas
                arr = (net_profit / alloc_val) * (SECONDS_PER_YEAR / cand.target_settle_sec) if alloc_val > 0 else 0.0

                allocations.append(
                    SettlementAllocation(
                        condition_id=cand.condition_id,
                        token_id=cand.token_id,
                        question=cand.question,
                        allocated_usd=round(alloc_val, 2),
                        shares=round(total_shs, 2),
                        effective_avg_price=round(eff_px, 4),
                        marginal_price=round(marg_px, 4),
                        marginal_velocity=round(marg_vel, 4),
                        levels_consumed=len(fills),
                        net_profit_usd=round(net_profit, 4),
                        annualized_arr=round(arr, 4),
                        duration_sec=cand.target_settle_sec,
                        gas_fee_usd=gas,
                        level_fills=fills,
                    )
                )

        allocations.sort(key=lambda a: a.annualized_arr, reverse=True)
        tot_alloc = sum(a.allocated_usd for a in allocations)
        tot_profit = sum(a.net_profit_usd for a in allocations)
        port_arr = (
            sum(a.annualized_arr * a.allocated_usd for a in allocations) / tot_alloc
            if tot_alloc > 0
            else 0.0
        )
        min_marg_vel = min((a.marginal_velocity for a in allocations), default=0.0)

        return SettlementOptimizationResult(
            total_allocated_usd=round(tot_alloc, 2),
            usable_capital_usd=round(usable, 2),
            cash_reserve_usd=round(usable - tot_alloc, 2),
            active_markets_count=len(allocations),
            allocations=allocations,
            expected_portfolio_arr=round(port_arr, 4),
            expected_portfolio_profit_usd=round(tot_profit, 4),
            equalized_marginal_velocity=round(min_marg_vel, 4),
            solver_name="SCIP",
        )

    # =========================================================================
    # Unified Public Entrypoint
    # =========================================================================

    def allocate(
        self,
        candidates: Sequence[SettlementCandidate],
        usable_capital: float,
        bankroll_usd: float,
        open_slots: Optional[int] = None,
    ) -> SettlementOptimizationResult:
        """Allocates usable capital across candidates using selected solver backend."""
        if self.solver_backend == "water_filling":
            return self.optimize_water_filling(candidates, usable_capital, bankroll_usd, open_slots)
        elif self.solver_backend == "scip":
            return self.optimize_scip(candidates, usable_capital, bankroll_usd, open_slots)
        else:  # 'auto'
            try:
                return self.optimize_scip(candidates, usable_capital, bankroll_usd, open_slots)
            except Exception as exc:
                log.warning(f"SCIP allocation failed ({exc}), falling back to Water-Filling.")
                return self.optimize_water_filling(candidates, usable_capital, bankroll_usd, open_slots)

    def compare_with_uniform(
        self,
        candidates: Sequence[SettlementCandidate],
        usable_capital: float,
        bankroll_usd: float,
        open_slots: int = 5,
    ) -> Dict[str, Any]:
        """Provides a mathematical comparison between Marginal Allocation and Uniform Tranche Sizing."""
        # 1. Marginal Allocation
        marginal_res = self.allocate(candidates, usable_capital, bankroll_usd, open_slots)

        # 2. Naive Uniform Allocation (Status Quo in run_live_paper_soak.py)
        # slot_alloc = min(usable_cash / open_slots, equity * max_market_pct, usable_cash)
        usable = float(usable_capital)
        bankroll = float(bankroll_usd)
        slot_alloc = min(usable / max(1, open_slots), bankroll * self.max_market_pct, usable)

        uniform_allocs: List[Dict[str, Any]] = []
        rem_cash = usable
        slots_left = open_slots

        # Naively allocate in descending order of best ask discount
        sorted_cands = sorted(candidates, key=lambda c: c.initial_marginal_velocity, reverse=True)

        for c in sorted_cands:
            if slots_left <= 0 or rem_cash < self.min_ticket_usd:
                break
            deploy = min(slot_alloc, rem_cash, c.total_depth_usd)
            if deploy < self.min_ticket_usd:
                continue

            shs, cost, eff_px, _, lvls, _ = self.compute_shares_and_cost(c.ask_ladder, deploy)
            gas = c.gas_usd if c.gas_usd is not None else DEFAULT_SETTLEMENT_GAS_USD
            net_profit = shs * 1.000 - cost - gas
            arr = (net_profit / cost) * (SECONDS_PER_YEAR / c.target_settle_sec) if cost > 0 else 0.0

            uniform_allocs.append({
                "condition_id": c.condition_id,
                "allocated_usd": deploy,
                "shares": shs,
                "effective_price": eff_px,
                "net_profit_usd": net_profit,
                "arr": arr,
                "duration_sec": c.target_settle_sec,
            })
            rem_cash -= deploy
            slots_left -= 1

        tot_uniform_alloc = sum(u["allocated_usd"] for u in uniform_allocs)
        tot_uniform_profit = sum(u["net_profit_usd"] for u in uniform_allocs)
        uniform_arr = (
            sum(u["arr"] * u["allocated_usd"] for u in uniform_allocs) / tot_uniform_alloc
            if tot_uniform_alloc > 0
            else 0.0
        )

        return {
            "marginal": {
                "allocated_usd": marginal_res.total_allocated_usd,
                "net_profit_usd": marginal_res.expected_portfolio_profit_usd,
                "portfolio_arr": marginal_res.expected_portfolio_arr,
                "active_markets": marginal_res.active_markets_count,
                "allocations": marginal_res.allocations,
            },
            "uniform": {
                "allocated_usd": round(tot_uniform_alloc, 2),
                "net_profit_usd": round(tot_uniform_profit, 4),
                "portfolio_arr": round(uniform_arr, 4),
                "active_markets": len(uniform_allocs),
                "allocations": uniform_allocs,
            },
            "marginal_vs_uniform_profit_ratio": (
                round(marginal_res.expected_portfolio_profit_usd / tot_uniform_profit, 3)
                if tot_uniform_profit > 0
                else float("inf")
            ),
            "marginal_vs_uniform_arr_ratio": (
                round(marginal_res.expected_portfolio_arr / uniform_arr, 3)
                if uniform_arr > 0
                else float("inf")
            ),
        }
