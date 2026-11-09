"""Production Marginal Capital Allocator for Domain 2 Settlement Harvester.

Formulates and solves the non-linear orderbook consumption problem for pre-settlement
par discount harvesting.

The allocator owns both *how many markets* and *how much capital per market*.  It
consumes the full CLOB ask ladder of every candidate under the available cash and
only the venue's real constraints: minimum order size, gas, the market's taker fee
schedule, and the hazard-model survival probability.  There is no target slot count,
no bankroll-percentage concentration cap, and no static minimum ticket.

Expected profit of consuming capital C on market i:

    Profit_i(C) = p_win_i * Shares_i(C) - Cost_i(C) - Fees_i(C) - Gas_i

where ``Shares_i(C)`` is piecewise-linear and concave over the ask ladder, ``p_win``
is the conservative survival probability from ``RobustHazardModel``, ``Fees_i`` are
exact per-level taker fees from the market's Gamma fee schedule, and ``Gas_i`` is the
single on-chain redemption cost.

Annualised marginal velocity (expected edge per dollar per year):

    mu_i(C) = d(Profit_i * Sec_Per_Year / Duration_i) / dC
            = ((p_win_i - P_marginal - fee_per_share) / P_marginal)
              * (Sec_Per_Year / Duration_i)

which is strictly decreasing in capital as ask prices escalate up the ladder.
Water-filling equalises ``mu_i(C_i) = lambda*`` across funded markets subject only
to the budget and real ladder depth.  A market whose post-allocation expected net
profit is non-positive, or whose fill is below the venue minimum order size, is
pruned and its capital is re-offered to the next-best tranches until a fixed point.

Implements both:
- Google OR-Tools SCIP Mixed-Integer Linear Program (fixed-charge gas activation).
- Exact analytical Greedy Water-Filling Priority Queue solver.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ortools.linear_solver import pywraplp

from polymarketbot.fees import FeeSchedule
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
    fee_schedule: Optional[FeeSchedule] = None
    p_win: float = 1.0  # Conservative survival probability from the hazard model
    min_order_shares: float = 0.0  # Venue orderMinSize (shares); 0 means unconstrained
    category: str = "settlement"

    def __post_init__(self) -> None:
        """Sanitize ladder and sort ascending by ask price."""
        self.target_settle_sec = max(1.0, float(self.target_settle_sec))
        self.p_win = max(0.0, min(1.0, float(self.p_win)))
        self.min_order_shares = max(0.0, float(self.min_order_shares))
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
    def total_depth_usd(self) -> float:
        """Real capital capacity in USD across all ask levels (the only market cap)."""
        return sum(px * sz for px, sz in self.ask_ladder)

    def per_share_fee(self, price: float) -> float:
        """Exact taker fee per share at ``price`` from the market's fee schedule.

        Polymarket charges ``shares * rate * (p * (1 - p)) ** exponent``; the
        per-share component is therefore independent of order size.
        """
        if self.fee_schedule is None or self.fee_schedule.rate <= 0.0:
            return 0.0
        p = min(1.0, max(0.0, float(price)))
        return self.fee_schedule.rate * ((p * (1.0 - p)) ** self.fee_schedule.exponent)

    def fee_for_fills(self, fills: Sequence[Tuple[float, float, float]]) -> float:
        """Exact total taker fee for a set of ``(price, shares, cost)`` fills."""
        return sum(shs * self.per_share_fee(px) for px, shs, _ in fills)

    @property
    def initial_marginal_velocity(self) -> float:
        """Initial marginal velocity d(V*C)/dC at C = 0, net of survival and fees."""
        p = self.best_ask
        if p >= 1.000 or p <= 0.0:
            return 0.0
        edge = (self.p_win - p - self.per_share_fee(p)) / p
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
    - Orderbook ladder consumption: level k at price p_k provides shares s_k at cost p_k*s_k.
    - Marginal expected return of level k:
          mu_k = ((p_win - p_k - fee_k) / p_k) * (Sec_Per_Year / Duration_Sec).
    - Concave return: mu_1 >= mu_2 >= ... >= mu_K (prices ascending, fees small).
    - Water-filling / KKT: equalises mu_i(C_i) = lambda* across all funded markets.
    - Fixed charges: gas is paid once per activated market; markets whose expected
      net profit is non-positive, or whose fill is below the venue minimum order,
      are pruned and their budget re-offered to the remaining tranches.
    """

    def __init__(self, solver_backend: str = "auto") -> None:
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
            if rem_capital <= 1e-9:
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

        eff_px = total_cost / total_shares if total_shares > 1e-9 else marginal_px
        return total_shares, total_cost, eff_px, marginal_px, levels_consumed, level_fills

    @classmethod
    def marginal_velocity(
        cls,
        price: float,
        duration_sec: float,
        fee_per_share: float = 0.0,
        p_win: float = 1.0,
    ) -> float:
        """Calculates instantaneous marginal velocity d(V*C)/dC at price P.

        Formula:
            mu(P) = ((p_win - P - fee_per_share) / P) * (Sec_Per_Year / Duration_Sec)
        """
        p = max(0.001, min(0.9999, float(price)))
        tau = max(1.0, float(duration_sec))
        net_edge = (float(p_win) - p - float(fee_per_share)) / p
        return net_edge * (SECONDS_PER_YEAR / tau)

    @classmethod
    def candidate_profit(
        cls,
        candidate: SettlementCandidate,
        capital: float,
    ) -> Tuple[float, float, float]:
        """Calculates (net_profit_usd, annualized_arr, marginal_velocity) for a candidate given capital C.

        Formula:
            Profit(C) = p_win * Shares(C) * 1.000 - Cost(C) - Fees(C) - Gas
            ARR(C) = (Profit(C) / C) * (Sec_Per_Year / Duration_Sec)
        """
        if capital <= 0 or not candidate.ask_ladder:
            return 0.0, 0.0, 0.0

        shs, cost, eff_px, marg_px, _, fills = cls.compute_shares_and_cost(candidate.ask_ladder, capital)
        gas = candidate.gas_usd if candidate.gas_usd is not None else DEFAULT_SETTLEMENT_GAS_USD
        fee = candidate.fee_for_fills(fills)
        net_profit = candidate.p_win * shs - cost - fee - gas

        arr = (net_profit / cost) * (SECONDS_PER_YEAR / candidate.target_settle_sec) if cost > 0 else 0.0
        marg_vel = cls.marginal_velocity(
            marg_px,
            candidate.target_settle_sec,
            candidate.per_share_fee(marg_px),
            candidate.p_win,
        )
        return round(net_profit, 5), round(arr, 4), round(marg_vel, 4)

    # =========================================================================
    # Shared result / pruning helpers
    # =========================================================================

    @staticmethod
    def _empty_result(usable: float, solver_name: str) -> SettlementOptimizationResult:
        return SettlementOptimizationResult(
            total_allocated_usd=0.0,
            usable_capital_usd=float(usable),
            cash_reserve_usd=float(usable),
            active_markets_count=0,
            allocations=[],
            expected_portfolio_arr=0.0,
            expected_portfolio_profit_usd=0.0,
            equalized_marginal_velocity=0.0,
            solver_name=solver_name,
        )

    @staticmethod
    def _invalid_cids(
        by_cid: Dict[str, SettlementCandidate],
        allocations: Sequence[SettlementAllocation],
    ) -> set[str]:
        """Markets that must be pruned: negative expected profit or sub-minimum order."""
        dropped: set[str] = set()
        for alloc in allocations:
            cand = by_cid.get(alloc.condition_id)
            if cand is None:
                dropped.add(alloc.condition_id)
                continue
            if alloc.net_profit_usd <= 0.0:
                dropped.add(alloc.condition_id)
            elif alloc.shares + 1e-9 < cand.min_order_shares:
                dropped.add(alloc.condition_id)
        return dropped

    @staticmethod
    def _finalize(
        candidates: Sequence[SettlementCandidate],
        allocated: Dict[int, float],
        shares: Dict[int, float],
        fills: Dict[int, List[Tuple[float, float, float]]],
        usable: float,
        solver_name: str,
    ) -> SettlementOptimizationResult:
        allocations: List[SettlementAllocation] = []
        for i, cost in allocated.items():
            if cost <= 1e-9:
                continue
            cand = candidates[i]
            total_shs = shares[i]
            level_fills = fills[i]
            fee = cand.fee_for_fills(level_fills)
            gas = cand.gas_usd if cand.gas_usd is not None else DEFAULT_SETTLEMENT_GAS_USD
            net_profit = cand.p_win * total_shs - cost - fee - gas
            arr = (net_profit / cost) * (SECONDS_PER_YEAR / cand.target_settle_sec) if cost > 0 else 0.0
            eff_px = cost / total_shs if total_shs > 1e-9 else cand.best_ask
            marg_px = level_fills[-1][0] if level_fills else cand.best_ask
            marg_vel = MarginalSettlementAllocator.marginal_velocity(
                marg_px,
                cand.target_settle_sec,
                cand.per_share_fee(marg_px),
                cand.p_win,
            )
            allocations.append(
                SettlementAllocation(
                    condition_id=cand.condition_id,
                    token_id=cand.token_id,
                    question=cand.question,
                    allocated_usd=round(cost, 2),
                    shares=round(total_shs, 2),
                    effective_avg_price=round(eff_px, 4),
                    marginal_price=round(marg_px, 4),
                    marginal_velocity=round(marg_vel, 4),
                    levels_consumed=len(level_fills),
                    net_profit_usd=round(net_profit, 4),
                    annualized_arr=round(arr, 4),
                    duration_sec=cand.target_settle_sec,
                    gas_fee_usd=gas,
                    level_fills=level_fills,
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
        clearing_velocity = min((a.marginal_velocity for a in allocations), default=0.0)

        return SettlementOptimizationResult(
            total_allocated_usd=round(tot_alloc, 2),
            usable_capital_usd=round(usable, 2),
            cash_reserve_usd=round(max(0.0, usable - tot_alloc), 2),
            active_markets_count=len(allocations),
            allocations=allocations,
            expected_portfolio_arr=round(port_arr, 4),
            expected_portfolio_profit_usd=round(tot_profit, 4),
            equalized_marginal_velocity=round(clearing_velocity, 4),
            solver_name=solver_name,
        )

    # =========================================================================
    # High-Performance Water-Filling Priority Queue Solver
    # =========================================================================

    def _water_fill_once(
        self,
        candidates: Sequence[SettlementCandidate],
        budget: float,
    ) -> Tuple[Dict[int, float], Dict[int, float], Dict[int, List[Tuple[float, float, float]]], float]:
        """Consume the highest-marginal-velocity tranches until capital or depth runs out.

        No market cap and no slot limit: a candidate may consume its entire ladder.
        """
        allocated: Dict[int, float] = {}
        shares: Dict[int, float] = {}
        fills: Dict[int, List[Tuple[float, float, float]]] = {}
        # Heap entries: (-marginal_velocity, candidate_index, level_index, price, shares)
        heap: List[Tuple[float, int, int, float, float]] = []

        for i, cand in enumerate(candidates):
            if not cand.ask_ladder:
                continue
            px, sz = cand.ask_ladder[0]
            vel = self.marginal_velocity(
                px, cand.target_settle_sec, cand.per_share_fee(px), cand.p_win
            )
            if vel > 0.0:
                heapq.heappush(heap, (-vel, i, 0, px, sz))

        rem = float(budget)
        while heap and rem > 1e-9:
            neg_vel, i, k, px, sz = heapq.heappop(heap)
            level_cost = px * sz
            if level_cost <= 1e-9:
                continue
            deploy = min(rem, level_cost)
            if deploy <= 1e-9:
                continue

            shs = deploy / px
            allocated[i] = allocated.get(i, 0.0) + deploy
            shares[i] = shares.get(i, 0.0) + shs
            fills.setdefault(i, []).append((px, shs, deploy))
            rem -= deploy

            if deploy < level_cost - 1e-9:
                # Budget exhausted inside this level.
                break

            cand = candidates[i]
            next_level = k + 1
            if next_level < len(cand.ask_ladder):
                npx, nsz = cand.ask_ladder[next_level]
                nvel = self.marginal_velocity(
                    npx, cand.target_settle_sec, cand.per_share_fee(npx), cand.p_win
                )
                if nvel > 0.0:
                    heapq.heappush(heap, (-nvel, i, next_level, npx, nsz))

        return allocated, shares, fills, rem

    def _water_fill_once_allocations(
        self,
        candidates: Sequence[SettlementCandidate],
        budget: float,
    ) -> SettlementOptimizationResult:
        allocated, shares, fills, _ = self._water_fill_once(candidates, budget)
        return self._finalize(candidates, allocated, shares, fills, budget, "WATER_FILLING")

    def optimize_water_filling(
        self,
        candidates: Sequence[SettlementCandidate],
        usable_capital: float,
    ) -> SettlementOptimizationResult:
        """Exact analytical water-filling with fixed-charge / venue-minimum pruning."""
        usable = float(usable_capital)
        if usable <= 0.0 or not candidates:
            return self._empty_result(usable, "WATER_FILLING")

        by_cid = {c.condition_id: c for c in candidates}
        active: List[SettlementCandidate] = [c for c in candidates if c.initial_marginal_velocity > 0.0]
        while active:
            result = self._water_fill_once_allocations(active, usable)
            dropped = self._invalid_cids(by_cid, result.allocations)
            if not dropped:
                return result
            active = [c for c in active if c.condition_id not in dropped]

        return self._empty_result(usable, "WATER_FILLING")

    # =========================================================================
    # Google OR-Tools SCIP MILP Solver
    # =========================================================================

    def _scip_once(
        self,
        candidates: Sequence[SettlementCandidate],
        usable: float,
    ) -> Optional[SettlementOptimizationResult]:
        solver = pywraplp.Solver.CreateSolver("SCIP")
        if not solver:
            return None

        n = len(candidates)
        x_vars: Dict[Tuple[int, int], Any] = {}
        w_vars: Dict[int, Any] = {}
        z_vars: Dict[int, Any] = {}
        market_level_caps: Dict[int, List[Tuple[float, float, float]]] = {}

        objective = solver.Objective()

        for i, cand in enumerate(candidates):
            z_vars[i] = solver.BoolVar(f"z_{i}")
            cand_cap = min(usable, cand.total_depth_usd)

            level_info: List[Tuple[float, float, float]] = []
            cand_level_vars: List[Any] = []

            # Gas is a fixed charge per activated market; annualise to objective units.
            ann_gas = (cand.gas_usd * SECONDS_PER_YEAR) / cand.target_settle_sec
            objective.SetCoefficient(z_vars[i], -float(ann_gas))

            for k, (px, sz) in enumerate(cand.ask_ladder):
                level_cap = min(cand_cap, px * sz)
                if level_cap <= 1e-9:
                    continue

                var_x = solver.NumVar(0.0, level_cap, f"x_{i}_{k}")
                solver.Add(var_x <= level_cap * z_vars[i])

                vel = self.marginal_velocity(
                    px, cand.target_settle_sec, cand.per_share_fee(px), cand.p_win
                )
                objective.SetCoefficient(var_x, float(vel))

                x_vars[(i, k)] = var_x
                cand_level_vars.append(var_x)
                level_info.append((px, sz, level_cap))

            market_level_caps[i] = level_info
            w_vars[i] = solver.NumVar(0.0, cand_cap, f"w_{i}")

            # Venue minimum order size is a share count; its cheapest possible
            # notional at the best ask is a valid necessary lower bound.
            ticket_floor_usd = cand.min_order_shares * cand.best_ask
            if cand_level_vars:
                solver.Add(w_vars[i] == solver.Sum(cand_level_vars))
                solver.Add(w_vars[i] >= ticket_floor_usd * z_vars[i])
                solver.Add(w_vars[i] <= cand_cap * z_vars[i])
            else:
                solver.Add(w_vars[i] == 0.0)
                solver.Add(z_vars[i] == 0)

        # Budget constraint: the only portfolio-level cap.
        solver.Add(solver.Sum([w_vars[i] for i in range(n)]) <= usable)

        objective.SetMaximization()
        status = solver.Solve()

        if status not in (pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE):
            return None

        allocated: Dict[int, float] = {}
        shares: Dict[int, float] = {}
        fills: Dict[int, List[Tuple[float, float, float]]] = {}
        for i, cand in enumerate(candidates):
            alloc_val = float(w_vars[i].solution_value())
            if alloc_val <= 1e-9:
                continue
            total_shs = 0.0
            level_fills: List[Tuple[float, float, float]] = []
            for k, (px, _sz, _cap) in enumerate(market_level_caps[i]):
                var = x_vars.get((i, k))
                if var is None:
                    continue
                xv = float(var.solution_value())
                if xv > 1e-9:
                    shs_k = xv / px
                    total_shs += shs_k
                    level_fills.append((px, shs_k, xv))
            if total_shs <= 0.0:
                continue
            allocated[i] = alloc_val
            shares[i] = total_shs
            fills[i] = level_fills

        return self._finalize(candidates, allocated, shares, fills, usable, "SCIP")

    def optimize_scip(
        self,
        candidates: Sequence[SettlementCandidate],
        usable_capital: float,
    ) -> SettlementOptimizationResult:
        """Google OR-Tools SCIP MILP with fixed-charge gas and venue-minimum pruning.

        Solves:
            max sum_{i, k} mu_{i, k} * x_{i, k} - sum_i (AnnualizedGas_i * z_i)
        Subject to:
            sum_{i, k} x_{i, k} <= UsableCapital
            sum_k x_{i, k} >= min_order_notional_i * z_i
            sum_k x_{i, k} <= ladder_depth_i * z_i
            x_{i, k} <= LevelCap_{i, k} * z_i
            z_i in {0, 1}
        """
        usable = float(usable_capital)
        if usable <= 0.0 or not candidates:
            return self._empty_result(usable, "SCIP")

        by_cid = {c.condition_id: c for c in candidates}
        active: List[SettlementCandidate] = [c for c in candidates if c.initial_marginal_velocity > 0.0]
        while active:
            result = self._scip_once(active, usable)
            if result is None:
                log.warning("SCIP solver unavailable or infeasible; falling back to Water-Filling.")
                return self.optimize_water_filling(active, usable)
            dropped = self._invalid_cids(by_cid, result.allocations)
            if not dropped:
                return result
            active = [c for c in active if c.condition_id not in dropped]

        return self._empty_result(usable, "SCIP")

    # =========================================================================
    # Unified Public Entrypoint
    # =========================================================================

    def allocate(
        self,
        candidates: Sequence[SettlementCandidate],
        usable_capital: float,
    ) -> SettlementOptimizationResult:
        """Allocates usable capital across candidates using the selected solver backend."""
        if self.solver_backend == "water_filling":
            return self.optimize_water_filling(candidates, usable_capital)
        if self.solver_backend == "scip":
            return self.optimize_scip(candidates, usable_capital)

        try:
            return self.optimize_scip(candidates, usable_capital)
        except Exception as exc:
            log.warning(f"SCIP allocation failed ({exc}), falling back to Water-Filling.")
            return self.optimize_water_filling(candidates, usable_capital)
