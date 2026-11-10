"""Fees, marketable pricing and Tier 2 de-skew execution."""

from __future__ import annotations

import uuid
from typing import Optional

from polymarketbot.fees import maker_fee_rebate
from polymarketbot.live.engine.markout import RealMoneyFillRecord
from polymarketbot.logs import get_logger
from polymarketbot.strategy.reward.deskew import DeSkewDecision, DeSkewPathway

logger = get_logger("real_money_engine")


class DeskewMixin:
    """Tier 2 emergency de-skew and the fee helpers it uses."""

    def _taker_fee(self, size: float, price: float) -> float:
        """Exchange taker fee for a fill, from fee_schedule or fee_rate_bps."""
        if size <= 0.0 or price <= 0.0:
            return 0.0
        if self.fee_schedule is not None and self.fee_schedule.rate > 0:
            from polymarketbot.fees import taker_fee
            return taker_fee(size, price, self.fee_schedule)
        if self.fee_rate_bps <= 0:
            return 0.0
        return float(size) * float(price) * (self.fee_rate_bps / 10_000.0)

    def _maker_rebate(self, size: float, price: float) -> float:
        """Exchange maker fee rebate for a resting maker fill."""
        if size <= 0.0 or price <= 0.0:
            return 0.0
        if self.fee_schedule is not None and self.fee_schedule.rate > 0:
            return maker_fee_rebate(
                price, size, self.fee_schedule, maker_rebate_share_pct=self.maker_rebate_share_pct
            )
        elif self.fee_rate_bps > 0:
            t_fee = float(size) * float(price) * (self.fee_rate_bps / 10_000.0)
            return round(t_fee * self.maker_rebate_share_pct, 5)
        return 0.0

    def _marketable_price(self, token_side: str, side: str) -> Optional[float]:
        bid, ask = self.get_best_bid_ask(token_side)
        price = ask if str(side).upper() == "BUY" else bid
        if price is None or not (0.0 < price < 1.0):
            return None
        return price

    def _execute_tier2_taker_deskew(self, decision: DeSkewDecision, ts: float) -> None:
        """Execute Tier 2 emergency cross-spread taker execution on CLOB.

        Placement is not a fill: the order is registered as a taker quote and
        booked only by reconciliation against confirmed exchange state.
        """
        target_token = str(decision.target_token).upper()
        target_side = str(decision.action_side).upper()
        if target_token not in ("YES", "NO") or target_side not in ("BUY", "SELL"):
            return

        if self.dry_run:
            self._execute_tier2_dry_run(decision, ts)
            return

        if not self.trading_enabled or self.market_closed:
            return
        if not self.has_fresh_book(target_token):
            logger.warning(
                f"Tier 2 de-skew skipped for {self.condition_id[:12]}: {target_token} book is stale"
            )
            return

        # Never stack taker orders: resolve the in-flight one first.
        if self._deskew_order is not None:
            age = ts - float(self._deskew_order.get("timestamp", 0.0) or 0.0)
            if age < self.deskew_min_interval_s:
                return
            qid = str(self._deskew_order.get("quote_id", "") or "")
            tracked = self.active_quotes.get(qid)
            if tracked is None or str(tracked.get("order_id", "")) != str(
                self._deskew_order.get("order_id", "")
            ):
                self._deskew_order = None
            else:
                self.sync_live_orders_and_fills(timestamp=ts)
                qid = str(self._deskew_order.get("quote_id", "") if self._deskew_order else "")
                tracked = self.active_quotes.get(qid) if qid else None
                if tracked is None or str(tracked.get("order_id", "")) != str(
                    self._deskew_order.get("order_id", "") if self._deskew_order else ""
                ):
                    self._deskew_order = None
                else:
                    marketable = self._marketable_price(target_token, target_side)
                    moved = (
                        marketable is not None
                        and abs(marketable - float(self._deskew_order.get("price", 0.0) or 0.0))
                        >= self.tick_size - 1e-9
                    )
                    if moved:
                        self._cancel_quote(qid)
                    if self._deskew_order is not None and qid not in self.active_quotes:
                        self._deskew_order = None
                    if self._deskew_order is not None:
                        return

        if ts - self._last_deskew_attempt_ts < self.deskew_min_interval_s:
            return

        marketable = self._marketable_price(target_token, target_side)
        if marketable is None:
            logger.warning(f"Tier 2 de-skew skipped: no valid {target_side} price for {target_token}")
            return
        target_price = self.round_quote_price(marketable, target_token)

        target_size = min(float(decision.target_size), self.quote_size * 2.0)
        max_quote_shares = float(
            getattr(self.risk_manager.config, "max_quote_size_shares", 0.0) or 0.0
        )
        if max_quote_shares > 0.0:
            target_size = min(target_size, max_quote_shares)
        if target_side == "SELL" and not bool(
            getattr(self.risk_manager.config, "allow_short", False)
        ):
            owned = self.shares_yes if target_token == "YES" else self.shares_no
            target_size = min(target_size, max(0.0, owned))
        if target_size <= 0.0:
            logger.warning(
                f"Tier 2 de-skew skipped: size after caps is {target_size:.4f} for {target_token}"
            )
            return

        self._record_deskew_event(decision, ts)
        taker_fee = self._taker_fee(target_size, target_price)
        logger.warning(
            f"REAL TRADER: EXECUTING TIER 2 EMERGENCY DE-SKEW ({decision.pathway.value}): "
            f"{target_side} {target_size:.2f} {target_token} @ ${target_price:.3f} | "
            f"optimizer savings {decision.synthetic_savings_cents:.3f}c | "
            f"market taker fee ${taker_fee:.4f} ({self.fee_rate_bps}bps) | {decision.reason}"
        )

        token_id = self.asset_id_yes if target_token == "YES" else self.asset_id_no
        res = self.clob_client.place_order(
            token_id=token_id,
            side=target_side,
            price=target_price,
            size=target_size,
            tick_size=self.tick_size,
            neg_risk=self.neg_risk,
            fee_rate_bps=self.fee_rate_bps,
            post_only=False,
        )
        self._last_deskew_attempt_ts = ts
        if not res.success:
            logger.warning(f"Tier 2 taker order rejected: {res.error_message}")
            self._deskew_order = None
            return

        self._reserve_cash_for_buy(target_size, target_price)

        qid = f"DESKEW_{target_token}_{int(ts * 1000)}"
        self.active_quotes[qid] = {
            "quote_id": qid,
            "order_id": res.order_id,
            "market": self.condition_id,
            "token_side": target_token,
            "side": target_side,
            "price": target_price,
            "size": target_size,
            "size_matched": 0.0,
            "queue_ahead": 0.0,
            "score": 0.0,
            "v_queue": 0.0,
            "timestamp": ts,
            "status": "LIVE",
            "is_dry_run": res.is_dry_run,
            "is_taker": True,
            "decision_target_price": float(decision.target_price),
        }
        self._deskew_order = {
            "quote_id": qid,
            "order_id": res.order_id,
            "timestamp": ts,
            "price": target_price,
            "size": target_size,
            "token_side": target_token,
            "side": target_side,
        }
        self.locked_cash = self._compute_locked_cash()

    def _execute_tier2_dry_run(self, decision: DeSkewDecision, ts: float) -> None:
        """Simulated tier-2 cross for dry-run only: books the modelled fill."""
        target_token = str(decision.target_token).upper()
        target_side = str(decision.action_side).upper()
        target_size = min(float(decision.target_size), self.quote_size * 2.0)
        target_price = self.round_quote_price(float(decision.target_price), target_token)
        if target_size <= 0.0 or not (0.0 < target_price < 1.0):
            return
        self._record_deskew_event(decision, ts)
        logger.warning(
            f"[DRY-RUN] TIER 2 EMERGENCY DE-SKEW ({decision.pathway.value}): "
            f"{target_side} {target_size:.2f} {target_token} @ ${target_price:.3f} | {decision.reason}"
        )
        fill_rec = RealMoneyFillRecord(
            fill_id=str(uuid.uuid4())[:8],
            timestamp=ts,
            condition_id=self.condition_id,
            token_side=target_token,
            side=target_side,
            fill_price=target_price,
            fill_size=target_size,
            is_maker=False,
            order_id="",
            notes=f"Simulated Tier 2 Taker Fill ({decision.pathway.value}, Dry-Run)",
        )
        self.fills_history.append(fill_rec)
        shares_attr = "shares_yes" if target_token == "YES" else "shares_no"
        shares_before = float(getattr(self, shares_attr, 0.0) or 0.0)
        realized_before = float(self.realized_pnl)
        taker_fee = self._taker_fee(target_size, target_price)
        self._apply_fill(
            target_token,
            target_side,
            target_size,
            target_price,
            fee_usd=taker_fee,
            ts=ts,
            is_taker=True,
        )
        fill_key = f"{fill_rec.fill_id}:{float(ts):.3f}:{target_price:.6f}:{target_size:.6f}"
        self._record_fill_observation(
            fill_key=fill_key,
            ts=ts,
            tok=target_token,
            side=target_side,
            price=target_price,
            size=target_size,
            is_maker=False,
            order_id="",
            fee_usd=taker_fee,
            realized_pnl=float(self.realized_pnl) - realized_before,
            note=fill_rec.notes,
        )
        self._insert_rebalance_row(
            pathway=f"TIER2_{decision.pathway.value}",
            ts=ts,
            crossed_cents=abs(target_price - float(decision.target_price)) * 100.0,
            taker_fee_usd=float(taker_fee),
            gas_usd=0.0,
            inventory_before=shares_before,
            inventory_after=float(getattr(self, shares_attr, 0.0) or 0.0),
            note="simulated tier2 cross (dry-run)",
        )
        if decision.pathway == DeSkewPathway.SYNTHETIC_MERGE:
            self.trigger_ctf_merge()
