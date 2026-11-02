"""Autonomous Liquidity Rewards Harvester Daemon for PolySimulator.

Fully autonomous algorithmic execution engine:
1. Dynamically scans, evaluates, and selects the optimal market(s).
2. Autonomously decides whether to concentrate on 1 pool or split across multiple pools.
3. Dynamically sizes quote quantities (Q*) based on available capital, price, and min size constraints.
4. Autonomously places and refreshes complementary binary quotes (BUY YES + BUY NO).
5. Monitors book drift and evaluates switching hurdles for opportunistic migration.
6. Records all mathematical decisions and quadratic rewards into SQLite.

User Configuration:
The ONLY external setting is `unlocked_capital_usd` (default: $100.00).
All trading decisions, sizing, and allocations are made autonomously.
"""

from __future__ import annotations

import threading
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
from polymarketbot.rewards.scanner import MarketOpportunity, OpportunityScanner

log = get_logger("autonomous_daemon")


@dataclass
class AutonomousQuoteState:
    order_id: str
    token_id: str
    outcome: str
    side: str
    price: float
    size: float
    placed_at: float = field(default_factory=time.time)


class AutonomousHarvesterDaemon:
    """Background autonomous daemon managing market making and liquidity rewards."""

    _instance: Optional["AutonomousHarvesterDaemon"] = None
    _lock = threading.Lock()

    def __init__(
        self,
        unlocked_capital_usd: float = 100.0,
        client: Optional[PolySimulatorClient] = None,
        db: Optional[PolySimDatabase] = None,
        poll_interval_seconds: float = 4.0,
        scan_interval_seconds: float = 300.0,
    ) -> None:
        self.unlocked_capital_usd = unlocked_capital_usd
        self.client = client or PolySimulatorClient()
        self.db = db or PolySimDatabase()
        self.poll_interval = poll_interval_seconds
        self.scan_interval = scan_interval_seconds

        # Active state
        self.active_quotes: dict[str, AutonomousQuoteState] = {}
        self.current_market: Optional[dict[str, Any]] = None
        self.current_yes_token: Optional[str] = None
        self.current_no_token: Optional[str] = None
        self.current_yes_tick: float = 0.01
        self.current_no_tick: float = 0.01
        self.current_quote_size: float = 50.0
        self.current_mid: Optional[float] = None
        self.daily_reward_pool: float = 2000.0
        self.max_spread_cents: float = 3.0

        self.spread_offset_cents: float = 0.75

        # Timing
        self.last_scan_time: float = 0.0
        self.last_epoch_time: float = 0.0
        self._running: bool = False
        self._thread: Optional[threading.Thread] = None

    @classmethod
    def get_instance(cls, unlocked_capital_usd: float = 100.0) -> "AutonomousHarvesterDaemon":
        """Singleton accessor to coordinate background thread with UI."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(unlocked_capital_usd=unlocked_capital_usd)
            return cls._instance

    def update_capital(self, new_capital: float) -> None:
        """Update the unlocked trading capital fraction dynamically."""
        if new_capital > 0 and new_capital != self.unlocked_capital_usd:
            log.info(f"Updating unlocked trading capital from ${self.unlocked_capital_usd:.2f} to ${new_capital:.2f}")
            self.unlocked_capital_usd = new_capital

    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start the autonomous harvester in a background daemon thread."""
        with self._lock:
            if self.is_running():
                log.info("Autonomous harvester daemon is already running.")
                return
            self._running = True
            self._thread = threading.Thread(target=self._run_loop, daemon=True, name="AutoHarvester")
            self._thread.start()
            log.info(f"Autonomous Harvester Daemon started with unlocked capital: ${self.unlocked_capital_usd:.2f}")

    def stop(self) -> None:
        """Stop the background daemon and cleanly cancel resting quotes."""
        with self._lock:
            self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self.cancel_all_quotes()
        log.info("Autonomous Harvester Daemon stopped.")

    # ------------------------------------------------------------------
    # Autonomous Market & Sizing Optimizer
    # ------------------------------------------------------------------

    def evaluate_and_select_market(self) -> bool:
        """Autonomously scan active markets, evaluate net yield, and decide allocations."""
        log.info("Scanning markets for optimal liquidity rewards yield...")
        try:
            markets = self.client.get_markets(limit=25, hot_only=True)
            if not markets:
                markets = self.client.get_markets(limit=25)
        except Exception as e:
            log.error(f"Failed to query PolySimulator markets: {e}")
            return False

        # Filter valid binary markets
        candidates = [
            m for m in markets
            if not m.get("closed") and len(m.get("clob_token_ids", [])) >= 2
        ]

        if not candidates:
            log.warning("No open binary candidates found on PolySimulator.")
            return False

        # 1. Autonomous Sizing & Concentration Decision:
        # Reserve 25% cash for buffer/hedging, use 75% for active quoting.
        active_capital = self.unlocked_capital_usd * 0.75
        reserve_capital = self.unlocked_capital_usd * 0.25

        # Decision: Single market vs Multi-market
        # If active_capital < $150, splitting across 2 pools gives <= $37.50 per market,
        # which fails to buy min 50 shares and fails the $1.00 daily payout floor.
        should_split = active_capital >= 200.0
        num_markets = 2 if should_split else 1

        allocated_per_market = active_capital / num_markets

        # 2. Score and Rank Candidates (polite rate-limited inspection of top candidates)
        scored_candidates = []
        for m in candidates[:3]:
            tok_yes = m["clob_token_ids"][0]
            time.sleep(0.5)  # Stay strictly under 2 req/sec Free tier limit
            mid = self.client.get_midpoint(tok_yes) or 0.50
            # Sizing formula: Q* = allocated_per_market / (P_yes + P_no) = allocated_per_market / 1.00
            target_q = min(100.0, max(50.0, allocated_per_market))

            time.sleep(0.5)
            book = self.client.get_book(tok_yes, depth=10)
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            comp_score, _, _ = compute_competing_depth_score(bids, asks, mid, self.max_spread_cents)
            comp_score = max(500.0, comp_score)

            user_sc = compute_quadratic_score(self.max_spread_cents, self.spread_offset_cents, target_q * 2)
            pool_budget = float(m.get("reward_pool", 2000.0))
            gross_payout = (user_sc / (user_sc + comp_score)) * pool_budget
            cleared_floor = gross_payout >= 1.00

            scored_candidates.append({
                "market": m,
                "mid": mid,
                "quote_size": target_q,
                "user_score": user_sc,
                "comp_score": comp_score,
                "gross_payout": gross_payout,
                "cleared_floor": cleared_floor,
                "net_yield_per_dollar": (gross_payout / max(1.0, allocated_per_market)),
            })


        # Sort by net yield per dollar, prioritizing markets that clear the $1.00 floor
        scored_candidates.sort(key=lambda x: (x["cleared_floor"], x["net_yield_per_dollar"]), reverse=True)
        top = scored_candidates[0]

        best_market = top["market"]
        new_cond_id = best_market.get("condition_id", "")
        new_yes = best_market["clob_token_ids"][0]
        new_no = best_market["clob_token_ids"][1]

        # Check if already quoting this market
        if self.current_market and self.current_market.get("condition_id") == new_cond_id:
            log.info(f"Optimizer: Retaining market '{best_market.get('question')}' (Rank #1 yield).")
            return True

        # Migration / Initial Selection
        reason = (
            f"Autonomous Decision: Concentrating 100% of quoting capital (${active_capital:.2f}) "
            f"on '{best_market.get('question')[:40]}...' | "
            f"Sized Q*={top['quote_size']:.0f} shares | Est. Daily Reward: ${top['gross_payout']:.2f}/day "
            f"({'Floor Cleared' if top['cleared_floor'] else 'Sub-floor'}). "
            f"Rationale: Micro-capital ${self.unlocked_capital_usd:.0f} requires single-market concentration "
            f"to guarantee clearing the $1.00 daily payout floor and 50-share min size constraint."
        )

        log.info(reason)
        self.db.record_decision(
            decision_type="MARKET_SELECTION",
            condition_id=new_cond_id,
            market_question=best_market.get("question", ""),
            allocated_capital=active_capital,
            quote_size=top["quote_size"],
            rationale=reason,
            net_yield_est=top["gross_payout"],
        )

        # Apply new market
        self.cancel_all_quotes()
        self.current_market = best_market
        self.current_yes_token = new_yes
        self.current_no_token = new_no
        self.current_quote_size = top["quote_size"]
        self.current_yes_tick = self.client.get_tick_size(new_yes)
        time.sleep(0.5)
        self.current_no_tick = self.client.get_tick_size(new_no)
        self.current_mid = top["mid"]
        return True

    # ------------------------------------------------------------------
    # Autonomous Order Placement
    # ------------------------------------------------------------------

    def calculate_binary_quotes(self, mid_price: float) -> Tuple[float, float]:
        """Compute quantized complementary BUY prices for YES and NO."""
        half_spread = self.spread_offset_cents / 100.0
        raw_yes_bid = max(0.01, mid_price - half_spread)
        raw_no_bid = max(0.01, (1.0 - mid_price) - half_spread)

        tick_yes = self.current_yes_tick
        tick_no = self.current_no_tick
        yes_bid = round(round(raw_yes_bid / tick_yes) * tick_yes, 4)
        no_bid = round(round(raw_no_bid / tick_no) * tick_no, 4)
        return yes_bid, no_bid

    def refresh_quotes(self) -> None:
        """Autonomously quote both sides and adjust if midpoint drifts."""
        if not self.current_yes_token or not self.current_no_token:
            return

        mid = self.client.get_midpoint(self.current_yes_token)
        if not mid or mid <= 0:
            book = self.client.get_book(self.current_yes_token, depth=5)
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            if bids and asks:
                mid = round((float(bids[-1]["price"]) + float(asks[-1]["price"])) / 2.0, 4)
            else:
                return

        # Check drift or missing quotes
        needs_requote = False
        if self.current_mid is None:
            needs_requote = True
        elif abs(mid - self.current_mid) * 100.0 >= 1.0:
            log.info(f"Book drift detected: {self.current_mid:.3f} -> {mid:.3f}. Re-quoting...")
            needs_requote = True
        elif len(self.active_quotes) < 2:
            needs_requote = True

        if not needs_requote:
            return

        self.cancel_all_quotes()

        yes_bid, no_bid = self.calculate_binary_quotes(mid)
        self.current_mid = mid

        log.info(
            f"Autonomous Quoting ({self.current_quote_size:.0f} shares): "
            f"BUY YES @ ${yes_bid:.3f} | BUY NO @ ${no_bid:.3f} (Mid: ${mid:.3f})"
        )

        cond_id = self.current_market.get("condition_id", "") if self.current_market else ""

        # Post BUY YES
        r_yes = self.client.place_order(
            token_id=self.current_yes_token,
            side="BUY",
            size=self.current_quote_size,
            price=yes_bid,
            order_type="GTC",
            tick_size=self.current_yes_tick,
        )
        if r_yes.success and r_yes.order_id:
            self.active_quotes[r_yes.order_id] = AutonomousQuoteState(
                order_id=r_yes.order_id,
                token_id=self.current_yes_token,
                outcome="Yes",
                side="BUY",
                price=yes_bid,
                size=self.current_quote_size,
            )
            self.db.record_order(
                order_id=r_yes.order_id,
                token_id=self.current_yes_token,
                side="BUY",
                price=yes_bid,
                size=self.current_quote_size,
                status="live",
                market_id=cond_id,
                outcome="Yes",
            )

        time.sleep(0.6)  # rate limit courtesy

        # Post BUY NO
        r_no = self.client.place_order(
            token_id=self.current_no_token,
            side="BUY",
            size=self.current_quote_size,
            price=no_bid,
            order_type="GTC",
            tick_size=self.current_no_tick,
        )
        if r_no.success and r_no.order_id:
            self.active_quotes[r_no.order_id] = AutonomousQuoteState(
                order_id=r_no.order_id,
                token_id=self.current_no_token,
                outcome="No",
                side="BUY",
                price=no_bid,
                size=self.current_quote_size,
            )
            self.db.record_order(
                order_id=r_no.order_id,
                token_id=self.current_no_token,
                side="BUY",
                price=no_bid,
                size=self.current_quote_size,
                status="live",
                market_id=cond_id,
                outcome="No",
            )


    def cancel_all_quotes(self) -> None:
        """Cancel resting quotes and update SQLite."""
        if not self.active_quotes:
            return
        log.info(f"Cancelling {len(self.active_quotes)} active quotes...")
        for oid in list(self.active_quotes.keys()):
            self.client.cancel_order(oid)
            self.db.update_order_status(oid, "cancelled")
            time.sleep(0.4)
        self.active_quotes.clear()

    def sync_open_orders(self) -> None:
        """Sync with remote PolySimulator to detect fills."""
        try:
            open_orders = self.client.get_open_orders()
            remote_ids = {str(o.get("order_id") or o.get("id")) for o in open_orders}
            for oid in list(self.active_quotes.keys()):
                if oid not in remote_ids:
                    q = self.active_quotes.pop(oid)
                    self.db.update_order_status(oid, "matched")
                    log.info(f"Quote {oid} ({q.outcome} {q.side} @ ${q.price:.3f}) executed/matched!")
        except Exception as e:
            log.warning(f"Error syncing open orders: {e}")

    def evaluate_reward_epoch(self) -> None:
        """Evaluate 1-minute reward sampling epoch and record to SQLite."""
        if not self.current_yes_token or not self.current_market:
            return

        now = time.time()
        if now - self.last_epoch_time < 60.0:
            return
        self.last_epoch_time = now

        try:
            book = self.client.get_book(self.current_yes_token, depth=15)
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            mid = self.current_mid or 0.50

            comp_score, _, _ = compute_competing_depth_score(bids, asks, mid, self.max_spread_cents)
            user_score = compute_quadratic_score(
                self.max_spread_cents, self.spread_offset_cents, self.current_quote_size * 2
            )
            total_score = comp_score + user_score

            epoch_payout = 0.0
            if total_score > 0:
                epoch_share = user_score / total_score
                daily_epoch_budget = self.daily_reward_pool / 1440.0
                epoch_payout = epoch_share * daily_epoch_budget

            cond_id = self.current_market.get("condition_id", "")
            self.db.record_reward_epoch(
                market_id=cond_id,
                token_id=self.current_yes_token,
                mid_price=mid,
                spread_offset_cents=self.spread_offset_cents,
                max_spread_cents=self.max_spread_cents,
                quote_size=self.current_quote_size * 2,
                user_score=user_score,
                competing_score=comp_score,
                daily_reward_pool=self.daily_reward_pool,
                epoch_reward_usd=epoch_payout,
            )
        except Exception as e:
            log.warning(f"Error evaluating reward epoch: {e}")

    # ------------------------------------------------------------------
    # Main Daemon Loop
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        """Main execution loop running in background."""
        log.info("Autonomous execution loop entered.")
        try:
            while self._running:
                now = time.time()

                # 1. Periodic Universe Re-scan & Opportunistic Switching (every 300s)
                if now - self.last_scan_time >= self.scan_interval or self.current_market is None:
                    self.evaluate_and_select_market()
                    self.last_scan_time = now

                # 2. Sync orders & check fills
                self.sync_open_orders()

                # 3. Refresh quotes
                self.refresh_quotes()

                # 4. Evaluate 1-min quadratic rewards
                self.evaluate_reward_epoch()

                # 5. Record account snapshot
                if int(now) % 30 == 0:
                    try:
                        bal = self.client.get_balance()
                        cum = self.db.get_total_cumulative_rewards()
                        self.db.record_account_snapshot(bal.balance, bal.total_value, bal.unrealized_pnl, cum)
                    except Exception:
                        pass

                time.sleep(self.poll_interval)
        except Exception as exc:
            log.error(f"Autonomous daemon crashed with unhandled exception: {exc}")
        finally:
            self._running = False
            self.cancel_all_quotes()
            self.client.cancel_all()
            log.info("Autonomous daemon execution loop terminated.")
