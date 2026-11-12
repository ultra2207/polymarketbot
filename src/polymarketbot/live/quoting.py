"""Quote lifecycle and the two-tier de-skew quote loop.

Mixed into ``BaseMarketMakingEngine``: every method is bound to the engine
instance and mutates its quotes exactly as before, so the tick order is
unchanged by living in a separate module.
"""

from __future__ import annotations

import math
import time

from polymarketbot.logs import get_logger
from polymarketbot.risk.contracts import RiskAction, RiskDecision
from polymarketbot.strategy.reward.deskew import (
    DeSkewDecision,
    DeSkewPathway,
    DynamicThresholdCalculator,
)
from polymarketbot.strategy.reward.scoring import (
    OrderQuote,
    compute_book_side_scores,
    compute_minimum_score,
)

logger = get_logger("trading.base")


def _reservation_price(
    mid_yes: float,
    net_q: float,
    gamma: float,
    days_to_resolution: float,
) -> float:
    """Avellaneda-Stoikov reservation price for the YES leg.

    ``r = mid - q * gamma * sigma^2`` where ``sigma^2`` is the shared
    Wright-Fisher bounded variance (:meth:`DynamicThresholdCalculator.
    calculate_bounded_variance`). The variance's time floor is the engine's own
    one-hour minimum horizon, passed as ``tau`` so this live path keeps its
    measured horizon while the formula itself is the strategy's.
    """
    tau = max(1.0 / 24.0, float(days_to_resolution))
    variance = DynamicThresholdCalculator.calculate_bounded_variance(mid_yes, tau)
    return max(0.01, min(0.99, mid_yes - (net_q * gamma * variance)))


