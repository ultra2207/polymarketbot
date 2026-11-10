"""Continuous real-time closed-loop portfolio governor.

Re-solves the allocator on a cadence and turns the change in the optimal book
into discrete control actions (maintain, resize, switch). The allocator itself
is injected, so the governor never owns the optimiser's state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from polymarketbot.strategy.reward.allocator import LiquidityRewardsAllocator


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
        allocator: Optional["LiquidityRewardsAllocator"] = None,
        hurdle_pct: float = 0.15,
        min_dwell_time_seconds: float = 300.0,
        re_solve_interval_seconds: float = 30.0,
    ) -> None:
        # Imported here, not at module scope: the allocator imports this
        # module to re-export the governor, so a module-level import would
        # be a cycle.
        if allocator is None:
            from polymarketbot.strategy.reward.allocator import (
                LiquidityRewardsAllocator,
            )

            allocator = LiquidityRewardsAllocator()
        self.allocator = allocator
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

