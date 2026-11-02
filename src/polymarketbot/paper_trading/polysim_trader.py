"""PolySimulator Cloud Paper Trading Strategy Orchestrator.

Autonomous market-making and liquidity rewards execution engine with:
1. Dynamic market selection based on net yield and trade activity.
2. Binary Complementation Quoting (BUY YES + BUY NO) with independent tick sizes.
3. Live position tracking, fill detection, and SQLite rewards epoch recording.
4. Rich, high-density terminal telemetry output.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    """Orchestrates market making quotes on PolySimulator with terminal telemetry."""

    def __init__(
        self,
        unlocked_capital_usd: float = 100.0,
        client: Optional[PolySimulatorClient] = None,
        db: Optional[PolySimDatabase] = None,
        spread_offset_cents: float = 0.75,
        max_spread_cents: float = 3.0,
        daily_reward_pool_usd: float = 2000.0,
        requote_threshold_cents: float = 2.0,
        poll_interval_seconds: float = 25.0,
        quote_size: Optional[float] = None,
    ) -> None:
        self.unlocked_capital_usd = unlocked_capital_usd
        self.client = client or PolySimulatorClient()
        self.db = db or PolySimDatabase()
        self.spread_offset_cents = spread_offset_cents
        self.max_spread_cents = max_spread_cents
        self.daily_reward_pool = daily_reward_pool_usd
        self.requote_threshold_cents = requote_threshold_cents
        self.poll_interval = poll_interval_seconds

        # Derived sizing (75% quoting capital, 25% reserve) or explicit override
        active_capital = self.unlocked_capital_usd * 0.75
        self.quote_size: float = (
            quote_size if quote_size is not None else min(100.0, max(50.0, round(active_capital)))
        )

        self.active_quotes: dict[str, ActiveQuote] = {}
        self.target_market: Optional[dict[str, Any]] = None
        self.yes_token_id: Optional[str] = None
        self.no_token_id: Optional[str] = None
        self.yes_tick: float = 0.01
        self.no_tick: float = 0.01
        self.last_mid: Optional[float] = None
        self.last_epoch_time: float = 0.0
        self._running: bool = False

    @property
    def target_tick_size(self) -> float:
        return self.yes_tick

    @target_tick_size.setter
    def target_tick_size(self, val: float) -> None:
        self.yes_tick = val
        self.no_tick = val

    def select_market(self, filter_keyword: Optional[str] = None) -> bool:
        """Find and select an active market, prioritizing liquid binary markets."""
        try:
            markets = self.client.get_markets(limit=25, hot_only=True)
            if not markets:
                markets = self.client.get_markets(limit=25)
        except Exception as e:
            log.error(f"Error fetching markets: {e}")
            return False

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
            log.warning("No candidate markets found matching criteria.")
            return False

        # Pick top active market
        self.target_market = candidates[0]
        self.yes_token_id = self.target_market["clob_token_ids"][0]
        self.no_token_id = self.target_market["clob_token_ids"][1]

        # Fetch independent tick sizes with polite rate limit pause
        self.yes_tick = self.client.get_tick_size(self.yes_token_id)
        time.sleep(0.5)
        self.no_tick = self.client.get_tick_size(self.no_token_id)

        if "reward_pool" in self.target_market:
            self.daily_reward_pool = float(self.target_market["reward_pool"])

        # Record autonomous decision
        reason = (
            f"Autonomous Selection: Concentrating ${self.unlocked_capital_usd * 0.75:.2f} quoting capital "
            f"on '{self.target_market.get('question')[:40]}...' | Sized Q*={self.quote_size:.0f} shares."
        )
        self.db.record_decision(
            decision_type="MARKET_SELECTION",
            condition_id=self.target_market.get("condition_id", ""),
            market_question=self.target_market.get("question", ""),
            allocated_capital=self.unlocked_capital_usd * 0.75,
            quote_size=self.quote_size,
            rationale=reason,
            net_yield_est=self.daily_reward_pool * 0.05,
        )

        return True

    def calculate_binary_quotes(self, mid_price: float) -> Tuple[float, float]:
        """Compute quantized BUY prices for YES and NO using binary complementation."""
        half_spread = self.spread_offset_cents / 100.0
        raw_yes_bid = max(0.01, mid_price - half_spread)
        raw_no_bid = max(0.01, (1.0 - mid_price) - half_spread)

        yes_bid = round(round(raw_yes_bid / self.yes_tick) * self.yes_tick, 4)
        no_bid = round(round(raw_no_bid / self.no_tick) * self.no_tick, 4)
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
                mid = round((float(bids[-1]["price"]) + float(asks[-1]["price"])) / 2.0, 4)
            else:
                return

        needs_requote = False
        if self.last_mid is None:
            needs_requote = True
        elif abs(mid - self.last_mid) * 100.0 >= self.requote_threshold_cents:
            needs_requote = True
        elif len(self.active_quotes) < 2:
            needs_requote = True

        if time.time() < getattr(self, "_quote_cooldown_until", 0.0):
            return

        if not needs_requote:
            return

        # Dynamically size quote based on actual available cash
        bal = self.client.get_balance()
        available_cash = max(0.0, bal.balance)
        unit_cost = yes_bid + no_bid

        max_affordable_shares = int((available_cash * 0.90) / unit_cost) if unit_cost > 0 else 0
        actual_size = min(self.quote_size, float(max_affordable_shares))

        if actual_size < 10.0:
            log.warning(
                f"Available cash (${available_cash:.2f}) insufficient for two-sided quoting "
                f"(needs ${unit_cost * 10:.2f} for 10 shares). Holding existing inventory."
            )
            self._quote_cooldown_until = time.time() + 30.0
            return

        self.cancel_all_quotes()

        yes_bid, no_bid = self.calculate_binary_quotes(mid)
        self.last_mid = mid

        cond_id = self.target_market.get("condition_id", "") if self.target_market else ""

        # Post BUY YES
        r_yes = self.client.place_order(
            token_id=self.yes_token_id,
            side="BUY",
            size=actual_size,
            price=yes_bid,
            order_type="GTC",
            tick_size=self.yes_tick,
        )
        if r_yes.success and r_yes.order_id:
            self.active_quotes[r_yes.order_id] = ActiveQuote(
                order_id=r_yes.order_id,
                token_id=self.yes_token_id,
                outcome="Yes",
                side="BUY",
                price=yes_bid,
                size=actual_size,
            )
            self.db.record_order(
                order_id=r_yes.order_id,
                token_id=self.yes_token_id,
                side="BUY",
                price=yes_bid,
                size=actual_size,
                status="live",
                market_id=cond_id,
                outcome="Yes",
            )
        elif not r_yes.success:
            log.warning(f"BUY YES rejected: {r_yes.error_msg}. Cooling down quotes.")
            self._quote_cooldown_until = time.time() + 30.0
            return

        time.sleep(0.6)  # rate limit courtesy

        # Post BUY NO
        r_no = self.client.place_order(
            token_id=self.no_token_id,
            side="BUY",
            size=actual_size,
            price=no_bid,
            order_type="GTC",
            tick_size=self.no_tick,
        )
        if r_no.success and r_no.order_id:
            self.active_quotes[r_no.order_id] = ActiveQuote(
                order_id=r_no.order_id,
                token_id=self.no_token_id,
                outcome="No",
                side="BUY",
                price=no_bid,
                size=actual_size,
            )
            self.db.record_order(
                order_id=r_no.order_id,
                token_id=self.no_token_id,
                side="BUY",
                price=no_bid,
                size=actual_size,
                status="live",
                market_id=cond_id,
                outcome="No",
            )
        elif not r_no.success:
            log.warning(f"BUY NO rejected: {r_no.error_msg}. Cooling down quotes.")
            self._quote_cooldown_until = time.time() + 30.0

    def cancel_all_quotes(self) -> None:
        """Cancel resting quotes and update SQLite."""
        if not self.active_quotes:
            return
        for oid in list(self.active_quotes.keys()):
            self.client.cancel_order(oid)
            self.db.update_order_status(oid, "cancelled")
            time.sleep(0.4)
        self.active_quotes.clear()

    def sync_open_orders(self) -> list[str]:
        """Sync open orders to detect fills and notify."""
        filled_events = []
        try:
            open_orders = self.client.get_open_orders()
            remote_ids = {str(o.get("order_id") or o.get("id")) for o in open_orders}

            for oid in list(self.active_quotes.keys()):
                if oid not in remote_ids:
                    quote = self.active_quotes.pop(oid)
                    self.db.update_order_status(oid, "matched")
                    evt = f"Order #{oid} FILLED: {quote.size:.0f} shares {quote.outcome} @ ${quote.price:.3f}!"
                    filled_events.append(evt)
        except Exception as e:
            log.warning(f"Failed to sync open orders: {e}")
        return filled_events

    def evaluate_reward_epoch(self) -> Optional[float]:
        """Evaluate 1-minute reward sampling epoch and record to SQLite."""
        if not self.yes_token_id or not self.target_market:
            return None

        now = time.time()
        if now - self.last_epoch_time < 60.0:
            return None
        self.last_epoch_time = now

        try:
            book = self.client.get_book(self.yes_token_id, depth=15)
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            mid = self.last_mid or 0.50

            comp_score, _, _ = compute_competing_depth_score(bids, asks, mid, self.max_spread_cents)
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
            return epoch_payout
        except Exception as e:
            log.warning(f"Error evaluating reward epoch: {e}")
            return None

    def print_terminal_status(self, fills: list[str]) -> None:
        """Render clean, high-density terminal status display."""
        try:
            bal = self.client.get_balance()
            positions = self.client.get_positions(status="open")
        except Exception:
            return

        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        daily = self.db.get_daily_summary()
        accrued = daily.get("accumulated_reward_usd", 0.0) if daily else 0.0
        payout = daily.get("payout_usd", 0.0) if daily else 0.0
        floor_cleared = daily.get("cleared_floor", 0) if daily else 0
        total_epochs = daily.get("total_epochs", 0) if daily else 0

        # Calculate collateral held
        collateral_in_use = sum(q.size * q.price for q in self.active_quotes.values())

        print("\n" + "=" * 88)
        print(f" [POLYMARKET AUTONOMOUS TRADER] - {now_utc} | Mode: PolySimulator Cloud")
        print("-" * 88)
        print(f" Account Equity: ${bal.total_value:.2f} USD | Cash Balance: ${bal.balance:.2f} | Collateral In-Use: ${collateral_in_use:.2f}")
        print(f" Unlocked Capital Cap: ${self.unlocked_capital_usd:.2f} | Active Sizing Q*: {self.quote_size:.0f} shares")

        if self.target_market:
            q_text = self.target_market.get("question", "")
            mid_str = f"${self.last_mid:.3f}" if self.last_mid else "N/A"
            print(f"\n TARGET MARKET: '{q_text}'")
            print(f" Midpoint: {mid_str} | YES Tick: {self.yes_tick} | NO Tick: {self.no_tick} | Reward Pool: ${self.daily_reward_pool:,.0f}/day")

        print("\n ACTIVE RESTING QUOTES (PolySimulator Matching Engine):")
        if self.active_quotes:
            for q in self.active_quotes.values():
                val = q.size * q.price
                print(f"   • [ORDER #{q.order_id}] BUY {q.outcome:<3} : {q.size:.0f} shares @ ${q.price:.3f} (${val:.2f} collateral) [PENDING]")
        else:
            print("   (Placing resting quotes...)")

        print("\n OPEN POSITIONS (Active Portfolio on polysimulator.com):")
        if positions:
            for p in positions:
                qnt = float(p.get("quantity", 0))
                avg_p = float(p.get("avg_entry_price", 0))
                cur_p = float(p.get("current_price", 0))
                unr = float(p.get("unrealized_pnl", 0))
                m_q = p.get("market_question", "")[:35]
                print(f"   • [{p.get('outcome')} on '{m_q}'] {qnt:.1f} shares @ avg ${avg_p:.3f} | Current: ${cur_p:.3f} | Unrealized PnL: ${unr:+.2f}")
        else:
            print("   (0 token inventory held — quotes resting as maker liquidity)")

        print("\n REWARDS ACCRUAL (Quadratic Scoring Engine in SQLite):")
        user_sc = compute_quadratic_score(self.max_spread_cents, self.spread_offset_cents, self.quote_size * 2)
        floor_label = f"CLEARED (${payout:.2f})" if floor_cleared else f"Accruing (${accrued:.3f}/$1.00)"
        print(f"   • Quote Score S(v, s): {user_sc:.1f} pts | Daily Accrued: ${accrued:.3f} | $1.00 Floor: {floor_label} | Epochs: {total_epochs}/1440")

        gov = getattr(self.client, "governor", None)
        if gov:
            st = gov.get_status()
            print("\n RATE LIMIT & 100K MONTHLY BUDGET GOVERNOR:")
            print(
                f"   • Minute: {st.requests_last_minute}/40 RPM | "
                f"Hour: {st.requests_last_hour}/138 RPH (Remaining: {st.hourly_budget_remaining}) | "
                f"Day: {st.requests_today}/3,333 RPD"
            )
            print(
                f"   • Daily Orders Placed: {st.orders_placed_today}/90 | "
                f"Order Placement Permitted: {'YES' if st.can_place_order else 'NO (Daily Cap)'}"
            )

        if fills:
            print("\n " + "!" * 86)
            for f in fills:
                print(f" >>> [EXECUTION EVENT] {f}")
            print(" " + "!" * 86)

        print("=" * 88)

    def manage_positions(self) -> list[str]:
        """Autonomously manage open positions: take profit, exit or rebalance."""
        events = []
        if not self.target_market:
            return events

        cond_id = self.target_market.get("condition_id", "")
        try:
            positions = self.client.get_positions(status="open")
            for p in positions:
                if p.get("market_id") == cond_id:
                    qty = float(p.get("quantity", 0))
                    entry = float(p.get("avg_entry_price", 0))
                    current = float(p.get("current_price", 0))
                    outcome = p.get("outcome", "")

                    if qty <= 0:
                        continue

                    # If position has >= +0.5c profit, lock in profit!
                    profit_cents = (current - entry) * 100.0
                    token_id = self.yes_token_id if outcome == "Yes" else self.no_token_id
                    tick = self.yes_tick if outcome == "Yes" else self.no_tick

                    if token_id and profit_cents >= 0.5:
                        res = self.client.place_order(
                            token_id=token_id,
                            side="SELL",
                            size=qty,
                            price=current,
                            order_type="GTC",
                            tick_size=tick,
                        )
                        if res.success:
                            msg = f"TAKE PROFIT MATCHED: Sold {qty:.0f} {outcome} @ ${current:.3f} (+{profit_cents:.1f}c PnL)"
                            events.append(msg)
                            self.db.record_decision(
                                decision_type="TAKE_PROFIT",
                                condition_id=cond_id,
                                market_question=self.target_market.get("question", ""),
                                allocated_capital=qty * current,
                                quote_size=qty,
                                rationale=msg,
                                net_yield_est=qty * (current - entry),
                            )
        except Exception as e:
            log.warning(f"Error in manage_positions: {e}")
        return events

    def execute_active_starter_trade(self, size: float = 10.0) -> list[str]:
        """Execute an initial tactical trade if no open positions exist on target market."""
        events = []
        if not self.yes_token_id or not self.target_market:
            return events

        try:
            positions = self.client.get_positions(status="open")
            cond_id = self.target_market.get("condition_id", "")
            has_pos = any(
                p.get("market_id") == cond_id and float(p.get("quantity", 0)) > 0 for p in positions
            )

            if not has_pos:
                book = self.client.get_book(self.yes_token_id, depth=5)
                asks = book.get("asks", [])
                if asks:
                    best_ask = float(asks[-1]["price"])
                    res = self.client.place_order(
                        token_id=self.yes_token_id,
                        side="BUY",
                        size=size,
                        price=best_ask,
                        order_type="GTC",
                        tick_size=self.yes_tick,
                    )
                    if res.success:
                        evt = f"TACTICAL ENTRY: Bought {size:.0f} shares YES @ ${best_ask:.3f} (status: {res.status})"
                        events.append(evt)
                        self.db.record_decision(
                            decision_type="TACTICAL_ENTRY",
                            condition_id=cond_id,
                            market_question=self.target_market.get("question", ""),
                            allocated_capital=size * best_ask,
                            quote_size=size,
                            rationale=evt,
                            net_yield_est=0.0,
                        )
        except Exception as e:
            log.warning(f"Error executing starter trade: {e}")
        return events

    def run_loop(self, duration_seconds: Optional[float] = None) -> None:
        """Run the main quoting and monitoring loop with clean terminal printing."""
        self._running = True
        start_time = time.time()

        if not self.select_market():
            log.error("Failed to select an initial market.")
            return

        bal = self.client.get_balance()
        self.db.record_account_snapshot(bal.balance, bal.total_value, bal.unrealized_pnl, 0.0)

        # Seed initial tactical starter trade if empty
        initial_events = self.execute_active_starter_trade(size=10.0)

        try:
            while self._running:
                now = time.time()
                if duration_seconds and (now - start_time) >= duration_seconds:
                    print(f"\n[INFO] Target duration of {duration_seconds}s reached. Exiting cleanly...")
                    break

                # 1. Sync orders to check fills
                fills = self.sync_open_orders()
                if initial_events:
                    fills.extend(initial_events)
                    initial_events = []

                # 2. Manage open positions (take profit / rebalance)
                pos_events = self.manage_positions()
                if pos_events:
                    fills.extend(pos_events)

                # 3. Refresh quotes
                self.refresh_quotes()

                # 4. Evaluate 1-min reward epoch
                self.evaluate_reward_epoch()

                # 5. Print clean terminal status
                self.print_terminal_status(fills)

                time.sleep(self.poll_interval)

        except KeyboardInterrupt:
            print("\n[INFO] Interrupted by user. Shutting down...")
        finally:
            self._running = False
            self.cancel_all_quotes()
            self.client.cancel_all()
            final_bal = self.client.get_balance()
            cum_rewards = self.db.get_total_cumulative_rewards()
            print(f"\n[SESSION COMPLETED] Final Balance: ${final_bal.balance:.2f} | Equity: ${final_bal.total_value:.2f} | Rewards: ${cum_rewards:.2f}\n")
