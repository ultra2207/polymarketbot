"""Opportunistic Market Switching Engine for Polymarket Liquidity Rewards.

Automated state machine that:
1. Periodically scans the market universe (e.g. every 5 minutes / 300s).
2. Calculates exact switching cost:
     gas to unquote M_A + CTF complete-set merge gas + crossing spread friction to enter M_B.
3. Evaluates Switching Hurdle:
     Y_net(M_B) > Y_net(M_A) * (1 + switch_hurdle_pct) + Switching Cost
   (amortized over a 4-hour horizon).
4. Executes atomic transition:
     - Cleanly cancels all resting quotes on M_A
     - Triggers complete-set merge on M_A to unlock 100% USDC cash
     - Migrates quoting to M_B.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from polymarketbot.logs import get_logger
from polymarketbot.market import MarketConfig
from polymarketbot.strategy.reward.scanner import MarketOpportunity, OpportunityScanner

logger = get_logger("market_switching")


class SwitchEngineState(str, Enum):
    IDLE = "IDLE"
    QUOTING = "QUOTING"
    SCANNING = "SCANNING"
    SWITCHING = "SWITCHING"


@dataclass
class SwitchingConfig:
    """Configuration for opportunistic market switching."""

    scan_interval_seconds: float = 300.0  # 5 minutes periodic re-scan
    switch_hurdle_pct: float = 0.20  # M_B must provide >= 20% higher net yield
    amortization_horizon_hours: float = 4.0  # 4-hour amortization window
    unquote_gas_usd: float = 0.0  # Off-chain EIP-712 unquote / cancel on Polygon PoS costs 0 gas
    merge_gas_usd: Optional[float] = None  # Polygon CTF complete-set merge gas fee (dynamic if None)
    crossing_spread_friction_pct: float = 0.0025  # Friction / half-spread crossing into M_B
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
    from_condition_id: Optional[str]
    from_question: str
    to_condition_id: Optional[str]
    to_question: str
    y_net_from: float
    y_net_to: float
    expected_gain_per_day: float
    switching_cost_usd: float
    amortized_cost_per_day: float
    hurdle_threshold: float
    should_switch: bool
    reason: str
    candidate: Optional[MarketOpportunity] = field(default=None, repr=False, compare=False)

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
    merge_gas_usd: Optional[float] = None,
    crossing_spread_friction_pct: float = 0.0025,
) -> float:
    """Calculate the total switching cost:

    Switching Cost = Gas to unquote M_A (0.0 for off-chain EIP-712 cancel) + CTF merge gas + crossing spread friction to enter M_B.
    """
    if merge_gas_usd is None:
        try:
            from polymarketbot.strategy.onchain.ctf import estimate_merge_cost_usd
            m_gas = float(estimate_merge_cost_usd())
        except Exception:
            m_gas = 0.015
    else:
        m_gas = float(merge_gas_usd)

    crossing_cost = allocated_capital * crossing_spread_friction_pct
    return float(unquote_gas_usd) + m_gas + crossing_cost


def evaluate_switching_hurdle(
    y_net_current: float,
    y_net_candidate: float,
    switching_cost: float,
    switch_hurdle_pct: float = 0.20,
    horizon_hours: float = 4.0,
    amortize_costs: bool = True,
) -> tuple[bool, float, str]:
    """Evaluate whether switching to candidate market M_B clears the hurdle:

    Y_net(M_B) > Y_net(M_A) * (1 + switch_hurdle_pct) + Switching Cost

    Args:
        y_net_current: Daily net expected yield of currently quoted market M_A.
        y_net_candidate: Daily net expected yield of candidate market M_B.
        switching_cost: Total frictional cost in USD.
        switch_hurdle_pct: Minimum yield premium (default: 20%).
        horizon_hours: Amortization horizon in hours (default: 4h).
        amortize_costs: Whether to amortize switching costs to a daily rate basis.

    Returns:
        (should_switch, hurdle_threshold, reason)
    """
    if amortize_costs:
        # Convert frictional cost to equivalent daily rate over horizon
        effective_cost = switching_cost * (24.0 / max(0.1, horizon_hours))
    else:
        effective_cost = switching_cost

    base_required = max(0.0, y_net_current) * (1.0 + switch_hurdle_pct)
    hurdle_threshold = base_required + effective_cost

    if y_net_candidate > hurdle_threshold:
        reason = (
            f"candidate yield ${y_net_candidate:.2f}/d clears hurdle ${hurdle_threshold:.2f}/d "
            f"(current: ${y_net_current:.2f}/d + {switch_hurdle_pct*100:.0f}% hurdle + ${effective_cost:.2f}/d amortized cost)"
        )
        return True, hurdle_threshold, reason
    else:
        reason = (
            f"candidate yield ${y_net_candidate:.2f}/d fails hurdle ${hurdle_threshold:.2f}/d "
            f"(current: ${y_net_current:.2f}/d + {switch_hurdle_pct*100:.0f}% hurdle + ${effective_cost:.2f}/d amortized cost)"
        )
        return False, hurdle_threshold, reason


class OpportunisticSwitchingEngine:
    """State machine governing periodic scanning, hurdle evaluation, and capital migration."""

    def __init__(
        self,
        scanner: OpportunityScanner,
        config: Optional[SwitchingConfig] = None,
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
        self.active_opportunity: Optional[MarketOpportunity] = None
        self.last_scan_time: float = 0.0
        self.last_decision: Optional[SwitchDecision] = None
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
        current_yield_fallback: Optional[float] = None,
        exclude_condition_ids: Optional[set[str]] = None,
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

    def check_opportunity_switch(
        self,
        ranked_opportunities: Optional[list[MarketOpportunity]] = None,
        exclude_condition_ids: Optional[set[str]] = None,
    ) -> tuple[bool, Optional[MarketOpportunity], str]:
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

        # Calculate switching cost
        switch_cost = self.effective_switching_cost()
        if self.config.amortize_costs:
            amortized_cost = switch_cost * (
                24.0 / max(0.1, self.config.amortization_horizon_hours)
            )
        else:
            amortized_cost = switch_cost

        # Evaluate hurdle
        should_switch, threshold, reason = evaluate_switching_hurdle(
            y_net_current=active.net_expected_yield,
            y_net_candidate=top_candidate.net_expected_yield,
            switching_cost=switch_cost,
            switch_hurdle_pct=self.config.switch_hurdle_pct,
            horizon_hours=self.config.amortization_horizon_hours,
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
                switching_cost_usd=switch_cost,
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
        engine: PaperTradingEngine,
        client: Optional[Any] = None,
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

        alloc_cap = self.allocated_capital_usd
        cost = calculate_switching_cost(
            allocated_capital=alloc_cap,
            unquote_gas_usd=self.config.unquote_gas_usd,
            merge_gas_usd=self.config.merge_gas_usd,
            crossing_spread_friction_pct=self.config.crossing_spread_friction_pct,
        )

        logger.info(f"================================================================")
        logger.info(f"🔄 EXECUTING MARKET SWITCH: '{old_q[:30]}' -> '{new_market.question[:30]}'")
        logger.info(f"================================================================")

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

        event = SwitchEvent(
            timestamp=time.time(),
            from_condition_id=old_cid,
            from_question=old_q,
            to_condition_id=new_market.condition_id,
            to_question=new_market.question,
            y_net_from=y_from,
            y_net_to=y_to,
            switching_cost=cost,
            hurdle_threshold=y_from * (1.0 + self.config.switch_hurdle_pct) + cost,
            reason=reason,
            unlocked_cash_usd=unlocked_cash,
        )
        self.history.append(event)
        logger.info(f"✅ Market switch complete. Now actively harvesting '{new_market.question[:40]}'.")
        return event
