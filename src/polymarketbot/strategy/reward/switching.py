"""Opportunistic Market Switching Engine for Polymarket Liquidity Rewards.

Automated state machine that:
1. Periodically scans the market universe (e.g. every 5 minutes / 300s).
2. Calculates exact switching cost:
     gas to unquote M_A + CTF complete-set merge gas + crossing spread friction to enter M_B.
3. Evaluates Switching Hurdle:
     Y_net(M_B) > Y_net(M_A) * (1 + switch_hurdle_pct) + Switching Cost
4. Executes atomic transition:
     - Cleanly cancels all resting quotes on M_A
     - Triggers complete-set merge on M_A to unlock 100% USDC cash
     - Migrates quoting to M_B.

Both hurdle inputs are solved from measurements, not policy:

* ``switch_hurdle_pct = round_trip_cost / capital_moved`` - the one-off cost of
  moving the capital as a fraction of it, where ``round_trip_cost`` is the
  measured crossing friction (taker fee at the candidate + half-spread) plus
  gas plus the yield forgone over the measured unquoted interval.
* ``payback_hours = round_trip_cost / yield_differential_per_hour`` - the
  break-even holding period. It is the horizon the model amortises over when
  the caller has no measured holding horizon; a switch taken at exactly that
  horizon is break-even, so the model rejects it (the premium makes it a loss).
  A caller that *measured* how long it will hold the destination passes that
  horizon instead and the decision is the Net Alpha test.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from polymarketbot.logs import get_logger
from polymarketbot.strategy.reward.accrual import REWARD_ELIGIBILITY_SECONDS
from polymarketbot.strategy.reward.scanner import MarketOpportunity, OpportunityScanner

logger = get_logger("market_switching")


class SwitchEngineState(str, Enum):
    IDLE = "IDLE"
    QUOTING = "QUOTING"
    SCANNING = "SCANNING"
    SWITCHING = "SWITCHING"


@dataclass
class SwitchingConfig:
    """Configuration for opportunistic market switching.

    ``switch_hurdle_pct`` and ``amortization_horizon_hours`` have no defaults:
    both are solved by the caller from measured quantities (see the module
    docstring). ``None`` means "solve it from the measurements in this config",
    never "use a representative 20 % / 4 h".
    """

    scan_interval_seconds: float = 300.0  # 5 minutes periodic re-scan
    switch_hurdle_pct: float | None = None
    # ^ round_trip_cost / capital_moved. None = solve from the measured cost.
    amortization_horizon_hours: float | None = None
    # ^ The measured holding horizon. None = solve the payback from the yield
    #   differential; a switch at exactly the payback is break-even, so the
    #   model rejects it rather than assuming a 4 h hold.
    round_trip_latency_seconds: float | None = None
    # ^ Measured one-way CLOB round trip. It prices the unquoted interval in
    #   the round-trip cost. None = the cost is unmeasured and the engine
    #   refuses to migrate (``calculate_switching_cost`` reports +inf).
    unquote_gas_usd: float = 0.0  # Off-chain EIP-712 unquote / cancel on Polygon PoS costs 0 gas
    merge_gas_usd: float | None = None  # Polygon CTF complete-set merge gas fee (dynamic if None)
    crossing_spread_friction_pct: float | None = None
    # ^ Measured cost of crossing into M_B as a fraction of capital (half the
    #   spread plus the taker fee at the quoted price). None = unmeasured, which
    #   makes the switching cost infinite so no migration is chosen on a guessed
    #   constant; the old default was a hand-set 25 bps.
    amortize_costs: bool = True


@dataclass
class SwitchEvent:
    """Audit record of a market migration execution."""

    timestamp: float
    from_condition_id: str
    from_question: str
    to_condition_id: str
    to_question: str
    y_net_from: float
    y_net_to: float
    switching_cost: float
    hurdle_threshold: float
    reason: str
    unlocked_cash_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "from_condition_id": self.from_condition_id,
            "from_question": self.from_question,
            "to_condition_id": self.to_condition_id,
            "to_question": self.to_question,
            "y_net_from": round(self.y_net_from, 2),
            "y_net_to": round(self.y_net_to, 2),
            "switching_cost": round(self.switching_cost, 4),
            "hurdle_threshold": round(self.hurdle_threshold, 2),
            "reason": self.reason,
            "unlocked_cash_usd": round(self.unlocked_cash_usd, 2),
        }


@dataclass
class SwitchDecision:
    """Outcome of one re-scan: hold the active market or migrate to a candidate."""

    timestamp: float
    from_condition_id: str | None
    from_question: str
    to_condition_id: str | None
    to_question: str
    y_net_from: float
    y_net_to: float
    expected_gain_per_day: float
    switching_cost_usd: float
    amortized_cost_per_day: float
    hurdle_threshold: float
    should_switch: bool
    reason: str
    candidate: MarketOpportunity | None = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "from_condition_id": self.from_condition_id,
            "from_question": self.from_question,
            "to_condition_id": self.to_condition_id,
            "to_question": self.to_question,
            "y_net_from": round(self.y_net_from, 2),
            "y_net_to": round(self.y_net_to, 2),
            "expected_gain_per_day": round(self.expected_gain_per_day, 2),
            "switching_cost_usd": round(self.switching_cost_usd, 4),
            "amortized_cost_per_day": round(self.amortized_cost_per_day, 4),
            "hurdle_threshold": round(self.hurdle_threshold, 2),
            "should_switch": self.should_switch,
            "reason": self.reason,
        }


def calculate_switching_cost(
    allocated_capital: float,
    unquote_gas_usd: float = 0.0,
    merge_gas_usd: float | None = None,
    crossing_spread_friction_pct: float | None = None,
) -> float:
    """Calculate the total switching cost:

    Switching Cost = Gas to unquote M_A (0.0 for off-chain EIP-712 cancel) + CTF merge gas + crossing spread friction to enter M_B.

    ``crossing_spread_friction_pct`` is a measurement, not a policy knob: it is
    the cost of converting cash into the candidate's position, i.e. half the
    candidate's measured spread plus the taker fee at the quoted price, as a
    fraction of the capital moved. It therefore has no default. The old 0.0025
    (25 bps) was a hand-set number that priced every crossing as if it were
    cheaper than the venue's own fee curve on a 1c spread, and switching
    *toward* a market is exactly where an understated cost loses money.

    Returns ``float("inf")`` when either cost component is unmeasured, so the
    caller never migrates on a guessed fee.
    """
    if crossing_spread_friction_pct is None:
        logger.warning(
            "switching cost unknown: crossing friction is unmeasured; reporting "
            "+inf so no migration is chosen on a guessed cost"
        )
        return float("inf")
    if merge_gas_usd is None:
        from polymarketbot.strategy.onchain.ctf import estimate_merge_cost_usd

        measured = estimate_merge_cost_usd()
        if measured is None:
            # An unpriced merge is not a cheap merge. Report an infinite
            # switching cost so the candidate is never preferred, instead of
            # substituting the $0.015 constant this used to bury here.
            logger.warning(
                "switching cost unknown: merge gas unmeasured; reporting +inf so no "
                "migration is chosen on a guessed fee"
            )
            return float("inf")
        m_gas = float(measured)
    else:
        m_gas = float(merge_gas_usd)

    crossing_cost = float(allocated_capital) * float(crossing_spread_friction_pct)
    return float(unquote_gas_usd) + m_gas + crossing_cost


def evaluate_switching_hurdle(
    y_net_current: float,
    y_net_candidate: float,
    switching_cost: float,
    switch_hurdle_pct: float,
    horizon_hours: float,
    amortize_costs: bool = True,
) -> tuple[bool, float, str]:
    """Evaluate whether switching to candidate market M_B clears the hurdle:

    Y_net(M_B) > Y_net(M_A) * (1 + switch_hurdle_pct) + Switching Cost

    Args:
        y_net_current: Daily net expected yield of currently quoted market M_A.
        y_net_candidate: Daily net expected yield of candidate market M_B.
        switching_cost: Total frictional cost in USD.
        switch_hurdle_pct: Minimum yield premium as a fraction of the current
            yield. Required: solve it as ``round_trip_cost / capital_moved``.
        horizon_hours: Horizon the one-off cost is amortised over. Required:
            either the caller's measured holding horizon or the solved
            ``payback_hours``.
        amortize_costs: Whether to amortize switching costs to a daily rate basis.

    Returns:
        (should_switch, hurdle_threshold, reason)
    """
    if amortize_costs:
        # Convert frictional cost to equivalent daily rate over horizon
        effective_cost = switching_cost * (24.0 / max(1e-9, horizon_hours))
    else:
        effective_cost = switching_cost

    base_required = max(0.0, y_net_current) * (1.0 + switch_hurdle_pct)
    hurdle_threshold = base_required + effective_cost

    if y_net_candidate > hurdle_threshold:
        reason = (
            f"candidate yield ${y_net_candidate:.2f}/d clears hurdle ${hurdle_threshold:.2f}/d "
            f"(current: ${y_net_current:.2f}/d + {switch_hurdle_pct*100:.3f}% hurdle + ${effective_cost:.2f}/d amortized cost)"
        )
        return True, hurdle_threshold, reason
    else:
        reason = (
            f"candidate yield ${y_net_candidate:.2f}/d fails hurdle ${hurdle_threshold:.2f}/d "
            f"(current: ${y_net_current:.2f}/d + {switch_hurdle_pct*100:.3f}% hurdle + ${effective_cost:.2f}/d amortized cost)"
        )
        return False, hurdle_threshold, reason


def solve_switching_hurdle_inputs(
    *,
    switching_cost: float,
    capital_moved: float,
    yield_now_daily: float,
    yield_candidate_daily: float,
    holding_horizon_hours: float | None,
    unquoted_seconds: float | None,
) -> tuple[float, float, float]:
    """Solve ``(hurdle_pct, horizon_hours, round_trip_cost)`` from measurements.

    ``round_trip_cost`` is the module's measured switching cost plus the yield
    forgone while the capital is unquoted: the measured unquoted interval
    (``unquoted_seconds``, two CLOB flights plus the venue's 3.5 s eligibility
    age) times the current yield. The hurdle is that cost per dollar moved.

    ``payback_hours`` is ``round_trip_cost / yield_differential_per_hour`` where
    the differential is measured over one day. When the caller has no measured
    holding horizon, the payback is the horizon the model amortises over; at
    exactly that horizon the amortised cost equals the differential, so the
    model rejects the switch (the premium makes it a loss). A caller that
    measured how long it will hold the destination passes that horizon and the
    decision becomes the Net Alpha test.

    Every component is required to be measured: a negative or non-finite input
    yields ``inf`` for the cost so no migration is chosen on a guessed number.
    """
    capital = float(capital_moved)
    cost = float(switching_cost)
    if (
        not (capital > 0.0)
        or not math.isfinite(float(yield_now_daily))
        or unquoted_seconds is None
        or not (float(unquoted_seconds) >= 0.0)
    ):
        return float("inf"), float("inf"), float("inf")
    unquoted_yield = max(0.0, float(yield_now_daily)) * float(unquoted_seconds) / 86_400.0
    round_trip = cost + unquoted_yield
    hurdle = round_trip / capital
    delta_daily = float(yield_candidate_daily) - float(yield_now_daily)
    if holding_horizon_hours is not None:
        horizon = float(holding_horizon_hours)
    elif delta_daily > 0.0:
        horizon = round_trip / (delta_daily / 24.0)
    else:
        horizon = float("inf")
    return hurdle, horizon, round_trip


class OpportunisticSwitchingEngine:
    """State machine governing periodic scanning, hurdle evaluation, and capital migration."""

    def __init__(
        self,
        scanner: OpportunityScanner,
        config: SwitchingConfig | None = None,
        *,
        bankroll_usd: float,
        concentration_pct: float,
    ) -> None:
        """Wire the engine to a scanner and the caller's own capital split.

        ``bankroll_usd`` and ``concentration_pct`` are the caller's numbers and
        have no defaults: the engine reports yield on the capital the caller is
        prepared to deploy in one market, and an engine that invented a $300
        bankroll at 75 % concentration was reporting 75 % of a bankroll nobody
        had chosen. The scanner is told the resulting dollar amount, so the
        concentration decision is made once, here, instead of inside a scanner
        default.
        """
        if bankroll_usd is None or float(bankroll_usd) <= 0.0:
            raise ValueError(
                f"bankroll_usd must be > 0, got {bankroll_usd!r}: capital "
                "allocation has no default"
            )
        if concentration_pct is None or not 0.0 < float(concentration_pct) <= 1.0:
            raise ValueError(
                f"concentration_pct must be in (0, 1], got {concentration_pct!r}"
            )
        self.scanner = scanner
        self.config = config or SwitchingConfig()
        self.bankroll_usd = float(bankroll_usd)
        self.concentration_pct = float(concentration_pct)
        self.allocated_capital_usd = self.bankroll_usd * self.concentration_pct
        self.state = SwitchEngineState.IDLE
        self.active_opportunity: MarketOpportunity | None = None
        self.last_scan_time: float = 0.0
        self.last_decision: SwitchDecision | None = None
        self.history: list[SwitchEvent] = []

    def set_active_market(self, opp: MarketOpportunity) -> None:
        """Set the currently quoted active market opportunity."""
        self.active_opportunity = opp
        self.state = SwitchEngineState.QUOTING
        logger.info(f"Active market set: '{opp.question[:40]}' (yield: ${opp.net_expected_yield:.2f}/d)")

    def refresh_active_market(self, opp: MarketOpportunity) -> None:
        """Update the active market's measured metrics without re-announcing it."""
        self.active_opportunity = opp
        self.state = SwitchEngineState.QUOTING

    @property
    def quote_horizon_s(self) -> float:
        """How long one quote rests before the next scan replaces it.

        The engine rescans every ``config.scan_interval_seconds`` and requotes
        then, so that caller-set interval - not an invented lifetime - is the
        horizon the measured fill table must be read at.
        """
        return float(self.config.scan_interval_seconds)

    def effective_switching_cost(self) -> float:
        """Total switching cost in USD for the capital currently allocated."""
        return calculate_switching_cost(
            allocated_capital=self.allocated_capital_usd,
            unquote_gas_usd=self.config.unquote_gas_usd,
            merge_gas_usd=self.config.merge_gas_usd,
            crossing_spread_friction_pct=self.config.crossing_spread_friction_pct,
        )

    def _record_decision(self, decision: SwitchDecision) -> None:
        self.last_decision = decision
        if decision.should_switch:
            logger.info(f"🚀 Switching hurdle satisfied: {decision.reason}")
        else:
            logger.info(f"⏳ Switching hurdle not met: {decision.reason}")

    def rescan_and_evaluate(
        self,
        current_yield_fallback: float | None = None,
        exclude_condition_ids: set[str] | None = None,
    ) -> SwitchDecision:
        """Re-scan the rewards universe and evaluate a migration from the active market.

        When the active market is still listed, its scanner-measured yield is
        refreshed so both sides of the comparison come from the same scan.
        ``current_yield_fallback`` (the runner's live measured yield) is used
        only when the active market has dropped out of the rewards universe.
        """
        ranked = self.scanner.scan_and_rank_markets(
            allocated_capital_usd=self.allocated_capital_usd,
            quote_horizon_s=self.quote_horizon_s,
        )
        active = self.active_opportunity
        if active is not None:
            measured = next(
                (opp for opp in ranked if opp.condition_id == active.condition_id),
                None,
            )
            if measured is not None:
                self.refresh_active_market(measured)
            elif current_yield_fallback is not None:
                active.net_expected_yield = float(current_yield_fallback)
                self.state = SwitchEngineState.QUOTING
        self.check_opportunity_switch(ranked, exclude_condition_ids=exclude_condition_ids)
        assert self.last_decision is not None
        return self.last_decision

    def _resolved_hurdle(
        self, *, y_now: float, y_candidate: float
    ) -> tuple[float, float, float]:
        """The measured ``(hurdle_pct, horizon_hours, round_trip_cost)``.

        ``round_trip_cost`` prices the measured crossing friction and gas (the
        module's own switching cost) plus the yield forgone while unquoted:
        two measured CLOB flights and the venue's 3.5 s eligibility age. A
        missing latency makes the cost infinite, so no migration is chosen on
        a guessed flight time. A caller that pinned ``switch_hurdle_pct`` or
        ``amortization_horizon_hours`` overrides the solved value; nothing here
        invents one.
        """
        rtt = self.config.round_trip_latency_seconds
        unquoted = None if rtt is None else 2.0 * float(rtt) + REWARD_ELIGIBILITY_SECONDS
        hurdle, horizon, round_trip = solve_switching_hurdle_inputs(
            switching_cost=self.effective_switching_cost(),
            capital_moved=self.allocated_capital_usd,
            yield_now_daily=float(y_now),
            yield_candidate_daily=float(y_candidate),
            holding_horizon_hours=self.config.amortization_horizon_hours,
            unquoted_seconds=unquoted,
        )
        if self.config.switch_hurdle_pct is not None:
            hurdle = float(self.config.switch_hurdle_pct)
        return hurdle, horizon, round_trip

    def check_opportunity_switch(
        self,
        ranked_opportunities: list[MarketOpportunity] | None = None,
        exclude_condition_ids: set[str] | None = None,
    ) -> tuple[bool, MarketOpportunity | None, str]:
        """Scan opportunities and evaluate if top candidate exceeds switching hurdle against active market.

        ``exclude_condition_ids`` drops candidates the caller already refused
        (for example a neg-risk market a live engine cannot settle) so the next
        best market is considered instead of stalling on the same top entry.
        """
        self.state = SwitchEngineState.SCANNING
        now = time.time()
        self.last_scan_time = now

        if ranked_opportunities is None:
            ranked_opportunities = self.scanner.scan_and_rank_markets(
                allocated_capital_usd=self.allocated_capital_usd,
                quote_horizon_s=self.quote_horizon_s,
            )

        if exclude_condition_ids:
            ranked_opportunities = [
                opp
                for opp in ranked_opportunities
                if opp.condition_id not in exclude_condition_ids
            ]

        active = self.active_opportunity
        if not ranked_opportunities:
            self.state = SwitchEngineState.QUOTING if active else SwitchEngineState.IDLE
            reason = "No active reward market opportunities found."
            self._record_decision(
                SwitchDecision(
                    timestamp=now,
                    from_condition_id=active.condition_id if active else None,
                    from_question=active.question if active else "",
                    to_condition_id=None,
                    to_question="",
                    y_net_from=active.net_expected_yield if active else 0.0,
                    y_net_to=0.0,
                    expected_gain_per_day=0.0,
                    switching_cost_usd=0.0,
                    amortized_cost_per_day=0.0,
                    hurdle_threshold=0.0,
                    should_switch=False,
                    reason=reason,
                    candidate=None,
                )
            )
            return False, None, reason

        top_candidate = ranked_opportunities[0]

        # If no market is currently quoted, adopt the top candidate immediately
        if active is None:
            self.state = SwitchEngineState.IDLE
            reason = "Initial market selection"
            self._record_decision(
                SwitchDecision(
                    timestamp=now,
                    from_condition_id=None,
                    from_question="",
                    to_condition_id=top_candidate.condition_id,
                    to_question=top_candidate.question,
                    y_net_from=0.0,
                    y_net_to=top_candidate.net_expected_yield,
                    expected_gain_per_day=top_candidate.net_expected_yield,
                    switching_cost_usd=0.0,
                    amortized_cost_per_day=0.0,
                    hurdle_threshold=0.0,
                    should_switch=True,
                    reason=reason,
                    candidate=top_candidate,
                )
            )
            return True, top_candidate, reason

        # If top candidate is the same market we are already quoting
        if top_candidate.condition_id == active.condition_id:
            self.refresh_active_market(top_candidate)
            reason = "Current market remains #1 opportunity"
            self._record_decision(
                SwitchDecision(
                    timestamp=now,
                    from_condition_id=active.condition_id,
                    from_question=active.question,
                    to_condition_id=top_candidate.condition_id,
                    to_question=top_candidate.question,
                    y_net_from=active.net_expected_yield,
                    y_net_to=top_candidate.net_expected_yield,
                    expected_gain_per_day=0.0,
                    switching_cost_usd=0.0,
                    amortized_cost_per_day=0.0,
                    hurdle_threshold=0.0,
                    should_switch=False,
                    reason=reason,
                    candidate=top_candidate,
                )
            )
            return False, top_candidate, reason

        # Calculate switching cost and solve both hurdle inputs from measured
        # quantities. No 20 % premium and no 4 h horizon: the premium is the
        # round-trip cost per dollar moved, and the horizon is either the
        # caller's measured holding horizon or the solved payback.
        hurdle_pct, horizon, round_trip_cost = self._resolved_hurdle(
            y_now=active.net_expected_yield,
            y_candidate=top_candidate.net_expected_yield,
        )
        if self.config.amortize_costs and math.isfinite(horizon) and horizon > 0.0:
            amortized_cost = round_trip_cost * (24.0 / horizon)
        else:
            amortized_cost = round_trip_cost

        # Evaluate hurdle
        should_switch, threshold, reason = evaluate_switching_hurdle(
            y_net_current=active.net_expected_yield,
            y_net_candidate=top_candidate.net_expected_yield,
            switching_cost=round_trip_cost,
            switch_hurdle_pct=hurdle_pct,
            horizon_hours=horizon,
            amortize_costs=self.config.amortize_costs,
        )

        self.state = SwitchEngineState.QUOTING
        self._record_decision(
            SwitchDecision(
                timestamp=now,
                from_condition_id=active.condition_id,
                from_question=active.question,
                to_condition_id=top_candidate.condition_id,
                to_question=top_candidate.question,
                y_net_from=active.net_expected_yield,
                y_net_to=top_candidate.net_expected_yield,
                expected_gain_per_day=top_candidate.net_expected_yield - active.net_expected_yield,
                switching_cost_usd=round_trip_cost,
                amortized_cost_per_day=amortized_cost,
                hurdle_threshold=threshold,
                should_switch=should_switch,
                reason=reason,
                candidate=top_candidate,
            )
        )
        return should_switch, top_candidate, reason

    def execute_market_switch(
        self,
        new_market: MarketOpportunity,
        engine: Any,
        client: Any | None = None,
        reason: str = "Hurdle cleared",
    ) -> SwitchEvent:
        """Execute atomic market migration:

        1. Cleanly cancel all resting quotes on M_A
        2. Trigger complete-set merge on M_A to unlock 100% USDC cash
        3. Migrate quoting to M_B.
        """
        self.state = SwitchEngineState.SWITCHING
        old_opp = self.active_opportunity
        old_cid = old_opp.condition_id if old_opp else "none"
        old_q = old_opp.question if old_opp else "None"
        y_from = old_opp.net_expected_yield if old_opp else 0.0
        y_to = new_market.net_expected_yield

        # The audit record uses exactly the cost and hurdle inputs the decision
        # path solves; the old literal 4 h horizon recorded a threshold the
        # decision never used.
        hurdle_pct, horizon, cost = self._resolved_hurdle(
            y_now=y_from, y_candidate=y_to
        )

        logger.info("================================================================")
        logger.info(f"🔄 EXECUTING MARKET SWITCH: '{old_q[:30]}' -> '{new_market.question[:30]}'")
        logger.info("================================================================")

        unlocked_cash = 0.0
        # 1. Cleanly cancel all resting quotes on old market
        if old_opp and old_cid in engine.markets:
            logger.info(f"Step 1: Cancelling all resting quotes on {old_cid[:12]}...")
            engine.cancel_quotes_for_condition(old_cid)

            # 2. Trigger complete-set merge on old market to unlock cash
            logger.info(f"Step 2: Merging complete sets on {old_cid[:12]} to unlock cash...")
            merge_evt = engine.execute_merge(old_cid)
            if merge_evt:
                unlocked_cash = merge_evt.usdc_unlocked
                logger.info(f"Unlocked ${unlocked_cash:.2f} USDC cash from complete sets.")

        # 3. Register new market M_B in engine
        logger.info(f"Step 3: Registering target market {new_market.condition_id[:12]}...")
        mkt_cfg = new_market.to_market_config()
        engine.register_market(mkt_cfg)

        # 4. Synchronize client tokens & fetch book if live client present
        if client is not None:
            logger.info("Step 4: Synchronizing live client subscriptions & order books...")
            if hasattr(client, "switch_active_tokens"):
                client.switch_active_tokens(mkt_cfg.tokens)
            else:
                for tok in mkt_cfg.tokens:
                    if tok.token_id not in client.subscribed_token_ids:
                        client.subscribed_token_ids.append(tok.token_id)
            if hasattr(client, "fetch_initial_order_books"):
                client.fetch_initial_order_books()

        # 5. Generate initial quotes on new market
        for tok in mkt_cfg.tokens:
            engine.generate_quotes(tok.token_id)

        # 6. Update state
        self.active_opportunity = new_market
        self.state = SwitchEngineState.QUOTING

        # The recorded threshold is the one the decision was judged against: the
        # same function, the same solved hurdle and horizon.
        _switch, threshold, _reason = evaluate_switching_hurdle(
            y_net_current=y_from,
            y_net_candidate=y_to,
            switching_cost=cost,
            switch_hurdle_pct=hurdle_pct,
            horizon_hours=horizon,
            amortize_costs=self.config.amortize_costs,
        )
        event = SwitchEvent(
            timestamp=time.time(),
            from_condition_id=old_cid,
            from_question=old_q,
            to_condition_id=new_market.condition_id,
            to_question=new_market.question,
            y_net_from=y_from,
            y_net_to=y_to,
            switching_cost=cost,
            hurdle_threshold=threshold,
            reason=reason,
            unlocked_cash_usd=unlocked_cash,
        )
        self.history.append(event)
        logger.info(f"✅ Market switch complete. Now actively harvesting '{new_market.question[:40]}'.")
        return event
