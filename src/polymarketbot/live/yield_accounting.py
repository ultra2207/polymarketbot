"""Reward yield, resting queue value and capital-migration accounting.

Mixed into ``BaseMarketMakingEngine``; reads book and quote state and records
the measured switching friction. No quoting decision is made here.
"""

from __future__ import annotations

from typing import Optional, Tuple

from polymarketbot.logs import get_logger
from polymarketbot.strategy.reward.queue import (
    HurdleDecision,
    LiquidationRoute,
    SwitchingCostDecomposition,
)

logger = get_logger("trading.base")


class YieldMixin:
    """Resting queue value, current daily yield and the switching hurdle."""

    # -------------------------------------------------------------------------
    # Queue Valuation, Switching Hurdle, and Capital Migration
    # -------------------------------------------------------------------------

    def compute_resting_queue_value(self) -> float:
        """Compute the total economic value ($V_queue) of resting FIFO queue priority across all active quotes."""
        total_v = 0.0
        for q in self.active_quotes.values():
            sz = float(q.get("size", 0.0))
            if sz <= 0.0:
                continue
            q_ahead = float(q.get("queue_ahead", 0.0))
            tok = q.get("token_side", "YES")
            b_tok, a_tok = self.get_best_bid_ask(tok)
            if b_tok is None or a_tok is None:
                continue
            mid = (b_tok + a_tok) / 2.0
            px = float(q.get("price", mid))
            spread_c = abs(px - mid) * 100.0
            val = self.migration_engine.queue_model.evaluate_queue_value(
                quote_size=sz,
                queue_ahead=q_ahead,
                half_spread_cents=max(0.1, spread_c),
                token_mid_price=mid,
            )
            total_v += val.net_queue_value_usd
        return round(total_v, 4)

    def get_current_daily_yield(self) -> float:
        """Calculate current daily reward payout ($/day) from the official Q_min score."""
        user_score, _, _ = self.compute_quote_scores()
        if user_score <= 0.0 and self.quote_size > 0.0 and self.rewards_max_spread > 0.0:
            # Expected target quote score for current market. Single-sided estimate only:
            # Q_min already accounts for the complementary side, so do not double it.
            v = self.rewards_max_spread
            s = self.spread_offset_cents
            if s < v:
                user_score = ((v - s) / v) ** 2 * self.quote_size

        comp_score = self.competing_score_override if self.competing_score_override is not None else 0.0
        tot = user_score + comp_score
        share_payout = (user_score / tot) * self.daily_reward_rate if tot > 0 else 0.0
        if self._epoch_samples:
            observed_daily = sum(self._epoch_samples[-1440:])
            return max(share_payout, observed_daily)
        return share_payout

    def compute_switching_cost(
        self,
        daily_yield: Optional[float] = None,
        transit_latency_seconds: float = 4.5,
    ) -> SwitchingCostDecomposition:
        """Compute exact economic switching friction to migrate away from active market."""
        pos = self.get_position(self.condition_id)
        net_q = pos.shares_yes - pos.shares_no

        b_yes, a_yes = self.get_best_bid_ask("YES")
        b_no, a_no = self.get_best_bid_ask("NO")
        if b_yes is None or a_yes is None or a_no is None:
            # Fail closed: without a real book the switching friction is unknown,
            # so report an infinite cost instead of inventing a mid/liquidation.
            logger.debug(
                f"No complete book for {self.condition_id}; switching cost unknown"
            )
            decomp = SwitchingCostDecomposition(
                queue_value_lost_usd=0.0,
                liquidation_friction_usd=0.0,
                gas_fees_usd=0.0,
                opportunity_cost_downtime_usd=0.0,
                total_switching_cost_usd=float("inf"),
                liquidation_route=LiquidationRoute.ZERO_INVENTORY,
            )
            self.latest_switching_cost = decomp
            return decomp
        mid_yes = (b_yes + a_yes) / 2.0

        bid_quote = self.active_quotes.get("BUY_YES")
        ask_quote = self.active_quotes.get("BUY_NO") or self.active_quotes.get("SELL_YES")

        resting_bid_sz = float(bid_quote.get("size", 0.0)) if bid_quote else 0.0
        q_ahead_bid = float(bid_quote.get("queue_ahead", 0.0)) if bid_quote else 0.0

        resting_ask_sz = float(ask_quote.get("size", 0.0)) if ask_quote else 0.0
        q_ahead_ask = float(ask_quote.get("queue_ahead", 0.0)) if ask_quote else 0.0

        y_a = daily_yield if daily_yield is not None else self.get_current_daily_yield()

        decomp = self.migration_engine.compute_switching_cost(
            q_a=net_q,
            p_mid_a=mid_yes,
            p_bid_a=b_yes,
            p_ask_opp_a=a_no,
            resting_bid_size=resting_bid_sz,
            resting_ask_size=resting_ask_sz,
            queue_ahead_bid=q_ahead_bid,
            queue_ahead_ask=q_ahead_ask,
            half_spread_cents_a=self.spread_offset_cents,
            daily_yield_a=y_a,
            has_matched_complete_sets=(pos.complete_sets > 0),
            transit_latency_seconds=transit_latency_seconds,
        )
        self.latest_switching_cost = decomp
        return decomp

    def evaluate_migration_hurdle(
        self,
        candidate_daily_yield: float,
        holding_horizon_hours: float = 4.0,
        daily_yield_a: Optional[float] = None,
    ) -> Tuple[bool, HurdleDecision, SwitchingCostDecomposition]:
        """Evaluate Net Alpha Hurdle: Delta_Yield * Tau >= Cost_switch."""
        y_a = daily_yield_a if daily_yield_a is not None else self.get_current_daily_yield()
        cost = self.compute_switching_cost(daily_yield=y_a)
        decision = self.migration_engine.evaluate_switching_hurdle(
            y_net_a_daily=y_a,
            y_net_b_daily=candidate_daily_yield,
            switching_cost_usd=cost.total_switching_cost_usd,
            holding_horizon_hours=holding_horizon_hours,
        )
        self.latest_hurdle_decision = decision
        return decision.should_switch, decision, cost
