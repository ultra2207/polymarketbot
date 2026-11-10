"""Reward yield, resting queue value and capital-migration accounting.

Mixed into ``BaseMarketMakingEngine``; reads book and quote state and records
the measured switching friction. No quoting decision is made here.
"""

from __future__ import annotations

from typing import Optional, Tuple

from polymarketbot.logs import get_logger
from polymarketbot.strategy.reward.marginal import PROTOCOL_MIN_DAILY_PAYOUT_USD
from polymarketbot.strategy.reward.queue import (
    HurdleDecision,
    LiquidationRoute,
    SwitchingCostDecomposition,
)
from polymarketbot.strategy.reward.scoring import (
    PROTOCOL_EPOCH_SAMPLES,
    calculate_epoch_payout,
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
        """Current daily reward payout in USD/day, or 0.0 when it is not payable.

        The reward decision is the paper pillar's, not a second opinion:

        * only the **resting** quotes are scored. The old fallback scored the
          configured ``spread_offset_cents`` when nothing qualified, i.e. it
          priced a quote that was not on the book; that number is not a
          measurement and is gone.
        * the published $1.00-per-day venue floor applies, so a sub-floor share
          is reported as ``0.0`` rather than as a small cash amount the engine
          would bank (:func:`calculate_epoch_payout` owns both the
          normalisation and the floor).
        * a missing book or a missing measured competing denominator is
          **unmeasured**, not zero competition. Reading an absent denominator as
          zero pays the whole pool on any size and would attract a quote into a
          book nobody read, so it is refused exactly as
          :func:`polymarketbot.strategy.reward.marginal.evaluate_market_payout`
          refuses it.

        Returns 0.0 in every refused case and logs whether the reading was
        unmeasured (no complete book, no measured denominator) or a measured
        zero (nothing resting, or resting quotes outside the qualifying
        band/minimum size), so the two are never confused in the tape.
        """
        b_yes, a_yes = self.get_best_bid_ask("YES")
        b_no, a_no = self.get_best_bid_ask("NO")
        if b_yes is None or a_yes is None or b_no is None or a_no is None:
            logger.debug(
                f"Reward yield unmeasured for {self.condition_id}: no complete book; "
                "reporting 0.0"
            )
            return 0.0

        measured_competing = self.competing_score_override
        if measured_competing is None or float(measured_competing) <= 0.0:
            logger.debug(
                f"Reward yield unmeasured for {self.condition_id}: no measured competing "
                "score; reporting 0.0"
            )
            return 0.0

        user_score, _, _ = self.compute_quote_scores()
        if user_score <= 0.0:
            resting = [
                q
                for q in self.active_quotes.values()
                if float(q.get("size", 0.0) or 0.0) > 0.0
            ]
            logger.debug(
                f"Reward yield zero for {self.condition_id}: "
                + (
                    "resting quote(s) outside the qualifying band/minimum size; reporting 0.0"
                    if resting
                    else "nothing resting to score; reporting 0.0"
                )
            )
            return 0.0

        # Same normalisation and the same documented daily floor the paper
        # pillar prices with: a market paying under $1.00/day pays nothing.
        _share, _gross_payout, net_payout = calculate_epoch_payout(
            maker_epoch_score=user_score,
            total_epoch_score=user_score + float(measured_competing),
            rate_per_day=self.daily_reward_rate,
        )
        if self._epoch_samples:
            observed_daily = sum(self._epoch_samples[-PROTOCOL_EPOCH_SAMPLES:])
            if observed_daily < PROTOCOL_MIN_DAILY_PAYOUT_USD:
                observed_daily = 0.0
            return max(net_payout, observed_daily)
        return net_payout

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
