"""Reward yield, resting queue value and capital-migration accounting.

Mixed into ``BaseMarketMakingEngine``; reads book and quote state and records
the measured switching friction. No quoting decision is made here.
"""

from __future__ import annotations

from datetime import UTC, datetime

from polymarketbot.logs import get_logger
from polymarketbot.strategy.reward.accrual import RewardDayAccrual
from polymarketbot.strategy.reward.queue import (
    HurdleDecision,
    LiquidationRoute,
    SwitchingCostDecomposition,
)
from polymarketbot.strategy.reward.scoring import calculate_epoch_payout

logger = get_logger("trading.base")


class YieldMixin:
    """Resting queue value, current daily yield and the switching hurdle."""

    # -------------------------------------------------------------------------
    # Queue Valuation, Switching Hurdle, and Capital Migration
    # -------------------------------------------------------------------------

    def compute_resting_queue_value(self) -> float:
        """Compute the total economic value ($V_queue) of resting FIFO queue priority across all active quotes.

        Without a migration engine there is no queue model, so no queue value
        is measured. Reporting ``0.0`` is a refusal (there is nothing to
        price), logged once rather than on every telemetry cycle.
        """
        if self.migration_engine is None:
            if not getattr(self, "_queue_value_model_missing_logged", False):
                logger.warning(
                    f"Resting queue value unmeasured for {self.condition_id}: "
                    "no migration engine/queue model; reporting 0.0"
                )
                self._queue_value_model_missing_logged = True
            return 0.0
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
        * the published $1.00-per-day venue floor applies per market **per UTC
          day**, at payout time. Completed days are settled by
          :meth:`settle_completed_reward_days` from ``process_reward_epoch``
          and startup only - never as a side effect of this read. A sub-floor
          day therefore reports ``0.0`` here until it is settled rather than a
          partial cash amount the engine would bank
          (:func:`calculate_epoch_payout` owns the spot estimate's
          normalisation and its own daily floor).
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
        # Settlement happens in ``process_reward_epoch``/startup, never as a
        # side effect of this telemetry read. Report the best of the spot
        # estimate and the last amount actually settled.
        cached_last_settled = float(
            getattr(self, "_last_settled_reward_daily_usd", 0.0)
        )
        return max(net_payout, cached_last_settled)

    def reward_day_accrual(self) -> RewardDayAccrual:
        """The per-UTC-day reward accumulator this market settles against.

        Fed by ``RewardDayAccrual.record(ts, usd_per_day)`` from the reward
        sampler. Created lazily so an engine that has not been wired to record
        yet still reports a measured zero rather than an unfloored sum.
        """
        accrual = getattr(self, "_reward_day_accrual", None)
        if accrual is None:
            accrual = RewardDayAccrual(
                condition_id=str(getattr(self, "condition_id", ""))
            )
            self._reward_day_accrual = accrual
        return accrual

    def settle_completed_reward_days(self, now: datetime | None = None) -> float:
        """Settle every UTC day that rolled over; return the latest floored payout.

        The venue pays this market's rewards per UTC day and only when that
        day's accrual reaches $1.00, so a sub-floor day settles to ``0.0``
        instead of a partial payment. Today is still accruing and is never
        settled here.
        """
        accrual = self.reward_day_accrual()
        today = (now or datetime.now(UTC)).date()
        settled = getattr(self, "_last_settled_reward_daily_usd", 0.0)
        for day in accrual.pending_days():
            if day < today:
                settled = accrual.settle_utc_day(day)
        self._last_settled_reward_daily_usd = settled
        return settled

    def compute_switching_cost(
        self,
        daily_yield: float | None = None,
        transit_latency_seconds: float | None = None,
    ) -> SwitchingCostDecomposition:
        """Compute exact economic switching friction to migrate away from active market.

        ``transit_latency_seconds`` is the downtime a migration pays: the capital
        cannot quote while the move is in flight. It has no default — the
        measured CLOB round trip (the same probe every venue action pays) is
        used, and an unmeasured flight makes the cost infinite instead of
        pricing the downtime at 4.5 s.
        """
        pos = self.get_position(self.condition_id)
        net_q = pos.shares_yes - pos.shares_no
        flight_s = (
            float(transit_latency_seconds)
            if transit_latency_seconds is not None
            else self._measured_clob_rtt_s()
        )

        b_yes, a_yes = self.get_best_bid_ask("YES")
        _b_no, a_no = self.get_best_bid_ask("NO")
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

        try:
            fee_schedule = getattr(self, "fee_schedule", None)
            if fee_schedule is None:
                raise ValueError(
                    "measured fee schedule unavailable; refusing to price the "
                    "taker exit as free"
                )
            if flight_s is None:
                raise ValueError(
                    "transit latency unmeasured (no CLOB round trip yet); refusing "
                    "to price the migration downtime as free"
                )
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
                taker_fee_rate=float(fee_schedule.rate),
                has_matched_complete_sets=(pos.complete_sets > 0),
                transit_latency_seconds=float(flight_s),
            )
        except Exception as exc:  # noqa: BLE001 - unmeasured fee/gas is a refusal
            # Fail closed, exactly as the no-book branch does: an unpriced merge
            # makes the switching friction unknown, so report an infinite cost
            # instead of substituting a gas number.
            logger.warning(
                f"Switching cost unknown for {self.condition_id}: {exc}"
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

    def _holding_horizon_hours(self) -> float:
        """Amortisation horizon: how long this engine's quotes actually rest.

        The reprice cadence is the measured lifetime of one quote
        (``min_reprice_interval_s``, shared with the microstructure gate), so a
        cost amortised over it is amortised over the period the capital is
        committed. There is no literal fallback: an engine without the cadence
        refuses instead of amortising over an invented 4 h.
        """
        cadence_s = float(getattr(self, "min_reprice_interval_s", 0.0) or 0.0)
        if cadence_s <= 0.0:
            raise ValueError(
                "quote reprice cadence is unset; cannot amortise the switching cost"
            )
        return cadence_s / 3600.0

    def evaluate_migration_hurdle(
        self,
        candidate_daily_yield: float,
        holding_horizon_hours: float | None = None,
        daily_yield_a: float | None = None,
    ) -> tuple[bool, HurdleDecision, SwitchingCostDecomposition]:
        """Evaluate Net Alpha Hurdle: Delta_Yield * Tau >= Cost_switch."""
        y_a = daily_yield_a if daily_yield_a is not None else self.get_current_daily_yield()
        try:
            horizon = (
                float(holding_horizon_hours)
                if holding_horizon_hours is not None
                else self._holding_horizon_hours()
            )
        except ValueError as exc:
            logger.warning(f"Migration hurdle unknown for {self.condition_id}: {exc}")
            cost = self.compute_switching_cost(daily_yield=y_a)
            decision = HurdleDecision(
                should_switch=False,
                delta_yield_daily_usd=float(candidate_daily_yield) - y_a,
                required_delta_yield_daily_usd=float("inf"),
                net_alpha_holding_usd=0.0,
                switching_cost_total_usd=cost.total_switching_cost_usd,
                holding_horizon_hours=0.0,
                amortized_cost_daily_usd=float("inf"),
                reason=f"holding horizon unmeasured: {exc}",
            )
            self.latest_hurdle_decision = decision
            return False, decision, cost
        cost = self.compute_switching_cost(daily_yield=y_a)
        decision = self.migration_engine.evaluate_switching_hurdle(
            y_net_a_daily=y_a,
            y_net_b_daily=candidate_daily_yield,
            switching_cost_usd=cost.total_switching_cost_usd,
            holding_horizon_hours=horizon,
        )
        self.latest_hurdle_decision = decision
        return decision.should_switch, decision, cost
