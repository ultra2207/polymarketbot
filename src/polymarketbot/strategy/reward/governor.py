"""Continuous real-time closed-loop portfolio governor.

Re-solves the allocator on a cadence and turns the change in the optimal book
into discrete control actions (maintain, resize, switch). The allocator itself
is injected, so the governor never owns the optimiser's state.

Hysteresis is an economic break-even, not a dwell constant. A cancel/replace
churn forgoes the accrual it would have earned while the order is unquoted
(one measured CLOB round trip out and back plus the venue's 3.5 s eligibility
age). The re-solve that proposes the change earns the *marginal* reward yield
of capital. The change is worth making only once the marginal yield over the
wait has repaid that churn cost:

    cycles_to_repay = churn_cost_fraction / marginal_yield_per_capital_per_cycle

which is exactly ``churn_cost / marginal_yield_per_share_per_cycle`` with both
sides expressed per unit of capital. Both the capital-resize gate and the exit
dwell gate use the same break-even, so no hand-set share of capital and no
hand-set dwell survive.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from polymarketbot.strategy.reward.accrual import REWARD_ELIGIBILITY_SECONDS

if TYPE_CHECKING:
    from polymarketbot.strategy.reward.allocator import LiquidityRewardsAllocator

_SECONDS_PER_DAY = 86_400.0


@dataclass(frozen=True)
class HysteresisBreakEven:
    """Measured break-even for one churn of a resting quote.

    ``churn_cost_fraction`` is the accrual forgone during the unquoted
    interval as a fraction of the capital behind the quote.
    ``marginal_yield_per_capital_per_cycle`` is the reward yield one more unit
    of capital earns over one governor cycle. Their ratio is the number of
    cycles the marginal gain needs to repay the churn, and
    ``min_dwell_seconds`` is that ratio in seconds.
    """

    churn_cost_fraction: float
    marginal_yield_per_capital_per_cycle: float
    min_dwell_seconds: float
    min_resize_fraction: float


def solve_hysteresis(
    *,
    clob_rtt_seconds: float,
    accrual_yield_usd_per_usd_per_day: float,
    marginal_yield_usd_per_usd_per_day: float,
    cycle_seconds: float,
) -> HysteresisBreakEven | None:
    """Break-even churn gate from measured latency, accrual and capital response.

    All four inputs are measurements:

    * ``clob_rtt_seconds`` - one-way CLOB round trip (the same probe the
      placement path pays). A cancel-and-place move pays it twice.
    * ``accrual_yield_usd_per_usd_per_day`` - the reward rate the resting
      capital actually earns, USD per USD per day.
    * ``marginal_yield_usd_per_usd_per_day`` - the KKT price of capital from
      the allocator's own solution: what the next dollar earns there.
    * ``cycle_seconds`` - the re-solve cadence, i.e. the period over which the
      marginal yield is credited.

    Returns ``None`` when the marginal yield is not positive (a churn cannot
    repay itself) or an input is not a measured, finite number. ``None`` means
    "hold the position", never "assume the old 300 s".
    """
    values = (
        clob_rtt_seconds,
        accrual_yield_usd_per_usd_per_day,
        marginal_yield_usd_per_usd_per_day,
        cycle_seconds,
    )
    if not all(isinstance(v, (int, float)) and math.isfinite(float(v)) for v in values):
        return None
    rtt = max(0.0, float(clob_rtt_seconds))
    accrual = max(0.0, float(accrual_yield_usd_per_usd_per_day))
    marginal = float(marginal_yield_usd_per_usd_per_day)
    cycle = float(cycle_seconds)
    if marginal <= 0.0 or cycle <= 0.0:
        return None
    unquoted_seconds = 2.0 * rtt + REWARD_ELIGIBILITY_SECONDS
    churn_fraction = accrual * unquoted_seconds / _SECONDS_PER_DAY
    marginal_per_cycle = marginal * cycle / _SECONDS_PER_DAY
    if marginal_per_cycle <= 0.0:
        return None
    repay_cycles = churn_fraction / marginal_per_cycle
    return HysteresisBreakEven(
        churn_cost_fraction=churn_fraction,
        marginal_yield_per_capital_per_cycle=marginal_per_cycle,
        min_dwell_seconds=repay_cycles * cycle,
        min_resize_fraction=repay_cycles,
    )


@dataclass
class GovernorAction:
    """Discrete action emitted by the continuous optimization governor.

    ``target_capital`` and ``target_quote_size`` are ``None`` when the action
    does not measure a target: a ``MAINTAIN`` for a market the optimizer did
    not fund carries no target size, and inventing one (a fraction of the
    bankroll, or the bankroll itself in a shares field) would state a position
    the allocator never chose. An exit's ``0.0`` is a command, not a
    measurement, and stays ``0.0``.
    """

    action_type: str  # "MAINTAIN", "RESIZE_QUOTE", "SWITCH_MARKET", "MERGE_INVENTORY"
    condition_id: str
    target_capital: float | None
    target_quote_size: float | None
    reason: str
    urgency: str = "NORMAL"  # "NORMAL", "HIGH", "CRITICAL"


class ContinuousOptimizationGovernor:
    """Continuously evaluates market conditions and dynamically adapts quoting allocations in real time.

    Operates in real-time to:
    1. Re-evaluate the optimizer every T seconds or upon a fill/book shift.
    2. Emit discrete control actions: resize a funded market, exit a market the
       optimizer no longer funds (after the dwell gate), or enter a newly funded
       one.

    It applies **no** switching-cost hurdle of its own. That hurdle needs the
    measured cost of the migration (merge gas, crossing friction, the taker fee
    at the destination price) and lives in
    :func:`polymarketbot.strategy.reward.switching.evaluate_switching_hurdle`,
    which the auto-selector path calls.

    Its own hysteresis is solved, not configured: :func:`solve_hysteresis`
    turns the measured CLOB round trip, the measured accrual yield and the
    allocator's own marginal yield into a break-even dwell and resize gate.
    ``min_dwell_time_seconds`` exists only to let a test or a caller pin a
    measured value; when omitted the gate is solved every cycle and an
    unmeasured cycle emits no exit.
    """

    def __init__(
        self,
        allocator: LiquidityRewardsAllocator | None = None,
        min_dwell_time_seconds: float | None = None,
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
        self.min_dwell_time = (
            None if min_dwell_time_seconds is None else float(min_dwell_time_seconds)
        )
        self.re_solve_interval = float(re_solve_interval_seconds)
        #: The break-even solved by the last cycle, or ``None`` when the inputs
        #: were not measured. Recorded so the action log carries the gate it
        #: applied instead of an unexplained 300 s.
        self.last_hysteresis: HysteresisBreakEven | None = None

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
        *,
        clob_rtt_seconds: float | None = None,
        accrual_yield_usd_per_usd_per_day: float | None = None,
    ) -> list[GovernorAction]:
        """Run continuous optimization cycle and generate market-making control actions.

        ``clob_rtt_seconds`` and ``accrual_yield_usd_per_usd_per_day`` are the
        caller's measurements; the marginal yield is read from the allocator's
        own solution (``shadow_yield_floor_usd_per_usd_per_day``). With all
        three present the dwell/resize gate is solved by
        :func:`solve_hysteresis`; otherwise exits and resizes are held and the
        action says the gate is unmeasured. A pinned ``min_dwell_time_seconds``
        overrides the solve.
        """
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

        hysteresis = self.last_hysteresis = self._solve_hysteresis(
            solution, clob_rtt_seconds, accrual_yield_usd_per_usd_per_day
        )
        min_dwell = (
            self.min_dwell_time
            if self.min_dwell_time is not None
            else (None if hysteresis is None else hysteresis.min_dwell_seconds)
        )
        min_resize = None if hysteresis is None else hysteresis.min_resize_fraction
        gate_reason = (
            f"hysteresis solved: {hysteresis.min_dwell_seconds:.0f}s dwell, "
            f"{hysteresis.min_resize_fraction * 100.0:.2f}% resize break-even"
            if hysteresis is not None
            else "hysteresis unmeasured (need CLOB rtt, accrual yield and the "
            "allocator's marginal yield); holding the position"
        )

        # 2. Check existing active markets
        for cid in current_active_markets:
            if cid in target_map:
                alloc = target_map[cid]
                old_cap = self.current_allocations.get(cid, alloc.allocated_capital)
                delta = abs(alloc.allocated_capital - old_cap)
                # The resize gate is the break-even capital change: a smaller
                # move cannot repay its own cancel/replace churn.
                breakeven_capital = (
                    None
                    if min_resize is None
                    else min_resize * max(old_cap, alloc.allocated_capital)
                )
                if breakeven_capital is not None and delta > breakeven_capital:
                    actions.append(
                        GovernorAction(
                            action_type="RESIZE_QUOTE",
                            condition_id=cid,
                            target_capital=alloc.allocated_capital,
                            target_quote_size=alloc.quote_size,
                            reason=(
                                f"Optimizer adjusted target capital from ${old_cap:.1f} to "
                                f"${alloc.allocated_capital:.1f} (${delta:.2f} > "
                                f"${breakeven_capital:.2f} churn break-even)."
                            ),
                        )
                    )
                    self.current_allocations[cid] = alloc.allocated_capital
            else:
                # Market is no longer in optimal allocation -> check the
                # solved dwell gate.
                dwell = current_time - self.market_entry_times.get(cid, current_time)
                if min_dwell is not None and dwell >= min_dwell:
                    actions.append(
                        GovernorAction(
                            action_type="SWITCH_MARKET",
                            condition_id=cid,
                            target_capital=0.0,
                            target_quote_size=0.0,
                            reason=f"Market yield diluted; dwell time {dwell:.0f}s >= {min_dwell:.0f}s break-even. Exiting position.",
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
                # Only the optimizer can state a target for this market. When it
                # did not fund the market (the exit is held by the dwell gate)
                # there is no target, and the action says so instead of
                # inventing ``0.75 x bankroll`` in a shares field.
                alloc = target_map.get(cid)
                actions.append(
                    GovernorAction(
                        action_type="MAINTAIN",
                        condition_id=cid,
                        target_capital=(
                            None if alloc is None else alloc.allocated_capital
                        ),
                        target_quote_size=None if alloc is None else alloc.quote_size,
                        reason=(
                            "Current market making allocation is globally optimal "
                            "within hurdle tolerance."
                            if alloc is not None
                            else "Held at the current size: the optimizer no "
                            "longer funds this market but the dwell gate "
                            f"blocks the exit ({gate_reason})."
                        ),
                    )
                )

        return actions

    def _solve_hysteresis(
        self,
        solution: Any,
        clob_rtt_seconds: float | None,
        accrual_yield_usd_per_usd_per_day: float | None,
    ) -> HysteresisBreakEven | None:
        """The break-even for this cycle, or ``None`` when an input is unmeasured.

        The marginal yield is the allocator's KKT price of capital at this
        cycle's optimum: the measured reward yield one more dollar earns. It is
        not re-derived here, because a second estimate of the same quantity
        would be a second model.
        """
        if clob_rtt_seconds is None or accrual_yield_usd_per_usd_per_day is None:
            return None
        marginal = getattr(
            solution, "shadow_yield_floor_usd_per_usd_per_day", None
        )
        if marginal is None:
            return None
        return solve_hysteresis(
            clob_rtt_seconds=clob_rtt_seconds,
            accrual_yield_usd_per_usd_per_day=accrual_yield_usd_per_usd_per_day,
            marginal_yield_usd_per_usd_per_day=float(marginal),
            cycle_seconds=self.re_solve_interval,
        )

