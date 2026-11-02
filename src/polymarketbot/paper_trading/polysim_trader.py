"""PolySimulator Cloud Paper Trading Strategy Orchestrator with SQLite Rewards Tracking.

Deploys the Polymarket liquidity rewards harvesting market-maker logic
directly onto PolySimulator's cloud execution engine, while logging all
orders and calculating 1-minute quadratic liquidity reward epochs in a local
SQLite database (data/polysim_paper_trading.db).

Microstructure Architecture:
- Binary Complementation: Quotes both sides by submitting `BUY YES` at (mid - offset)
  and `BUY NO` at ((1.0 - mid) - offset). Both are cash-collateralized, avoiding
  "Insufficient position for limit sell order" errors.
- SQLite Rewards Tracking: Because PolySimulator honestly stubs Polymarket's
  quadratic rewards engine, we sample the resting orders every 60 seconds against
  the live order book, compute the quadratic score S(v, s) = ((v - s)/v)^2 * size,
  and track daily accumulated rewards enforcing the $1.00 payout floor.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from polymarketbot.logs import get_logger
from polymarketbot.paper_trading.polysim_client import PolySimulatorClient
from polymarketbot.paper_trading.polysim_db import PolySimDatabase
from polymarketbot.paper_trading.rewards import (
    compute_competing_depth_score,
    compute_quadratic_score,
)

log = get_logger("polysim_trader")


@dataclass
class ActiveQuote:
    order_id: str
    token_id: str
    outcome: str
    side: str
    price: float
    size: float
    placed_at: float = field(default_factory=time.time)


class PolySimulatorTrader:
    """Orchestrates market making quotes on PolySimulator with SQLite rewards."""

    def __init__(
        self,
        client: Optional[PolySimulatorClient] = None,
        db: Optional[PolySimDatabase] = None,
        quote_size: float = 50.0,
        spread_offset_cents: float = 0.75,
        max_spread_cents: float = 3.0,
        daily_reward_pool_usd: float = 2000.0,
        requote_threshold_cents: float = 1.0,
        poll_interval_seconds: float = 3.0,
    ) -> None:
        self.client = client or PolySimulatorClient()
        self.db = db or PolySimDatabase()
        self.quote_size = quote_size
        self.spread_offset_cents = spread_offset_cents
        self.max_spread_cents = max_spread_cents
        self.daily_reward_pool = daily_reward_pool_usd
        self.requote_threshold_cents = requote_threshold_cents
        self.poll_interval = poll_interval_seconds

        self.active_quotes: dict[str, ActiveQuote] = {}
        self.target_market: Optional[dict[str, Any]] = None
        self.yes_token_id: Optional[str] = None
        self.no_token_id: Optional[str] = None
        self.target_tick_size: float = 0.01
        self.last_mid: Optional[float] = None
        self.last_epoch_time: float = 0.0
        self._running: bool = False

    def select_market(self, filter_keyword: Optional[str] = None) -> bool:
        """Find and select an active, open market with valid binary CLOB tokens."""
        markets = self.client.get_markets(limit=25, hot_only=True)
        if not markets:
            markets = self.client.get_markets(limit=25)

        candidates = []
        for m in markets:
            if m.get("closed"):
                continue
            tokens = m.get("clob_token_ids", [])
            if len(tokens) < 2:
                continue
            if filter_keyword and filter_keyword.lower() not in m.get("question", "").lower():
                continue
            candidates.append(m)

        if not candidates:
            log.warning("No open candidate markets with >= 2 tokens found matching criteria.")
            return False

        self.target_market = candidates[0]
        self.yes_token_id = self.target_market["clob_token_ids"][0]
        self.no_token_id = self.target_market["clob_token_ids"][1]
        self.target_tick_size = self.client.get_tick_size(self.yes_token_id)

        # Inspect if reward pool configuration is provided in market metadata
        if "reward_pool" in self.target_market:
            self.daily_reward_pool = float(self.target_market["reward_pool"])

        log.info(
            f"Selected target market: '{self.target_market.get('question')}'\n"
            f"  YES Token: {self.yes_token_id[:16]}...\n"
            f"  NO  Token: {self.no_token_id[:16]}...\n"
            f"  Tick Size: {self.target_tick_size}"
        )
        return True

    def calculate_binary_quotes(self, mid_price: float) -> Tuple[float, float]:
        """Compute quantized BUY prices for YES and NO using binary complementation.

        Returns:
            (yes_bid_price, no_bid_price)
        """
        half_spread = self.spread_offset_cents / 100.0

        # YES bid: mid - half_spread
        raw_yes_bid = max(0.01, mid_price - half_spread)
        # NO bid: (1.0 - mid) - half_spread
        raw_no_bid = max(0.01, (1.0 - mid_price) - half_spread)

        tick = self.target_tick_size
        yes_bid = round(round(raw_yes_bid / tick) * tick, 4)
        no_bid = round(round(raw_no_bid / tick) * tick, 4)

        return yes_bid, no_bid

    def refresh_quotes(self) -> None:
        """Check book midpoint and update resting maker quotes via binary complementation."""
        if not self.yes_token_id or not self.no_token_id:
            return

        mid = self.client.get_midpoint(self.yes_token_id)
        if mid is None or mid <= 0:
            book = self.client.get_book(self.yes_token_id, depth=5)
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            if bids and asks:
                best_bid = float(bids[-1]["price"])
                best_ask = float(asks[-1]["price"])
                mid = round((best_bid + best_ask) / 2.0, 4)
            else:
                return

        # Check if requote is needed
        needs_requote = False
        if self.last_mid is None:
            needs_requote = True
        elif abs(mid - self.last_mid) * 100.0 >= self.requote_threshold_cents:
            log.info(f"Midpoint shifted from {self.last_mid:.4f} to {mid:.4f}. Requoting...")
            needs_requote = True
        elif len(self.active_quotes) < 2:
            needs_requote = True

        if not needs_requote:
            return

        # Cancel existing orders
        self.cancel_all_quotes()

        # Compute prices
        yes_bid, no_bid = self.calculate_binary_quotes(mid)
        self.last_mid = mid

        log.info(
            f"Placing quotes ({self.quote_size:.0f} shares): "
            f"BUY YES @ ${yes_bid:.3f} | BUY NO @ ${no_bid:.3f} (Mid: ${mid:.3f})"
        )

        cond_id = self.target_market.get("condition_id") or ""

        # Post BUY YES
        yes_res = self.client.place_order(
            token_id=self.yes_token_id,
            side="BUY",
            size=self.quote_size,
            price=yes_bid,
            order_type="GTC",
            tick_size=self.target_tick_size,
        )
        if yes_res.success and yes_res.order_id:
            self.active_quotes[yes_res.order_id] = ActiveQuote(
                order_id=yes_res.order_id,
                token_id=self.yes_token_id,
                outcome="Yes",
                side="BUY",
                price=yes_bid,
                size=self.quote_size,
            )
            self.db.record_order(
                order_id=yes_res.order_id,
                token_id=self.yes_token_id,
                side="BUY",
                price=yes_bid,
                size=self.quote_size,
                status="live",
                market_id=cond_id,
                outcome="Yes",
            )

        # Courtesy sleep to respect rate limits (2 req/sec)
        time.sleep(0.6)

        # Post BUY NO
        no_res = self.client.place_order(
            token_id=self.no_token_id,
            side="BUY",
            size=self.quote_size,
            price=no_bid,
            order_type="GTC",
            tick_size=self.target_tick_size,
        )
        if no_res.success and no_res.order_id:
            self.active_quotes[no_res.order_id] = ActiveQuote(
                order_id=no_res.order_id,
                token_id=self.no_token_id,
                outcome="No",
                side="BUY",
                price=no_bid,
                size=self.quote_size,
            )
            self.db.record_order(
                order_id=no_res.order_id,
                token_id=self.no_token_id,
                side="BUY",
                price=no_bid,
                size=self.quote_size,
                status="live",
                market_id=cond_id,
                outcome="No",
            )

    def cancel_all_quotes(self) -> None:
        """Cancel all local and remote active quotes and update SQLite."""
        if not self.active_quotes:
            return
        log.info(f"Cancelling {len(self.active_quotes)} active quotes...")
        for oid in list(self.active_quotes.keys()):
            self.client.cancel_order(oid)
            self.db.update_order_status(oid, "cancelled")
            time.sleep(0.5)  # rate limit courtesy
        self.active_quotes.clear()

    def sync_open_orders(self) -> None:
        """Synchronize with remote open orders to detect fills or cancellations."""
        try:
            open_orders = self.client.get_open_orders()
            remote_ids = {str(o.get("order_id") or o.get("id")) for o in open_orders}

            for oid in list(self.active_quotes.keys()):
                if oid not in remote_ids:
                    quote = self.active_quotes.pop(oid)
                    self.db.update_order_status(oid, "matched")
                    log.info(
                        f"Order {oid} ({quote.outcome} {quote.side} @ ${quote.price:.3f}) filled / closed!"
                    )
        except Exception as e:
            log.warning(f"Failed to sync open orders: {e}")

    def evaluate_reward_epoch(self) -> None:
        """Evaluate 1-minute reward sampling epoch and record to SQLite."""
        if not self.yes_token_id or not self.target_market:
            return

        now = time.time()
        if now - self.last_epoch_time < 60.0:
            return
        self.last_epoch_time = now

        try:
            book = self.client.get_book(self.yes_token_id, depth=20)
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            mid = self.last_mid or 0.50

            comp_score, _, _ = compute_competing_depth_score(
                bids, asks, mid, self.max_spread_cents
            )
            # Add user score
            user_score = compute_quadratic_score(
                self.max_spread_cents, self.spread_offset_cents, self.quote_size * 2
            )
            total_score = comp_score + user_score

            epoch_payout = 0.0
            if total_score > 0:
                epoch_share = user_score / total_score
                daily_epoch_budget = self.daily_reward_pool / 1440.0
                epoch_payout = epoch_share * daily_epoch_budget

            cond_id = self.target_market.get("condition_id") or ""
            self.db.record_reward_epoch(
                market_id=cond_id,
                token_id=self.yes_token_id,
                mid_price=mid,
                spread_offset_cents=self.spread_offset_cents,
                max_spread_cents=self.max_spread_cents,
                quote_size=self.quote_size * 2,
                user_score=user_score,
                competing_score=comp_score,
                daily_reward_pool=self.daily_reward_pool,
                epoch_reward_usd=epoch_payout,
            )

            daily = self.db.get_daily_summary()
            accrued = daily.get("accumulated_reward_usd", 0.0) if daily else 0.0
            payout = daily.get("payout_usd", 0.0) if daily else 0.0
            floor_cleared = daily.get("cleared_floor", 0) if daily else 0

            log.info(
                f"[Reward Epoch] User Score: {user_score:.1f} | Comp Depth Score: {comp_score:.1f} | "
                f"Epoch Payout: +${epoch_payout:.4f} | Day Accrued: ${accrued:.2f} "
                f"(Floor >= $1.00: {'CLEARED ($' + f'{payout:.2f})' if floor_cleared else 'Pending'})"
            )
        except Exception as e:
            log.warning(f"Error evaluating reward epoch: {e}")

    def run_loop(self, duration_seconds: Optional[float] = None) -> None:
        """Run the main quoting and monitoring loop."""
        self._running = True
        start_time = time.time()
        log.info("Starting PolySimulator paper trading loop...")

        bal = self.client.get_balance()
        log.info(
            f"Account: Balance=${bal.balance:.2f} {bal.currency}, "
            f"Equity=${bal.total_value:.2f}, Unrealized PnL=${bal.unrealized_pnl:.2f}"
        )
        self.db.record_account_snapshot(bal.balance, bal.total_value, bal.unrealized_pnl, 0.0)

        try:
            while self._running:
                now = time.time()
                if duration_seconds and (now - start_time) >= duration_seconds:
                    log.info(f"Target duration of {duration_seconds}s reached. Exiting cleanly.")
                    break

                self.sync_open_orders()
                self.refresh_quotes()
                self.evaluate_reward_epoch()

                # Telemetry checkpoint every ~30 seconds
                if int(now - start_time) % 30 == 0:
                    bal = self.client.get_balance()
                    positions = self.client.get_positions(status="open")
                    cum_rewards = self.db.get_total_cumulative_rewards()
                    self.db.record_account_snapshot(
                        bal.balance, bal.total_value, bal.unrealized_pnl, cum_rewards
                    )
                    log.info(
                        f"[Heartbeat] Balance=${bal.balance:.2f} | "
                        f"Active Quotes={len(self.active_quotes)} | "
                        f"Positions={len(positions)} | Rewards Paid=${cum_rewards:.2f}"
                    )

                time.sleep(self.poll_interval)

        except KeyboardInterrupt:
            log.info("Interrupted by user. Shutting down...")
        finally:
            self._running = False
            self.cancel_all_quotes()
            self.client.cancel_all()
            final_bal = self.client.get_balance()
            cum_rewards = self.db.get_total_cumulative_rewards()
            log.info(
                f"Paper trader session completed. Final Balance: ${final_bal.balance:.2f}, "
                f"Total Equity: ${final_bal.total_value:.2f}, Cumulative Rewards: ${cum_rewards:.2f}"
            )
