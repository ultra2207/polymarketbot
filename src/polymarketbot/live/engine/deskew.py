"""Fees, marketable pricing and Tier 2 de-skew execution."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from polymarketbot.fees import FeeScheduleUnreadable, maker_fee_rebate, taker_fee
from polymarketbot.live.engine.markout import RealMoneyFillRecord
from polymarketbot.logs import get_logger
from polymarketbot.strategy.onchain.ctf import load_measured_block_interval_s
from polymarketbot.strategy.reward.deskew import DeSkewDecision, DeSkewPathway

logger = get_logger("real_money_engine")

# The same persisted measurements the rest of the stack prices flights with.
CLOB_RTT_PATH = Path("data/derived/clob_rtt.json")
LOCAL_LATENCY_PATH = Path("data/derived/local_latency.json")


def persisted_clob_rtt_s() -> float | None:
    """Persisted CLOB round trip p50, or ``None`` when nothing was measured.

    ``clob_rtt.json`` is the latency probe's own sample; ``local_latency.json``
    is its full report, whose ``clob_time`` endpoint is the legacy reading.
    """
    try:
        payload = json.loads(CLOB_RTT_PATH.read_text(encoding="utf-8"))
        p50_ms = payload.get("p50_ms")
        if isinstance(p50_ms, (int, float)) and float(p50_ms) > 0.0:
            return float(p50_ms) / 1000.0
    except (OSError, ValueError, TypeError):
        pass
    try:
        payload = json.loads(LOCAL_LATENCY_PATH.read_text(encoding="utf-8"))
        endpoints = payload.get("endpoints") or {}
        ms = (endpoints.get("clob_time") or {}).get("ttfb_p50_ms")
        if isinstance(ms, (int, float)) and float(ms) > 0.0:
            return float(ms) / 1000.0
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


class DeskewMixin:
    """Tier 2 emergency de-skew and the fee helpers it uses."""

    def _taker_fee(self, size: float, price: float) -> float:
        """Exchange taker fee for a fill, from this market's measured schedule."""
        if size <= 0.0 or price <= 0.0:
            return 0.0
        schedule = getattr(self, "fee_schedule", None)
        if schedule is None:
            raise FeeScheduleUnreadable(
                "no fee schedule on the engine; refusing to price a taker fill as free"
            )
        return taker_fee(size, price, schedule)

    def _maker_rebate(self, size: float, price: float) -> float:
        """Exchange maker rebate for a resting maker fill.

        The venue rebate rate is the schedule's ``rebate_rate``; a caller may
        pass a measured override. There is no percentage default.
        """
        if size <= 0.0 or price <= 0.0:
            return 0.0
        schedule = getattr(self, "fee_schedule", None)
        if schedule is None:
            raise FeeScheduleUnreadable(
                "no fee schedule on the engine; refusing to book a maker rebate"
            )
        return maker_fee_rebate(
            price,
            size,
            schedule,
            maker_rebate_share_pct=getattr(self, "maker_rebate_share_pct", None),
        )

    def _measured_clob_rtt_s(self) -> float | None:
        """This engine's own last placement round trip, else the persisted p50."""
        last_ms = getattr(self, "_last_clob_rtt_ms", None)
        if last_ms is not None and float(last_ms) > 0.0:
            return float(last_ms) / 1000.0
        return persisted_clob_rtt_s()

    def _taker_delay_s(self) -> float:
        """Published taker hold this market applies to a marketable order, or 0.

        The venue holds a marketable order (250 ms on selected crypto/finance
        up-down markets, ``site/concepts/order-lifecycle.md``) and the order is
        not cancellable while it is in flight, so the landing decision has to
        wait for it as well as the round trip. A market that applies a hold but
        publishes no duration refuses the cross: treating it as zero is the
        fabricated latency this engine no longer allows.
        """
        read = getattr(self.clob_client, "get_taker_delay_s", None)
        if not callable(read):
            return 0.0
        applies, delay_s = read(self.condition_id)
        if applies and delay_s is None:
            logger.error(
                f"Tier 2 cross refused: {self.condition_id[:12]} applies a taker delay "
                "but publishes no duration"
            )
            raise ValueError("taker delay applies but is unmeasured")
        return float(delay_s or 0.0)

    def _await_tier2_landing(self, pathway: DeSkewPathway) -> float | None:
        """Pay the measured flight before the cross can land; ``None`` = refused.

        A taker order changes nothing at decision time: it first travels the
        measured CLOB round trip. A synthetic merge also waits for the measured
        Polygon block interval, because the merge is an on-chain call. An
        unmeasured flight refuses the cross instead of executing it at 0 ms.
        """
        rtt = self._measured_clob_rtt_s()
        if rtt is None:
            logger.error(
                "Tier 2 cross refused: no measured CLOB round trip "
                f"({CLOB_RTT_PATH} / {LOCAL_LATENCY_PATH} carry no p50)"
            )
            return None
        flight = float(rtt)
        time.sleep(flight)
        try:
            hold = self._taker_delay_s()
        except ValueError:
            return None
        if hold > 0.0:
            time.sleep(hold)
            flight += hold
        if pathway == DeSkewPathway.SYNTHETIC_MERGE:
            block = load_measured_block_interval_s()
            if block is None:
                logger.error(
                    "Tier 2 synthetic merge refused: the Polygon block interval is "
                    "unmeasured, so the merge cannot be given a landing time"
                )
                return None
            time.sleep(float(block))
            flight += float(block)
        logger.warning(
            f"Tier 2 flight ({pathway.value}): submitted -> landing {flight:.3f}s "
            f"(clob rtt {rtt:.3f}s + taker delay {hold:.3f}s)"
        )
        return flight

    def _marketable_price(self, token_side: str, side: str) -> float | None:
        bid, ask = self.get_best_bid_ask(token_side)
        price = ask if str(side).upper() == "BUY" else bid
        if price is None or not (0.0 < price < 1.0):
            return None
        return price

    def _tier2_target_size(
        self, decision: DeSkewDecision, target_token: str, target_side: str
    ) -> float:
        """De-skew cross size after every cap the live order must respect.

        Shared by the live and dry-run paths so the simulated cross cannot book
        a size the live engine would have clamped away.
        """
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
        return target_size

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

        # The taker leg cannot land at decision time: pay the measured flight,
        # then price against the book that exists on arrival.
        flight_s = self._await_tier2_landing(decision.pathway)
        if flight_s is None:
            return
        landing_ts = ts + flight_s
        landing_price = self._marketable_price(target_token, target_side)
        if landing_price is None:
            logger.warning(
                f"Tier 2 de-skew skipped: {target_token} book has no valid "
                f"{target_side} price at landing"
            )
            return
        target_price = self.round_quote_price(landing_price, target_token)

        target_size = self._tier2_target_size(decision, target_token, target_side)
        if target_size <= 0.0:
            logger.warning(
                f"Tier 2 de-skew skipped: size after caps is {target_size:.4f} for {target_token}"
            )
            return

        self._record_deskew_event(decision, landing_ts)
        taker_fee = self._taker_fee(target_size, target_price)
        logger.warning(
            f"REAL TRADER: EXECUTING TIER 2 EMERGENCY DE-SKEW ({decision.pathway.value}): "
            f"{target_side} {target_size:.2f} {target_token} @ ${target_price:.3f} | "
            f"optimizer savings {decision.synthetic_savings_cents:.3f}c | "
            f"market taker fee ${taker_fee:.4f} (rate {self.fee_schedule.rate}) | "
            f"flight {flight_s:.3f}s | {decision.reason}"
        )

        token_id = self.asset_id_yes if target_token == "YES" else self.asset_id_no
        place_started = time.perf_counter()
        res = self.clob_client.place_order(
            token_id=token_id,
            side=target_side,
            price=target_price,
            size=target_size,
            tick_size=self.tick_size,
            neg_risk=self.neg_risk,
            fee_rate_bps=self.fee_rate_bps,
            min_order_usd=self.min_order_usd,
            post_only=False,
        )
        self._last_clob_rtt_ms = (time.perf_counter() - place_started) * 1000.0
        logger.warning(
            f"Tier 2 landing: submitted -> landed {flight_s:.3f}s | executed "
            f"{target_side} {target_size:.2f} {target_token} @ ${target_price:.3f} | "
            f"place rtt {self._last_clob_rtt_ms:.1f}ms"
        )
        self._last_deskew_attempt_ts = landing_ts
        if not res.success:
            logger.warning(f"Tier 2 taker order rejected: {res.error_message}")
            self._deskew_order = None
            return

        self._reserve_cash_for_buy(target_size, target_price)

        qid = f"DESKEW_{target_token}_{int(landing_ts * 1000)}"
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
            "timestamp": landing_ts,
            "status": "LIVE",
            "is_dry_run": res.is_dry_run,
            "is_taker": True,
            "decision_target_price": float(decision.target_price),
        }
        self._deskew_order = {
            "quote_id": qid,
            "order_id": res.order_id,
            "timestamp": landing_ts,
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
        target_size = self._tier2_target_size(decision, target_token, target_side)
        if target_size <= 0.0:
            return
        # Simulated or not, the cross has to travel: pay the same measured
        # flight and re-read the book that exists on arrival.
        flight_s = self._await_tier2_landing(decision.pathway)
        if flight_s is None:
            return
        landing_ts = ts + flight_s
        landing_price = self._marketable_price(target_token, target_side)
        price = landing_price if landing_price is not None else float(decision.target_price)
        target_price = self.round_quote_price(price, target_token)
        if not (0.0 < target_price < 1.0):
            return
        self._record_deskew_event(decision, landing_ts)
        logger.warning(
            f"[DRY-RUN] TIER 2 EMERGENCY DE-SKEW ({decision.pathway.value}): "
            f"{target_side} {target_size:.2f} {target_token} @ ${target_price:.3f} | "
            f"flight {flight_s:.3f}s | {decision.reason}"
        )
        fill_rec = RealMoneyFillRecord(
            fill_id=str(uuid.uuid4())[:8],
            timestamp=landing_ts,
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
            ts=landing_ts,
            is_taker=True,
        )
        fill_key = f"{fill_rec.fill_id}:{float(landing_ts):.3f}:{target_price:.6f}:{target_size:.6f}"
        self._record_fill_observation(
            fill_key=fill_key,
            ts=landing_ts,
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
            ts=landing_ts,
            crossed_cents=abs(target_price - float(decision.target_price)) * 100.0,
            taker_fee_usd=float(taker_fee),
            gas_usd=0.0,
            inventory_before=shares_before,
            inventory_after=float(getattr(self, shares_attr, 0.0) or 0.0),
            note="simulated tier2 cross (dry-run)",
        )
        if decision.pathway == DeSkewPathway.SYNTHETIC_MERGE:
            self.trigger_ctf_merge()
