"""Google OR-Tools SCIP MILP solver of the allocator."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from ortools.linear_solver import pywraplp

from ._shared import SECONDS_PER_YEAR, log
from .types import SettlementCandidate, SettlementOptimizationResult


class ScipMixin:
    """Fixed-charge gas activation as a mixed-integer linear program."""

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

        return self._finalize(candidates, allocated, shares, fills, usable)

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
            return self._empty_result(usable)

        by_cid = {c.condition_id: c for c in candidates}
        active: List[SettlementCandidate] = [c for c in candidates if c.is_fundable()]
        while active:
            result = self._scip_once(active, usable)
            if result is None:
                log.warning("SCIP solver unavailable or infeasible; falling back to Water-Filling.")
                return self.optimize_water_filling(active, usable)
            dropped = self._invalid_cids(by_cid, result.allocations)
            if not dropped:
                return result
            active = [c for c in active if c.condition_id not in dropped]

        return self._empty_result(usable)