class QuotingMixin:
    """Reprice gate, Q_min scoring, tier-1 fading and neutral maker quoting."""

    def should_reprice(
        self,
        existing: dict | None,
        price: float,
        size: float,
        ts: float,
    ) -> bool:
        """Return True when a resting quote must be cancelled/replaced.

        A missing quote always needs placement. An existing quote is left untouched
        while it is younger than ``min_reprice_interval_s`` and when neither its
        price has moved by more than ``min_reprice_ticks`` ticks nor its size has
        changed materially. This is what stops the cancel/replace storm that
        destroys FIFO queue priority (the actual source of LP income).
        """
        if not existing:
            return True

        tick = self.quote_tick_size if self.quote_tick_size > 0 else 0.001
        last_ts = float(existing.get("timestamp", 0.0) or 0.0)
        if ts - last_ts < self.min_reprice_interval_s:
            return False

        existing_price = float(existing.get("price", 0.0) or 0.0)
        existing_size = float(existing.get("size", 0.0) or 0.0)
        price_moved = abs(float(price) - existing_price)
        size_moved = abs(float(size) - existing_size)
        return not (price_moved <= self.min_reprice_ticks * tick and size_moved < 1e-06)

    def compute_quote_scores(self) -> tuple[float, float, float]:
        """Return ``(q_min, q_one, q_two)`` for the currently resting quotes.

        Uses the official complementary-book scoring with the live YES/NO mids and
        the real max spread / minimum size. Every resting quote is treated as having
        survived the full sampled minute.
        """
        b_yes, a_yes = self.get_best_bid_ask("YES")
        b_no, a_no = self.get_best_bid_ask("NO")
        if b_yes is None or a_yes is None or b_no is None or a_no is None:
            logger.debug(
                f"No complete book for {self.condition_id}; reward score is zero"
            )
            return 0.0, 0.0, 0.0

        mid_yes = (b_yes + a_yes) / 2.0
        mid_no = (b_no + a_no) / 2.0

        yes_quotes: list[OrderQuote] = []
        no_quotes: list[OrderQuote] = []
        for q in self.active_quotes.values():
            price = float(q.get("price", 0.0) or 0.0)
            size = float(q.get("size", 0.0) or 0.0)
            if size <= 0.0 or not (0.0 < price < 1.0):
                continue
            quote = OrderQuote(
                side=str(q.get("side", "BUY")).upper(),
                outcome=str(q.get("token_side", "YES")).upper(),
                price=price,
                size=size,
            )
            if quote.outcome == "NO":
                no_quotes.append(quote)
            else:
                yes_quotes.append(quote)

        q_one_yes, q_two_yes = compute_book_side_scores(
            yes_quotes,
            mid_price_yes=mid_yes,
            max_spread_cents=self.rewards_max_spread,
            min_size=self.rewards_min_size,
        )
        # Passing ``1 - mid_no`` scores the NO book around its own mid (the scorer
        # derives mid_no as ``1 - mid_price_yes``) while keeping the Q1/Q2 mapping.
        q_one_no, q_two_no = compute_book_side_scores(
            no_quotes,
            mid_price_yes=1.0 - mid_no,
            max_spread_cents=self.rewards_max_spread,
            min_size=self.rewards_min_size,
        )

        q_one = q_one_yes + q_one_no
        q_two = q_two_yes + q_two_no
        q_min = compute_minimum_score(q_one=q_one, q_two=q_two, midpoint=mid_yes)
        return q_min, q_one, q_two

    def evaluate_quotes(self, timestamp: float | None = None) -> None:
        """Run 2-Tier Dynamic De-Skewing Optimizer and generate maker quotes."""
        ts = timestamp or time.time()
        pos = self.get_position(self.condition_id)

        # Check master RiskManager halt or kill-switch status
        if self.risk_manager.portfolio_state.is_halted or self.risk_manager.portfolio_state.kill_switch_active:
            if self.active_quotes:
                self.cancel_all_quotes()
            return

        # Immediate Risk Breaker check on current mark-to-market equity
        cash_val = self.get_cash()
        equity_val = self.get_equity()
        self.risk_manager.update_portfolio_pnl(
            current_equity=equity_val,
            realized_pnl=pos.realized_pnl,
            unrealized_pnl=self._unrealized_pnl(),
            current_cash=cash_val,
            timestamp=ts,
        )
        if self.risk_manager.portfolio_state.is_halted or self.risk_manager.portfolio_state.kill_switch_active:
            if self.active_quotes:
                self.cancel_all_quotes()
            return

        b_yes, a_yes = self.get_best_bid_ask("YES")
        b_no, a_no = self.get_best_bid_ask("NO")
        if b_yes is None or a_yes is None or b_no is None or a_no is None:
            logger.debug(
                f"No complete book for {self.condition_id}; skipping quote evaluation"
            )
            return

        mid_yes = (b_yes + a_yes) / 2.0

        # Check Price Jump Volatility Circuit Breaker
        if self._check_volatility_breaker(mid_yes, ts):
            return

        # Microstructure signals
        state_yes = self.micro_risk.flow(self.asset_id_yes)
        rolling_ofi = state_yes.ofi_10_events if state_yes else 0.0
        drift_cents = state_yes.drift_cents if state_yes else 0.0

        # Evaluate skew. Without a fitted calibration (or any other measured
        # input the optimizer needs) de-skewing is disabled: quote neutrally
        # rather than calling into ``None`` or pricing a provisional threshold.
        optimizer = self.deskew_optimizer
        if optimizer is None:
            if not self._deskew_skip_logged:
                logger.warning(
                    f"DEGRADED: de-skew step skipped for {self.condition_id} "
                    "(no optimizer); quoting neutral two-sided"
                )
                self._deskew_skip_logged = True
            self._execute_neutral_maker_quoting(mid_yes, b_yes, a_yes, ts)
            return

        decision = optimizer.evaluate_skew(
            shares_yes=pos.shares_yes,
            shares_no=pos.shares_no,
            best_bid_yes=b_yes,
            best_ask_yes=a_yes,
            best_bid_no=b_no,
            best_ask_no=a_no,
            holding_time_seconds=self.inventory_age_seconds(ts),
            rolling_ofi=rolling_ofi,
            micro_price_drift_cents=drift_cents,
            time_to_resolution_days=max(1.0 / 24.0, float(self.days_to_resolution)),
        )
        self.latest_deskew_decision = decision

        # Tier 2: Emergency Taker
        if decision.pathway in (DeSkewPathway.SYNTHETIC_MERGE, DeSkewPathway.DIRECT_CROSS):
            self._execute_tier2_taker_deskew(decision, ts)
            return

        # Tier 1: Passive Avellaneda-Stoikov Quote Fading
        if decision.pathway == DeSkewPathway.PASSIVE_FADE:
            net_q = pos.shares_yes - pos.shares_no
            if abs(net_q) >= optimizer.q_soft:
                self._execute_tier1_passive_fade(decision, mid_yes, b_yes, a_yes, ts)
                return
            else:
                self._record_deskew_event(decision, ts)
                logger.warning(
                    f"[ASYMMETRIC TOXICITY DEFENSE] {decision.reason} "
                    f"(fading {decision.action_side} {decision.target_token})"
                )

        # Neutral Two-Sided Maker Quoting around Mid
        self._execute_neutral_maker_quoting(mid_yes, b_yes, a_yes, ts)

    def _measure_reward_score(self) -> float | None:
        """Current official Q_min score, or None when it cannot be computed.

        ``None`` is the refusal the paper pillar reports for an unmeasured
        book (:func:`polymarketbot.strategy.reward.accrual.measured_accrual`
        returns ``None`` there too). A missing book is missing data: reporting
        it as a measured ``0.0`` would claim the book was read and scored.
        """
        b_yes, a_yes = self.get_best_bid_ask("YES")
        b_no, a_no = self.get_best_bid_ask("NO")
        if b_yes is None or a_yes is None or b_no is None or a_no is None:
            logger.debug(
                f"No complete book for {self.condition_id}; reward Q_min unmeasured"
            )
            return None
        try:
            q_min, _q_one, _q_two = self.compute_quote_scores()
        except Exception as e:  # noqa: BLE001 - measurement must never break quoting
            logger.debug(f"Reward score measurement failed: {e}")
            return None
        value = float(q_min)
        if math.isnan(value) or value in (float("inf"), float("-inf")):
            return None
        return value

    def _execute_tier1_passive_fade(
        self,
        decision: DeSkewDecision,
        mid_yes: float,
        best_bid_yes: float,
        best_ask_yes: float,
        ts: float,
    ) -> None:
        """Tier 1: Avellaneda-Stoikov inventory quote fading.

        The quote mutation itself is unchanged; the reward score is sampled
        immediately before and after so the fading cost (Q_min given up) is
        measured rather than assumed.
        """
        self._record_deskew_event(decision, ts)
        q_min_before = self._measure_reward_score()
        inventory_before = float(getattr(self, "shares_yes", 0.0)) + float(
            getattr(self, "shares_no", 0.0)
        )

        self._apply_tier1_passive_fade(decision, mid_yes, best_bid_yes, best_ask_yes, ts)

        q_min_after = self._measure_reward_score()
        inventory_after = float(getattr(self, "shares_yes", 0.0)) + float(
            getattr(self, "shares_no", 0.0)
        )
        if q_min_before is not None and q_min_after is not None:
            reward_score_lost = max(0.0, q_min_before - q_min_after)
            note = (
                f"tier1 fade; Q_min {q_min_before:.4f} -> {q_min_after:.4f}"
            )
        else:
            reward_score_lost = 0.0
            note = "tier1 fade; reward Q_min not measurable"
        self._insert_rebalance_row(
            pathway=DeSkewPathway.PASSIVE_FADE.value,
            ts=ts,
            reward_score_lost=reward_score_lost,
            inventory_before=inventory_before,
            inventory_after=inventory_after,
            note=note,
        )

    def _apply_tier1_passive_fade(
        self,
        decision: DeSkewDecision,
        mid_yes: float,
        best_bid_yes: float,
        best_ask_yes: float,
        ts: float,
    ) -> None:
        if self.institutional_mode:
            pos = self.get_position(self.condition_id)
            net_q = pos.shares_yes - pos.shares_no
            r_yes = _reservation_price(
                mid_yes, net_q, self.gamma, self.days_to_resolution
            )
            offset = self.spread_offset_cents / 100.0
            tick = self.quote_tick_size if self.quote_tick_size > 0 else 0.001
            _, best_ask_no = self.get_best_bid_ask("NO")
            max_bid_yes = self.round_quote_price(best_ask_yes - tick, "YES")
            if best_ask_no is None:
                logger.debug(
                    f"No NO book for {self.condition_id}; skipping tier-1 fade"
                )
                return
            max_bid_no = self.round_quote_price(best_ask_no - tick, "NO")

            if net_q > 0:
                # Long YES -> tighten NO bid to complete sets; fade/widen YES bid
                target_bid_no = self.round_quote_price(
                    1.00 - max(best_bid_yes + tick, decision.target_price), "NO"
                )
                target_bid_no = min(target_bid_no, max_bid_no)
                faded_bid_yes = min(
                    self.round_quote_price(r_yes - offset - 0.01, "YES"), max_bid_yes
                )
                deskew_size = max(self.quote_size, net_q)
                if self.should_reprice(self.active_quotes.get("BUY_NO"), target_bid_no, deskew_size, ts):
                    self._set_quote("NO", "BUY", target_bid_no, deskew_size, ts)
                if self.should_reprice(
                    self.active_quotes.get("BUY_YES"), faded_bid_yes, self.quote_size, ts
                ):
                    self._set_quote("YES", "BUY", faded_bid_yes, self.quote_size, ts)
            elif net_q < 0:
                # Long NO -> tighten YES bid to complete sets; fade/widen NO bid
                target_bid_yes = min(
                    self.round_quote_price(decision.target_price, "YES"), max_bid_yes
                )
                faded_bid_no = min(
                    self.round_quote_price(1.00 - (r_yes + offset + 0.01), "NO"), max_bid_no
                )
                deskew_size = max(self.quote_size, abs(net_q))
                if self.should_reprice(
                    self.active_quotes.get("BUY_YES"), target_bid_yes, deskew_size, ts
                ):
                    self._set_quote("YES", "BUY", target_bid_yes, deskew_size, ts)
                if self.should_reprice(
                    self.active_quotes.get("BUY_NO"), faded_bid_no, self.quote_size, ts
                ):
                    self._set_quote("NO", "BUY", faded_bid_no, self.quote_size, ts)
            else:
                self._execute_neutral_maker_quoting(mid_yes, best_bid_yes, best_ask_yes, ts)
                return

            self._cancel_quote("SELL_YES")
            self._cancel_quote("SELL_NO")
            return

        # Legacy Single-Token Mode
        tick = self.quote_tick_size if self.quote_tick_size > 0 else 0.001
        if decision.target_token == "YES" and decision.action_side == "SELL":
            faded_ask = max(
                self.round_quote_price(decision.target_price, "YES"),
                self.round_quote_price(best_bid_yes + tick, "YES"),
            )
            if self.should_reprice(
                self.active_quotes.get("SELL_YES"), faded_ask, self.quote_size, ts
            ):
                self._set_quote("YES", "SELL", faded_ask, self.quote_size, ts)
            self._cancel_quote("BID_YES")
            self._cancel_quote("BUY_YES")
        elif decision.target_token == "YES" and decision.action_side == "BUY":
            faded_bid = min(
                self.round_quote_price(decision.target_price, "YES"),
                self.round_quote_price(best_ask_yes - tick, "YES"),
            )
            if self.should_reprice(
                self.active_quotes.get("BUY_YES"), faded_bid, self.quote_size, ts
            ):
                self._set_quote("YES", "BUY", faded_bid, self.quote_size, ts)
            self._cancel_quote("ASK_YES")
            self._cancel_quote("SELL_YES")

    def _execute_neutral_maker_quoting(
        self,
        mid_yes: float,
        best_bid_yes: float,
        best_ask_yes: float,
        ts: float,
    ) -> None:
        """Standard two-sided maker quotes with per-leg microstructure risk decisions."""
        if self._check_volatility_breaker(mid_yes, ts):
            return

        offset = self.spread_offset_cents / 100.0

        bid_offset = offset
        ask_offset = offset
        suppress_bid = False
        suppress_ask = False

        # Replacing a resting order releases its locked collateral, so sizing must use
        # total trading cash (free + locked), never free cash alone.
        cash_balance = self.get_cash() + self.get_locked_cash()
        tick = self.quote_tick_size if self.quote_tick_size > 0 else 0.001

        if self.institutional_mode:
            # Jane Street Institutional Dual-Cash Quoting (BUY YES + BUY NO)
            pos = self.get_position(self.condition_id)
            net_q = pos.shares_yes - pos.shares_no
            r_yes = _reservation_price(
                mid_yes, net_q, self.gamma, self.days_to_resolution
            )

            best_bid_no, best_ask_no = self.get_best_bid_ask("NO")
            max_bid_yes = self.round_quote_price(best_ask_yes - tick, "YES")
            if best_ask_no is None or best_bid_no is None:
                logger.debug(
                    f"No NO book for {self.condition_id}; skipping neutral quoting"
                )
                return
            max_bid_no = self.round_quote_price(best_ask_no - tick, "NO")
            min_ask_yes = self.round_quote_price(best_bid_yes + tick, "YES")

            # Inventory skew is price impact, not a policy constant: one share
            # of unavoidable net inventory moves the price by the reciprocal of
            # the measured size resting at the touch. No depth at the touch means
            # no measured impact, so no skew is applied (and the quote keeps its
            # plain reservation price) instead of using a hand-set 0.0005.
            skew_adj = net_q * self._inventory_impact_per_share("YES")
            target_bid_yes = min(
                self.round_quote_price(r_yes - bid_offset - max(0.0, skew_adj), "YES"),
                max_bid_yes,
            )
            target_ask_yes = max(
                self.round_quote_price(r_yes + ask_offset - min(0.0, skew_adj), "YES"),
                min_ask_yes,
            )
            # Derive the NO bid from the synthetic YES ask, then never cross the NO book.
            target_bid_no = min(
                self.round_quote_price(1.00 - target_ask_yes, "NO"),
                max_bid_no,
            )

            # One engine decides each leg on its own queue position and flow.
            mid_no = (best_bid_no + best_ask_no) / 2.0
            bid_pull, bid_widen_cents, bid_size_mult = self._micro_leg_decision(
                self.asset_id_yes, "BUY", target_bid_yes, mid_yes, ts, "BID YES"
            )
            no_pull, no_widen_cents, no_size_mult = self._micro_leg_decision(
                self.asset_id_no, "BUY", target_bid_no, mid_no, ts, "BID NO"
            )
            suppress_bid = suppress_bid or bid_pull
            suppress_ask = suppress_ask or no_pull
            if bid_widen_cents:
                bid_offset += bid_widen_cents / 100.0
                target_bid_yes = min(
                    self.round_quote_price(r_yes - bid_offset - max(0.0, skew_adj), "YES"),
                    max_bid_yes,
                )
            if no_widen_cents:
                ask_offset += no_widen_cents / 100.0
                target_ask_yes = max(
                    self.round_quote_price(r_yes + ask_offset - min(0.0, skew_adj), "YES"),
                    min_ask_yes,
                )
                target_bid_no = min(
                    self.round_quote_price(1.00 - target_ask_yes, "NO"),
                    max_bid_no,
                )

            # Enforce the rewards minimum size: a sub-minimum order earns nothing and
            # only adds fill risk, so suppress quoting entirely instead of emitting it.
            active_price_sum = 0.0
            if not suppress_bid:
                active_price_sum += target_bid_yes
            if not suppress_ask:
                active_price_sum += target_bid_no

            eff_size = self.quote_size
            if active_price_sum > 0:
                eff_size = min(self.quote_size, cash_balance / active_price_sum)

            if eff_size + 1e-9 < self.rewards_min_size:
                logger.warning(
                    f"[REWARDS MIN SIZE] Cash ${cash_balance:.2f} supports only {eff_size:.2f} shares at "
                    f"combined price {active_price_sum:.3f} < min_size={self.rewards_min_size:.1f} "
                    f"for {self.condition_id}; suppressing quotes."
                )
                self._cancel_quote("BUY_YES")
                self._cancel_quote("BUY_NO")
                self._cancel_quote("SELL_YES")
                self._cancel_quote("SELL_NO")
                return

            # Apply each leg's streak size reduction; a size that no longer
            # qualifies for rewards is not worth resting, so suppress the leg.
            bid_size = eff_size * bid_size_mult
            no_size = eff_size * no_size_mult
            if not suppress_bid and bid_size + 1e-9 < self.rewards_min_size:
                logger.warning(
                    f"[MICRO RISK] BID YES size {bid_size:.2f} < min_size="
                    f"{self.rewards_min_size:.1f} after the streak reduction; suppressing the leg."
                )
                suppress_bid = True
            if not suppress_ask and no_size + 1e-9 < self.rewards_min_size:
                logger.warning(
                    f"[MICRO RISK] BID NO size {no_size:.2f} < min_size="
                    f"{self.rewards_min_size:.1f} after the streak reduction; suppressing the leg."
                )
                suppress_ask = True

            # Place quotes or cancel if suppressed. Leave untouched quotes resting so
            # queue priority survives book ticks (reprice gate).
            if not suppress_bid:
                if self.should_reprice(
                    self.active_quotes.get("BUY_YES"), target_bid_yes, bid_size, ts
                ):
                    self._set_quote("YES", "BUY", target_bid_yes, bid_size, ts)
            else:
                self._cancel_quote("BUY_YES")

            if not suppress_ask:
                if self.should_reprice(
                    self.active_quotes.get("BUY_NO"), target_bid_no, no_size, ts
                ):
                    self._set_quote("NO", "BUY", target_bid_no, no_size, ts)
            else:
                self._cancel_quote("BUY_NO")

            self._cancel_quote("SELL_YES")
            self._cancel_quote("SELL_NO")
            return

        # Legacy Single-Token Mode (BUY YES + SELL YES)
        if self.quote_size < self.rewards_min_size:
            logger.warning(
                f"[REWARDS MIN SIZE] quote_size={self.quote_size:.1f} < min_size="
                f"{self.rewards_min_size:.1f} for {self.condition_id}; suppressing quotes."
            )
            self._cancel_quote("BUY_YES")
            self._cancel_quote("SELL_YES")
            return

        target_bid = min(
            self.round_quote_price(mid_yes - bid_offset, "YES"),
            self.round_quote_price(best_ask_yes - tick, "YES"),
        )
        target_ask = max(
            self.round_quote_price(mid_yes + ask_offset, "YES"),
            self.round_quote_price(best_bid_yes + tick, "YES"),
        )

        # Same mapping as the institutional legs: the engine answers per order.
        bid_pull, bid_widen_cents, bid_size_mult = self._micro_leg_decision(
            self.asset_id_yes, "BUY", target_bid, mid_yes, ts, "BID YES"
        )
        ask_pull, ask_widen_cents, ask_size_mult = self._micro_leg_decision(
            self.asset_id_yes, "SELL", target_ask, mid_yes, ts, "ASK YES"
        )
        suppress_bid = suppress_bid or bid_pull
        suppress_ask = suppress_ask or ask_pull
        if bid_widen_cents:
            bid_offset += bid_widen_cents / 100.0
            target_bid = min(
                self.round_quote_price(mid_yes - bid_offset, "YES"),
                self.round_quote_price(best_ask_yes - tick, "YES"),
            )
        if ask_widen_cents:
            ask_offset += ask_widen_cents / 100.0
            target_ask = max(
                self.round_quote_price(mid_yes + ask_offset, "YES"),
                self.round_quote_price(best_bid_yes + tick, "YES"),
            )

        bid_size = self.quote_size * bid_size_mult
        ask_size = self.quote_size * ask_size_mult
        if not suppress_bid and bid_size + 1e-9 < self.rewards_min_size:
            suppress_bid = True
        if not suppress_ask and ask_size + 1e-9 < self.rewards_min_size:
            suppress_ask = True

        if not suppress_bid:
            if self.should_reprice(
                self.active_quotes.get("BUY_YES"), target_bid, bid_size, ts
            ):
                self._set_quote("YES", "BUY", target_bid, bid_size, ts)
        else:
            self._cancel_quote("BUY_YES")

        if not suppress_ask:
            if self.should_reprice(
                self.active_quotes.get("SELL_YES"), target_ask, ask_size, ts
            ):
                self._set_quote("YES", "SELL", target_ask, ask_size, ts)
        else:
            self._cancel_quote("SELL_YES")

    def _micro_leg_decision(
        self,
        token_id: str,
        side: str,
        price: float,
        mid: float | None,
        ts: float,
        leg: str,
    ) -> tuple[bool, float, float]:
        """Map one unified risk decision onto one quote leg.

        Returns ``(suppress, widen_cents, size_multiplier)``. ``PULL``/
        ``SUPPRESS`` retract the leg immediately (bypassing the reprice gate);
        ``WIDEN`` returns the measured drift so the caller can rerun the leg's
        offset and let the normal reprice gate decide; ``MAINTAIN``/``BLOCK``
        change nothing (capital and breaker gates live in
        :class:`RiskManager`). ``size_multiplier`` is the adverse-fill streak's
        size reduction, which the caller applies to the leg's size.

        Every decision is recorded on the live tape, including ``MAINTAIN``:
        the unmeasured and near-miss decisions are exactly what a post-run
        audit needs to explain why a quote was not pulled.
        """
        decision: RiskDecision = self.micro_risk.evaluate(
            token_id,
            side,
            float(price),
            float(mid if mid is not None else price),
            ts,
        )
        self._emit_event(
            "micro_risk_decision",
            {"token_id": token_id, "leg": leg, **decision.as_record()},
        )
        if decision.action in (RiskAction.PULL, RiskAction.SUPPRESS):
            logger.warning(f"[MICRO RISK] Pulling {leg} quote: {decision.reason}")
            return True, 0.0, 1.0
        if decision.action is RiskAction.WIDEN:
            logger.warning(
                f"[MICRO RISK] Widening {leg} offset by +{decision.widen_cents:.2f}c: "
                f"{decision.reason}"
            )
            return False, float(decision.widen_cents), float(decision.size_multiplier)
        return False, 0.0, float(decision.size_multiplier)

    def update_quote_size(self, new_quote_size: float, timestamp: float | None = None) -> bool:
        """Safely update quote size and re-evaluate active quotes if changed by >= 5.0 shares."""
        new_size = round(max(5.0, float(new_quote_size)), 1)
        size_diff = abs(new_size - self.quote_size)
        old_size = self.quote_size
        self.quote_size = new_size

        if size_diff >= 5.0:
            ts = timestamp or time.time()
            logger.info(
                f"Quote size compounded: {old_size:.1f} -> {new_size:.1f} shares "
                f"(diff={size_diff:.1f} >= 5.0). Re-evaluating active quotes."
            )
            self.evaluate_quotes(timestamp=ts)
            return True

        return False

    def _record_deskew_event(self, decision: DeSkewDecision, ts: float) -> None:
        """Persist dynamic de-skew decision to SQLite."""
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO deskew_events (
                timestamp, condition_id, pathway, urgency, target_token,
                action_side, price, size, savings_cents, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                self.condition_id,
                decision.pathway.value,
                decision.urgency_level,
                decision.target_token,
                decision.action_side,
                decision.target_price,
                decision.target_size,
                decision.synthetic_savings_cents,
                decision.reason,
            ),
        )
        self._conn.commit()
