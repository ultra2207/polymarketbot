"""Maker quote placement and cancellation."""

from __future__ import annotations

import time
from typing import Optional

from polymarketbot.logs import get_logger
from polymarketbot.strategy.reward.scoring import quadratic_order_score
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
            mid_price=token_mid if token_mid is not None else price,
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
        if existing and (
            abs(float(existing.get("price", 0.0)) - price) < 1e-9
            and abs(float(existing.get("size", 0.0)) - requested_size) < 1e-6
        ):
            # Price and size unchanged: retain active CLOB order without queue penalty
            return

        pre_available: Optional[float] = None
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
            self._canceled_order_ids.add(old_oid)
            if not self.clob_client.cancel_order(old_oid):
                existing["status"] = "CANCELLING"
                logger.warning(
                    f"Cancel of {qid} order {old_oid} not confirmed; refusing to stack a replacement"
                )
                return
            self._release_reserved_cash(existing)
            self.active_quotes.pop(qid, None)

        place_started = time.perf_counter()
        res = self.clob_client.place_order(
            token_id=token_id,
            side=side,
            price=price,
            size=requested_size,
            tick_size=self.tick_size,
            neg_risk=self.neg_risk,
            fee_rate_bps=self.fee_rate_bps,
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
                decreased = (pre_available - post_available) > max(1e-6, expected_reserve * 0.5)
                self._locked_already_in_balance = not decreased
                if self._locked_already_in_balance:
                    # The endpoint includes the reserve, so undo the local
                    # reservation made while the mode was still unknown.
                    self.cash += expected_reserve
                logger.info(
                    f"Locked-cash calibration: available {pre_available:.4f} -> {post_available:.4f} "
                    f"after BUY {requested_size:.2f}@{price:.4f}; balance "
                    f"{'INCLUDES' if self._locked_already_in_balance else 'EXCLUDES'} locked collateral"
                )

        # Score quote for rewards. With no book there is no mid to score against;
        # the quote is still recorded, with a zero (unmeasured) score.
        b_tok, a_tok = self.get_best_bid_ask(tok)
        score = 0.0
        v_queue_value = 0.0
        if b_tok is not None and a_tok is not None:
            mid_tok = (b_tok + a_tok) / 2.0
            spread_cents = abs(price - mid_tok) * 100.0
            score = quadratic_order_score(
                max_spread_cents=self.rewards_max_spread,
                spread_cents=spread_cents,
                size=requested_size,
                min_size=self.rewards_min_size,
            )

            vq_res = self.migration_engine.queue_model.evaluate_queue_value(
                quote_size=requested_size,
                queue_ahead=0.0,
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
            "queue_ahead": 0.0,
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
                0.0,
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
        self._canceled_order_ids.add(order_id)
        cancel_started = time.perf_counter()
        cancelled = self.clob_client.cancel_order(order_id)
        cancel_rtt_ms = (time.perf_counter() - cancel_started) * 1000.0
        if cancelled:
            self._release_reserved_cash(quote_data)
            self.active_quotes.pop(qid, None)
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
