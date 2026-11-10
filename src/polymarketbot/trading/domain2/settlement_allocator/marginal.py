"""Water-filling allocation core of :class:`MarginalSettlementAllocator`."""

from __future__ import annotations

import heapq
from typing import Dict, List, Sequence, Tuple

from ._shared import SECONDS_PER_YEAR
from .allocate import AllocateMixin
from .ladder import LadderMathMixin
from .scip import ScipMixin
from .tail import TailBudgetMixin
from .types import (
    SettlementAllocation,
    SettlementCandidate,
    SettlementOptimizationResult,
)


class MarginalSettlementAllocator(LadderMathMixin, TailBudgetMixin, ScipMixin, AllocateMixin):
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
    # Shared result / pruning helpers
    # =========================================================================

    @staticmethod
    def _empty_result(usable: float) -> SettlementOptimizationResult:
        return SettlementOptimizationResult(
            total_allocated_usd=0.0,
            usable_capital_usd=float(usable),
            allocations=[],
            expected_portfolio_profit_usd=0.0,
            equalized_marginal_velocity=0.0,
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
    ) -> SettlementOptimizationResult:
        allocations: List[SettlementAllocation] = []
        for i, cost in allocated.items():
            if cost <= 1e-9:
                continue
            cand = candidates[i]
            total_shs = shares[i]
            level_fills = fills[i]
            fee = cand.fee_for_fills(level_fills)
            gas = cand.gas_usd
            net_profit = cand.p_win * total_shs - cost - fee - gas
            arr = (net_profit / cost) * (SECONDS_PER_YEAR / cand.target_settle_sec) if cost > 0 else 0.0
            eff_px = cost / total_shs if total_shs > 1e-9 else cand.best_ask
            marg_px = level_fills[-1][0] if level_fills else cand.best_ask
            marg_vel = MarginalSettlementAllocator.marginal_velocity(
                marg_px,
                cand.target_settle_sec,
                cand.per_share_fee(marg_px),
                cand.p_win,
                cand.risk_price_per_usd,
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
                    fee_usd=fee,
                    all_in_usd=round(cost + fee, 5),
                    level_fills=level_fills,
                )
            )

        allocations.sort(key=lambda a: a.annualized_arr, reverse=True)
        tot_alloc = sum(a.allocated_usd for a in allocations)
        tot_profit = sum(a.net_profit_usd for a in allocations)
        clearing_velocity = min((a.marginal_velocity for a in allocations), default=0.0)

        return SettlementOptimizationResult(
            total_allocated_usd=round(tot_alloc, 2),
            usable_capital_usd=round(usable, 2),
            allocations=allocations,
            expected_portfolio_profit_usd=round(tot_profit, 4),
            equalized_marginal_velocity=round(clearing_velocity, 4),
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
                px,
                cand.target_settle_sec,
                cand.per_share_fee(px),
                cand.p_win,
                cand.risk_price_per_usd,
            )
            if vel > 0.0:
                heapq.heappush(heap, (-vel, i, 0, px, sz))

        rem = float(budget)
        while heap and rem > 1e-9:
            neg_vel, i, k, px, sz = heapq.heappop(heap)
            cand = candidates[i]
            # The budget is *cash*, and a taker buy debits the price plus the
            # taker fee.  Charging only ``px * sz`` spent more than the treasury
            # (measured: $1,004.19 all-in against $1,000 at a 5 % schedule).
            fee_px = cand.per_share_fee(px)
            unit_all_in = px + fee_px
            level_all_in = unit_all_in * sz
            if level_all_in <= 1e-9:
                continue
            deploy_all_in = min(rem, level_all_in)
            if deploy_all_in <= 1e-9:
                continue

            shs = deploy_all_in / unit_all_in
            deploy_cost = px * shs
            allocated[i] = allocated.get(i, 0.0) + deploy_cost
            shares[i] = shares.get(i, 0.0) + shs
            fills.setdefault(i, []).append((px, shs, deploy_cost))
            rem -= deploy_all_in

            if deploy_all_in < level_all_in - 1e-9:
                # Budget exhausted inside this level.
                break

            next_level = k + 1
            if next_level < len(cand.ask_ladder):
                npx, nsz = cand.ask_ladder[next_level]
                nvel = self.marginal_velocity(
                    npx,
                    cand.target_settle_sec,
                    cand.per_share_fee(npx),
                    cand.p_win,
                    cand.risk_price_per_usd,
                )
                if nvel > 0.0:
                    heapq.heappush(heap, (-nvel, i, next_level, npx, nsz))

        return allocated, shares, fills, rem

    @staticmethod
    def _upsize_venue_minimums(
        candidates: Sequence[SettlementCandidate],
        allocated: Dict[int, float],
        shares: Dict[int, float],
        fills: Dict[int, List[Tuple[float, float, float]]],
        usable: float,
    ) -> bool:
        """Lift sub-minimum tickets to the venue minimum when that is profitable.

        Water-filling sizes by marginal velocity and can leave a market below the
        venue's ``orderMinSize``; the tranche was then pruned and the capital sat
        idle even though the *smallest order the venue accepts* clears the fixed
        redemption gas and the taker fee.  Upsizing spends budget that is
        otherwise unallocated, so a positive-expectancy tranche is never
        discarded merely for being small.  Returns True when anything changed.
        """
        if not allocated:
            return False
        changed = False
        for index, cost in list(allocated.items()):
            cand = candidates[index]
            if cost <= 0.0 or cand.min_order_shares <= 0.0:
                continue
            if shares.get(index, 0.0) + 1e-9 >= cand.min_order_shares:
                continue
            need_cost, need_fills = cand.fills_for_shares(cand.min_order_shares)
            if need_cost <= 0.0:
                continue
            extra = need_cost - cost
            if extra <= 1e-9:
                continue
            # Cash is debited all-in: price plus the taker fee on the whole order.
            committed = sum(
                allocated[j] + candidates[j].fee_for_fills(fills.get(j, []))
                for j in allocated
            )
            extra_all_in = extra + (
                cand.fee_for_fills(need_fills) - cand.fee_for_fills(fills.get(index, []))
            )
            if committed + extra_all_in > usable + 1e-9:
                # Budget is already committed to higher-velocity tranches; the
                # equalised solution wins.
                continue
            if cand.ticket_net_profit(cand.min_order_shares) <= 0.0:
                continue
            allocated[index] = need_cost
            shares[index] = cand.min_order_shares
            fills[index] = need_fills
            changed = True
        return changed

    def optimize_water_filling(
        self,
        candidates: Sequence[SettlementCandidate],
        usable_capital: float,
    ) -> SettlementOptimizationResult:
        """Exact analytical water-filling with fixed-charge / venue-minimum pruning."""
        usable = float(usable_capital)
        if usable <= 0.0 or not candidates:
            return self._empty_result(usable)

        by_cid = {c.condition_id: c for c in candidates}
        # Gas is a fixed charge per market: the smallest order the venue accepts
        # is the least profitable tranche, so a market that cannot clear gas and
        # fees on its minimum ticket is never started.
        active: List[SettlementCandidate] = [c for c in candidates if c.is_fundable()]
        while active:
            allocated, shares, fills, _ = self._water_fill_once(active, usable)
            self._upsize_venue_minimums(active, allocated, shares, fills, usable)
            result = self._finalize(active, allocated, shares, fills, usable)
            dropped = self._invalid_cids(by_cid, result.allocations)
            if not dropped:
                return result
            active = [c for c in active if c.condition_id not in dropped]

        return self._empty_result(usable)

    @staticmethod
    def _profit_per_dollar_day(net_profit_usd: float, all_in_usd: float, settle_sec: float) -> float:
        """Expected profit per dollar of capital per day held."""
        if all_in_usd <= 0.0 or settle_sec <= 0.0:
            return 0.0
        days = settle_sec / 86_400.0
        if days <= 0.0:
            return 0.0
        return (net_profit_usd / all_in_usd) / days
