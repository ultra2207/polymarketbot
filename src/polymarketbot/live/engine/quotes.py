"""Maker quote placement and cancellation."""

from __future__ import annotations

import time

from polymarketbot.logs import get_logger
from polymarketbot.strategy.reward.scoring import (
    order_spread_cents,
    quadratic_order_score,
)
from polymarketbot.strategy.venue.clob import ClobAPIError

logger = get_logger("real_money_engine")


class QuoteMixin:
    """Quote lifecycle on the live CLOB."""

    def _set_quote(
        self,
        token_side: str,
        quote_side: str,
        price: float,
        size: float,
        ts: float,
    ) -> None:
        """Place or update a live signed maker limit order on Polymarket CLOB."""
        tok = str(token_side).upper()
        side = str(quote_side).upper()
        qid = f"{side}_{tok}"

        if not self.trading_enabled or self.market_closed:
            logger.debug(f"Quote {qid} suppressed: trading_enabled={self.trading_enabled} closed={self.market_closed}")
            return
        if not self.has_fresh_book(tok):
            logger.debug(f"Quote {qid} suppressed: no fresh {tok} book")
            return

        requested_price = float(price)
        requested_size = float(size)
        if requested_size <= 0.0 or not (0.0 < requested_price < 1.0):
            logger.warning(f"Quote {qid} rejected before placement: price={requested_price} size={requested_size}")
            return
        price = self.round_quote_price(requested_price, tok)
        token_id = self.asset_id_yes if tok == "YES" else self.asset_id_no

        # Master risk check: position, notional, cash, inventory and toxicity caps.
        token_pos = self.shares_yes if tok == "YES" else self.shares_no
        token_avg = self.avg_cost_yes if tok == "YES" else self.avg_cost_no
        token_mid = self._fresh_mid(tok)
        if token_mid is None:
            # A limit price is not a mid. Without a fresh book the risk check,
            # the reward score and every mid-relative distance are unmeasured,
            # so the quote is refused rather than priced against itself.
            logger.warning(
                f"Quote {qid} refused: no fresh {tok} mid (book missing or stale); "
                "the limit price is not a mid"
            )
            self._cancel_quote(qid)
            return
        allowed, reason, approved_size, approved_price = self.risk_manager.validate_quote(
            asset_id=token_id,
            condition_id=self.condition_id,
            side=side,
            price=price,
            size=requested_size,
            current_pos_shares=token_pos,
            current_pos_cost=abs(token_pos * token_avg),
            current_portfolio_inventory_usd=self._inventory_value(),
            current_cash_usd=self.get_cash(),
            mid_price=token_mid,
            tick_size=self.tick_size,
            timestamp=ts,
            event_id=self.event_id,
            current_event_inventory_usd=self._inventory_value(),
            current_event_cost=abs(token_pos * token_avg),
        )
        if not allowed:
            if ts - self._last_risk_block_ts >= 30.0:
                self._last_risk_block_ts = ts
                logger.warning(f"Risk rejected quote {qid}: {reason}")
            else:
                logger.debug(f"Risk rejected quote {qid}: {reason}")
            self._emit_event(
                "risk_blocked",
                {
                    "asset_id": token_id,
                    "side": side,
                    "price": price,
                    "requested_size": requested_size,
                    "reason": reason,
                },
            )
            self._cancel_quote(qid)
            return
        price = self.round_quote_price(approved_price, tok)
        requested_size = float(approved_size)
        if requested_size + 1e-9 < self.rewards_min_size:
            logger.warning(
                f"Quote {qid} suppressed: risk-approved size {requested_size:.2f} is below "
                f"rewards_min_size={self.rewards_min_size:.2f}"
            )
            self._cancel_quote(qid)
            return

        existing = self.active_quotes.get(qid)
        if existing and str(existing.get("status", "")).upper() == "CANCELLING":
            # The previous cancel was never confirmed: the venue may still hold
            # this order, so neither re-placing nor pretending it rests is safe.
            # Retry the cancel and re-place only once the venue confirms it.
            old_oid = str(existing.get("order_id", "") or "")
            if old_oid and not self.clob_client.cancel_order(old_oid):
                logger.warning(
                    f"Quote {qid} order {old_oid} is still un-cancelled; retrying the "
                    "cancel and sending no replacement this tick"
                )
                return
            self._release_reserved_cash(existing)
            self.active_quotes.pop(qid, None)
            self.micro_risk.on_cancel(token_id, old_oid)
            existing = None
        if existing and (
            abs(float(existing.get("price", 0.0)) - price) < 1e-9
            and abs(float(existing.get("size", 0.0)) - requested_size) < 1e-6
        ):
            # Price and size unchanged: retain active CLOB order without queue penalty
            return

        pre_available: float | None = None
        if (
            not self.dry_run
            and side == "BUY"
            and self._locked_already_in_balance is None
        ):
            try:
                pre_available = float(self.clob_client.get_balance()["available"])
            except (ClobAPIError, KeyError, TypeError, ValueError) as e:
                logger.warning(f"Could not read pre-placement collateral for locked calibration: {e}")

        # Cancel previous resting order if modifying price or size. Never place a
        # replacement while the old order's cancel is unconfirmed.
        if existing and existing.get("order_id"):
            old_oid = str(existing["order_id"])
            if not self.clob_client.cancel_order(old_oid):
                existing["status"] = "CANCELLING"
                logger.warning(
                    f"Cancel of {qid} order {old_oid} not confirmed; refusing to stack a replacement"
                )
                return
            self._release_reserved_cash(existing)
            self.active_quotes.pop(qid, None)
            self.micro_risk.on_cancel(token_id, old_oid)

        place_started = time.perf_counter()
        res = self.clob_client.place_order(
            token_id=token_id,
            side=side,
            price=price,
            size=requested_size,
            tick_size=self.tick_size,
            neg_risk=self.neg_risk,
            fee_rate_bps=self.fee_rate_bps,
            min_order_usd=self.min_order_usd,
            post_only=True,
        )
        place_rtt_ms = (time.perf_counter() - place_started) * 1000.0

        if not res.success:
            logger.warning(f"Failed to place live CLOB quote {qid}: {res.error_message}")
            self.active_quotes.pop(qid, None)
            return

        self._reserve_cash_for_buy(requested_size, price)

        if pre_available is not None and not self.dry_run:
            try:
                post_available = float(self.clob_client.get_balance()["available"])
            except (ClobAPIError, KeyError, TypeError, ValueError) as e:
                logger.warning(f"Locked-cash calibration read failed after placement: {e}")
            else:
                expected_reserve = requested_size * price
                # Which accounting mode the endpoint reports is decided by
                # comparing the observed decrease to the reserve this order
                # placed, not by a hand-set 50 % threshold: the reserve either
                # left the free balance or it did not. A decrease in between
                # (another fill or cancel moved the balance in the same window)
                # measures nothing, so the mode stays uncalibrated.
                if expected_reserve <= 0.0:
                    observed_ratio = 0.0
                else:
                    observed_ratio = (pre_available - post_available) / expected_reserve
                if observed_ratio >= 0.99:
                    self._locked_already_in_balance = False
                elif observed_ratio <= 0.01:
                    self._locked_already_in_balance = True
                else:
                    logger.warning(
                        f"Locked-cash calibration inconclusive: free balance moved "
                        f"{observed_ratio * 100:.1f}% of the ${expected_reserve:.4f} reserve "
                        "placed; leaving the accounting mode uncalibrated"
                    )
                if self._locked_already_in_balance is True:
                    # The endpoint includes the reserve, so undo the local
                    # reservation made while the mode was still unknown.
                    self.cash += expected_reserve
                logger.info(
                    f"Locked-cash calibration: available {pre_available:.4f} -> {post_available:.4f} "
                    f"after BUY {requested_size:.2f}@{price:.4f}; balance "
                    f"{'INCLUDES' if self._locked_already_in_balance else 'EXCLUDES'} locked collateral"
                )

        # Track the L3 queue position this order lands in, from the last book.
        # ``on_entry`` snapshots the measured size resting ahead of us at this
        # price; that is this quote's real queue-ahead, so it is persisted
        # instead of reporting an unmeasured 0.0. A disabled gate returns None
        # (no calibration = nothing measured) and stays at 0.0.
        measured_queue_ahead = 0.0
        depth_bids, depth_asks = self._book_depth.get(tok, ([], []))
        try:
            entry = self.micro_risk.on_entry(
                str(res.order_id),
                token_id,
                side,
                price,
                requested_size,
                ts,
                bids=depth_bids,
                asks=depth_asks,
            )
            if entry is not None:
                measured_queue_ahead = float(entry.ahead_at_entry)
        except Exception as e:  # noqa: BLE001 - risk telemetry must never block a live order
            logger.warning(f"Unified risk engine entry record failed for {qid}: {e}")

        # Score quote for rewards. With no book there is no mid to score against;
        # the quote is still recorded, with a zero (unmeasured) score.
        b_tok, a_tok = self.get_best_bid_ask(tok)
        score = 0.0
        v_queue_value = 0.0
        if b_tok is not None and a_tok is not None:
            mid_tok = (b_tok + a_tok) / 2.0
            # The distance the venue scores from (``s``) is signed by side: a bid
            # at or above the mid, and an ask at or below it, sit at s = 0 and
            # score full, not at the absolute distance. Same function the paper
            # pillar scores with.
            spread_cents = order_spread_cents(price, mid_tok, side)
            score = quadratic_order_score(
                max_spread_cents=self.rewards_max_spread,
                spread_cents=spread_cents,
                size=requested_size,
                min_size=self.rewards_min_size,
            )

            if self.migration_engine is not None:
                vq_res = self.migration_engine.queue_model.evaluate_queue_value(
                    quote_size=requested_size,
                    queue_ahead=measured_queue_ahead,
                    half_spread_cents=max(0.1, spread_cents),
                    token_mid_price=mid_tok,
                )
                v_queue_value = vq_res.net_queue_value_usd

        self.active_quotes[qid] = {
            "quote_id": qid,
            "order_id": res.order_id,
            "market": self.condition_id,
            "token_side": tok,
            "side": side,
            "price": price,
            "size": requested_size,
            "size_matched": 0.0,
            "queue_ahead": round(measured_queue_ahead, 6),
            "score": round(score, 4),
            "v_queue": round(v_queue_value, 4),
            "timestamp": ts,
            "status": "LIVE",
            "is_dry_run": res.is_dry_run,
            "is_taker": False,
        }

        self.locked_cash = self._compute_locked_cash()
        self._emit_event(
            "quote_placed",
            {
                "quote_id": qid,
                "order_id": res.order_id,
                "asset_id": token_id,
                "side": side,
                "price": price,
                "size": requested_size,
                "queue_ahead": round(measured_queue_ahead, 6),
                "dry_run": bool(res.is_dry_run),
                "place_rtt_ms": round(place_rtt_ms, 3),
            },
        )

        # Persist quote to SQLite
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO active_quotes (
                timestamp, quote_id, condition_id, token_side, side, price, size, queue_ahead, score, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                qid,
                self.condition_id,
                tok,
                side,
                price,
                requested_size,
                round(measured_queue_ahead, 6),
                score,
                "LIVE",
            ),
        )
        self._conn.commit()

    def _cancel_quote(self, qid: str) -> None:
        """Cancel one resting quote, removing it from local state only on confirmation."""
        quote_data = self.active_quotes.get(qid)
        if not quote_data:
            return
        order_id = str(quote_data.get("order_id", "") or "")
        if not order_id:
            self.active_quotes.pop(qid, None)
            return
        cancel_started = time.perf_counter()
        cancelled = self.clob_client.cancel_order(order_id)
        cancel_rtt_ms = (time.perf_counter() - cancel_started) * 1000.0
        if cancelled:
            self._release_reserved_cash(quote_data)
            self.mark_quote_row(qid, "CANCELLED")
            self.active_quotes.pop(qid, None)
            token_side = str(quote_data.get("token_side", "")).upper()
            if token_side in ("YES", "NO"):
                self.micro_risk.on_cancel(
                    self.asset_id_yes if token_side == "YES" else self.asset_id_no,
                    order_id,
                )
            self.locked_cash = self._compute_locked_cash()
            self._emit_event(
                "quote_cancelled",
                {
                    "quote_id": qid,
                    "order_id": order_id,
                    "asset_id": quote_data.get("token_side"),
                    "cancel_rtt_ms": round(cancel_rtt_ms, 3),
                },
            )
        else:
            quote_data["status"] = "CANCELLING"
            logger.warning(f"Cancel of order {order_id} ({qid}) was not confirmed; keeping local tracking")
