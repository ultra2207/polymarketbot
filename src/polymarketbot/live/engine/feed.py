"""Feed and market event ingestion."""

from __future__ import annotations

import time
import uuid
from typing import Any, Optional

from polymarketbot.live.engine.markout import RealMoneyFillRecord
from polymarketbot.logs import get_logger

logger = get_logger("real_money_engine")


class FeedMixin:
    """L2 book, trade print and book-liveness ingestion."""

    def on_book_update(
        self,
        token_side: str,
        bids: list[Any],
        asks: list[Any],
        timestamp: Optional[float] = None,
    ) -> None:
        """Process incoming L2 order book updates from LivePolymarketFeed."""
        ts = timestamp or time.time()
        tok = str(token_side).upper()

        def _get_price(lvl: Any) -> float:
            if hasattr(lvl, "price"):
                return float(lvl.price)
            if isinstance(lvl, dict):
                return float(lvl.get("price", 0.0) or 0.0)
            return float(lvl[0]) if isinstance(lvl, (list, tuple)) else 0.0

        def _get_size(lvl: Any) -> float:
            if hasattr(lvl, "size"):
                return float(lvl.size)
            if isinstance(lvl, dict):
                return float(lvl.get("size", 100.0) or 100.0)
            return float(lvl[1]) if isinstance(lvl, (list, tuple)) and len(lvl) > 1 else 100.0

        if bids:
            self._best_bids[tok] = max(_get_price(b) for b in bids)
        if asks:
            self._best_asks[tok] = min(_get_price(a) for a in asks)
        self._book_ts[tok] = float(ts)

        # Update microstructure sentinel
        micro = self.microstructure_yes if tok == "YES" else self.microstructure_no
        b_val = self._best_bids.get(tok, 0.49)
        a_val = self._best_asks.get(tok, 0.51)
        bid_sz = sum(_get_size(b) for b in bids) if bids else 100.0
        ask_sz = sum(_get_size(a) for a in asks) if asks else 100.0
        try:
            micro.update(
                best_bid_price=b_val,
                best_bid_size=bid_sz,
                best_ask_price=a_val,
                best_ask_size=ask_sz,
                timestamp=ts,
            )
        except Exception as e:
            logger.debug(f"Microstructure sentinel update: {e}")

        # Price-jump detection uses trusted book mids, never public trade prints.
        # A jump enters a bounded per-asset cooldown; it never halts the engine.
        mid = self._fresh_mid(tok)
        if mid is not None:
            asset_id = self.asset_id_yes if tok == "YES" else self.asset_id_no
            try:
                healthy, message = self.risk_manager.update_market_price(
                    asset_id=asset_id,
                    condition_id=self.condition_id,
                    mid_price=mid,
                    timestamp=ts,
                )
            except Exception as e:
                logger.warning(f"Failed to record mid price for {asset_id}: {e}")
            else:
                if not healthy:
                    logger.warning(
                        f"[PRICE JUMP COOLDOWN] {tok}: {message} "
                        "(quotes cool down, engine is not halted)"
                    )

        # Circuit breaker / risk halt guard
        if self.risk_manager.portfolio_state.is_halted or self.risk_manager.portfolio_state.kill_switch_active:
            if self.active_quotes:
                self.cancel_all_quotes()
            return
        if not self.trading_enabled or self.market_closed:
            return

        # Re-evaluate quotes
        self.evaluate_quotes(timestamp=ts)

    def refresh_book_liveness(self, timestamp: Optional[float] = None) -> None:
        """Mark known books current while the feed connection is alive.

        The CLOB stream sends a book only when it changes, so a quiet market can
        go many minutes without a frame while its snapshot is still the current
        book. Staleness therefore tracks feed liveness, not frame recency; a book
        that has never been received stays stale.
        """
        ts = float(timestamp or time.time())
        for tok in ("YES", "NO"):
            if (
                tok in self._best_bids
                and tok in self._best_asks
                and self._best_bids[tok] > 0.0
                and self._best_asks[tok] > 0.0
            ):
                self._book_ts[tok] = ts

    def on_trade_update(
        self,
        token_side: str,
        trade_price: float,
        trade_size: float,
        trade_side: str,
        timestamp: Optional[float] = None,
    ) -> None:
        """Process a public trade print. Live fills are reconciled from the CLOB, not inferred here."""
        ts = timestamp or time.time()
        tok = str(token_side).upper()
        self.risk_manager.record_heartbeat(ts)

        if not self.dry_run:
            # Public prints between third parties do not fill our resting orders.
            return
        if not self.trading_enabled or self.market_closed:
            return

        # DRY-RUN / SIMULATED MODE ONLY: match public prints against mock quotes.
        taker_side = str(trade_side).upper()
        for qid, qdata in list(self.active_quotes.items()):
            if qdata.get("token_side") != tok:
                continue
            side = str(qdata.get("side", "")).upper()
            quote_price = float(qdata.get("price", 0.0) or 0.0)
            quote_size = float(qdata.get("size", 0.0) or 0.0)
            matched = float(qdata.get("size_matched", 0.0) or 0.0)
            remaining = quote_size - matched
            if remaining <= 1e-9:
                self._discard_dry_run_quote(qid)
                continue

            crossed = (
                side == "BUY"
                and taker_side == "SELL"
                and trade_price <= quote_price
            ) or (
                side == "SELL"
                and taker_side == "BUY"
                and trade_price >= quote_price
            )
            if not crossed:
                continue

            fill_size = min(remaining, float(trade_size))
            if fill_size <= 1e-9:
                continue
            rebate = self._maker_rebate(fill_size, quote_price)
            fill_rec = RealMoneyFillRecord(
                fill_id=str(uuid.uuid4())[:8],
                timestamp=ts,
                condition_id=self.condition_id,
                token_side=tok,
                side=side,
                fill_price=quote_price,
                fill_size=fill_size,
                is_maker=True,
                order_id=qdata.get("order_id", ""),
                notes="Simulated maker fill matched against market trade (Dry-Run)",
                maker_rebate_usd=rebate,
            )
            self.fills_history.append(fill_rec)
            self._apply_fill(
                tok, side, fill_size, quote_price, ts=ts, is_taker=False, maker_rebate_usd=rebate
            )
            qdata["size_matched"] = matched + fill_size
            if quote_size - qdata["size_matched"] <= 1e-9:
                self._discard_dry_run_quote(qid)
            self.evaluate_quotes(timestamp=ts)
            self.flush_ledger()

    def _discard_dry_run_quote(self, qid: str) -> None:
        """Remove a fully simulated quote and its mock CLOB order."""
        qdata = self.active_quotes.pop(qid, None)
        if not qdata:
            return
        self._release_reserved_cash(qdata)
        order_id = str(qdata.get("order_id", "") or "")
        if order_id:
            self.clob_client.cancel_order(order_id)
