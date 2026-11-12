"""The paper engine's measured per-order quote size for the shared risk gate.

Live's aggregate engine derives ``RiskConfig.max_quote_size_shares`` from the
sizer's measured quote size (``live/engine/trading.py``): the cap *is* the size
the sizer decided, so a single quote can never exceed what the measured inputs
support, and it is the fallback position-cost cap only when no measured size
exists. The paper engine has no separate sizer call, so its measurement is the
reward planner's own output: the share count of the largest leg the planner
intends to rest this tick. :func:`record` observes that plan once per tick and
publishes the observed size onto the same shared ``RiskConfig`` object the gate
reads, before any quote from the plan is validated.

Statistic
---------
``max planned leg``: the largest ``RewardQuote.shares`` in this tick's plan,
with the number of planned legs, the distinct conditions covered, the
observation timestamp, and a cumulative observation count. The plan is one leg
per outcome of a matched pair, so this is the plan's **per-order** size, not the
pair's total notional.

Policy
------
Re-measured every tick, non-monotonic: the gate must price the plan it is about
to validate, so a smaller plan lowers the cap and a larger plan raises it back.
An empty plan is **not** an observation - no size is invented and the last
measured cap stands. Until the first plan is observed the cap keeps whatever
the shared ``RiskConfig`` was built with, which is the documented live fallback
(``paper/risk.py::build_manager``).

What this module deliberately does not do
-----------------------------------------
* It does not touch the shared position / cost / inventory caps. This is the
  per-order cap only; ``max_position_shares_per_token`` and the cost headroom
  keep clamping the approved size in ``RiskManager.validate_quote``.
* It never observes resting state or a quote the risk gate refused: the input
  is the planner output, so an order that was never intended cannot raise the
  cap. The de-skew cross is likewise not an observation; it is a taker exit,
  not a planned maker size.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from polymarketbot.logs import get_logger
from polymarketbot.strategy.reward.quote_plan import RewardQuote

log = get_logger(__name__)

#: Printed with every applied size so the cap's provenance is auditable.
SOURCE = "reward planner plan (max planned leg)"


@dataclass(frozen=True)
class SizedPlan:
    """One tick's measured per-order quote size and the evidence behind it."""

    shares: float
    """The plan's largest leg: the per-order share cap for this tick."""
    legs: int
    """Planned legs with a positive size (one per outcome of each pair)."""
    conditions: int
    """Distinct conditions the sized legs belong to."""
    ts: float
    """Epoch seconds of the observation."""
    observations: int
    """Cumulative plans observed on this engine, including this one."""
    source: str = SOURCE

    def describe(self) -> str:
        stamp = datetime.fromtimestamp(self.ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return (
            f"max of {self.legs} planned leg(s) over {self.conditions} "
            f"condition(s), sample {self.observations} (plan @ {stamp}), "
            f"source: {self.source}"
        )


@dataclass
class QuoteSizer:
    """The engine's observer of planned quote sizes, created on first use."""

    source: str = SOURCE
    observations: int = 0
    last: SizedPlan | None = None

    def observe(
        self, quotes: Iterable[RewardQuote], *, now: float
    ) -> SizedPlan | None:
        """Measure one tick's plan, or ``None`` when the plan is empty.

        A zero-share quote is not a size: the planner uses it for a leg that
        carries no capital, so it is excluded rather than dragged in as a
        sample. If nothing positive remains there is no measurement.
        """
        planned = [
            (quote.condition_id, float(quote.shares))
            for quote in quotes
            if float(quote.shares) > 0.0
        ]
        if not planned:
            return None
        self.observations += 1
        plan = SizedPlan(
            shares=max(shares for _, shares in planned),
            legs=len(planned),
            conditions=len({condition_id for condition_id, _ in planned}),
            ts=float(now),
            observations=self.observations,
            source=self.source,
        )
        self.last = plan
        return plan


def sizer(engine) -> QuoteSizer:
    """The engine's quote-size observer, created on first use."""
    observer = getattr(engine, "_quote_sizer", None)
    if observer is None:
        observer = QuoteSizer()
        engine._quote_sizer = observer
    return observer


def publish(engine, plan: SizedPlan) -> float:
    """Write ``plan.shares`` onto the shared ``RiskConfig.max_quote_size_shares``.

    The config object is the one the risk manager already holds, so the gate
    sees the measured value on its next ``validate_quote``. Returns the applied
    cap; ``0.0`` means there was no risk manager to publish to (a non-engine
    caller's test double) and the observation only lives on the sizer.
    """
    rm = getattr(engine, "risk_manager", None)
    config = getattr(rm, "config", None)
    new = float(plan.shares)
    if config is None or new <= 0.0:
        return 0.0
    old = float(getattr(config, "max_quote_size_shares", 0.0) or 0.0)
    config.max_quote_size_shares = new
    if abs(old - new) <= 1e-9:
        log.debug(
            f"paper sizer: max_quote_size_shares held at {new:.2f} shares "
            f"({plan.describe()})"
        )
    else:
        log.info(
            f"paper sizer: max_quote_size_shares {old:.2f} -> {new:.2f} shares "
            f"({plan.describe()})"
        )
    return new


def record(engine, quotes: Iterable[RewardQuote], *, now: float) -> SizedPlan | None:
    """Observe this tick's planned quotes and apply the measured per-order cap.

    Called once per reward tick, after the planner has produced the plan and
    before any quote in it reaches the risk gate, so the gate prices the plan
    it is validating rather than the previous tick's size.
    """
    plan = sizer(engine).observe(quotes, now=now)
    if plan is None:
        log.debug("paper sizer: empty plan, no size observed; per-order cap held")
        return None
    publish(engine, plan)
    return plan
