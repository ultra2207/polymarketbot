"""Real-time Paper Trading Engine for Polymarket Liquidity Reward Harvesting.

Implements:
1. Virtual resting limit orders at optimal spread offsets (s = 0.5c - 1.0c).
2. Strict post-only order placement logic.
3. Real-time trade matching, fill execution, inventory, cash, and avg_cost tracking.
4. Avellaneda-Stoikov-style inventory skewing.
5. 1-minute reward sampling epoch simulation (1,440/day) with quadratic scoring.
6. Persistence to data/processed/paper_trading/ledger.json.
"""

from __future__ import annotations

import json
import math
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from polymarketbot.logs import get_logger
from polymarketbot.paper_trading.models import (
    EpochSnapshot,
    MarketConfig,
    MergeEvent,
    Position,
    SimulatedFill,
    TokenConfig,
    VirtualQuote,
)
from polymarketbot.paper_trading.rewards import (
    compute_competing_depth_score,
    compute_epoch_payout,
    compute_quadratic_score,
)

logger = get_logger("paper_trading")


def _parse_price(x: Any) -> float:
    try:
        if isinstance(x, dict):
            return float(x.get("price", 0.0))
        if isinstance(x, (list, tuple)) and len(x) > 0:
            return float(x[0])
    except (ValueError, TypeError):
        pass
    return 0.0


def _parse_size(x: Any) -> float:
    try:
        if isinstance(x, dict):
            return float(x.get("size", 0.0))
        if isinstance(x, (list, tuple)) and len(x) > 1:
            return float(x[1])
    except (ValueError, TypeError):
        pass
    return 0.0


class PaperTradingEngine:
    """Manages virtual liquidity provision, order books, fills, and reward epochs."""

    def __init__(
        self,
        initial_cash_usd: float = 10000.0,
        base_spread_offset_cents: float = 0.75,
        default_quote_size: float = 100.0,
        max_inventory_shares: float = 1000.0,
        max_skew_cents: float = 0.5,
        auto_merge_threshold: float = 100.0,
        merge_gas_fee_usd: float = 0.015,
        ledger_path: str | Path = "data/processed/paper_trading/ledger.json",
    ) -> None:
        self.initial_cash_usd = float(initial_cash_usd)
        self.cash_usd = float(initial_cash_usd)
        self.base_spread_offset_cents = float(base_spread_offset_cents)
        self.default_quote_size = float(default_quote_size)
        self.max_inventory_shares = float(max_inventory_shares)
        self.max_skew_cents = float(max_skew_cents)
        self.auto_merge_threshold = float(auto_merge_threshold)
        self.merge_gas_fee_usd = float(merge_gas_fee_usd)
        self.ledger_path = Path(ledger_path)
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)

        self.start_time = time.time()
        self.epoch_count = 0
        self.total_rewards_earned_usd = 0.0
        self.total_gas_fees_usd = 0.0
        self.total_merges_count = 0

        # Market registry
        self.markets: dict[str, MarketConfig] = {}  # condition_id -> config
        self.asset_to_condition: dict[str, str] = {}  # asset_id -> condition_id
        self.asset_to_outcome: dict[str, str] = {}  # asset_id -> outcome

        # Live book & quoting state
        self.orderbooks: dict[str, dict[str, Any]] = {}  # asset_id -> book info
        self.virtual_quotes: dict[str, dict[str, Optional[VirtualQuote]]] = {}  # asset_id -> {bid, ask}
        self.positions: dict[str, Position] = {}  # asset_id -> Position

        # Metrics & audit trail
        self.fills: list[SimulatedFill] = []
        self.merges: list[MergeEvent] = []
        self.epoch_snapshots: list[EpochSnapshot] = []

    def register_market(self, config: MarketConfig) -> None:
        """Register a target market and its tokens for quoting."""
        self.markets[config.condition_id] = config
        for tok in config.tokens:
            self.asset_to_condition[tok.token_id] = config.condition_id
            self.asset_to_outcome[tok.token_id] = tok.outcome
            if tok.token_id not in self.positions:
                self.positions[tok.token_id] = Position(
                    asset_id=tok.token_id,
                    outcome=tok.outcome,
                )
            if tok.token_id not in self.virtual_quotes:
                self.virtual_quotes[tok.token_id] = {"bid": None, "ask": None}
            if tok.token_id not in self.orderbooks:
                self.orderbooks[tok.token_id] = {
                    "bids": [],
                    "asks": [],
                    "mid": tok.price,
                    "best_bid": max(0.01, tok.price - 0.01),
                    "best_ask": min(0.99, tok.price + 0.01),
                    "spread_cents": 2.0,
                    "tick_size": 0.01,
                    "competing_score": 0.0,
                    "qualifying_bids_usd": 0.0,
                    "qualifying_asks_usd": 0.0,
                    "last_updated": time.time(),
                }
        logger.info(
            f"Registered market: {config.question[:45]} (${config.rate_per_day}/d, "
            f"max_spread={config.rewards_max_spread}c, min_sz={config.rewards_min_size})"
        )

    def update_orderbook(
        self,
        asset_id: str,
        bids: list[dict[str, Any]] | list[list[str]],
        asks: list[dict[str, Any]] | list[list[str]],
        tick_size: float = 0.01,
    ) -> None:
        """Update order book snapshot and recalculate resting virtual quotes."""
        if asset_id not in self.asset_to_condition:
            return

        def parse_price(x: Any) -> float:
            if isinstance(x, dict):
                return float(x.get("price", 0.0))
            return float(x[0]) if isinstance(x, (list, tuple)) else 0.0

        valid_bids = [b for b in bids if _parse_price(b) > 0.0]
        valid_asks = [a for a in asks if _parse_price(a) > 0.0]

        best_bid = max((_parse_price(b) for b in valid_bids), default=0.0)
        best_ask = min((_parse_price(a) for a in valid_asks), default=1.0)

        if best_bid > 0.0 and best_ask < 1.0 and best_ask > best_bid:
            mid = (best_bid + best_ask) / 2.0
            spread = best_ask - best_bid
        elif best_bid > 0.0:
            mid = best_bid + 0.01
            spread = 0.02
        elif best_ask < 1.0:
            mid = best_ask - 0.01
            spread = 0.02
        else:
            mid = 0.50
            spread = 0.02

        cid = self.asset_to_condition[asset_id]
        mkt = self.markets[cid]

        # Calculate competing depth score
        comp_score, b_usd, a_usd = compute_competing_depth_score(
            valid_bids, valid_asks, mid, mkt.rewards_max_spread
        )

        self.orderbooks[asset_id] = {
            "bids": valid_bids,
            "asks": valid_asks,
            "mid": round(mid, 4),
            "best_bid": round(best_bid, 4),
            "best_ask": round(best_ask, 4),
            "spread_cents": round(spread * 100.0, 2),
            "tick_size": tick_size,
            "competing_score": round(comp_score, 4),
            "qualifying_bids_usd": round(b_usd, 2),
            "qualifying_asks_usd": round(a_usd, 2),
            "last_updated": time.time(),
        }

        # Update position mark-to-market
        if asset_id in self.positions:
            self.positions[asset_id].update_mark(mid)

        # Regenerate virtual quotes for this asset
        self.generate_quotes(asset_id)

    def generate_quotes(self, asset_id: str) -> None:
        """Simulate virtual resting limit orders with queue priority and inventory skewing."""
        if asset_id not in self.asset_to_condition:
            return

        cid = self.asset_to_condition[asset_id]
        mkt = self.markets[cid]
        book = self.orderbooks.get(asset_id)
        if not book:
            return

        mid = book["mid"]
        best_bid = book["best_bid"]
        best_ask = book["best_ask"]
        tick = book.get("tick_size", 0.01)
        pos = self.positions[asset_id]
        inv = pos.shares

        # Inventory Skewing:
        inv_ratio = max(-1.0, min(1.0, inv / self.max_inventory_shares))
        skew_cents = inv_ratio * self.max_skew_cents

        s_bid_target = max(0.2, self.base_spread_offset_cents + skew_cents)
        s_ask_target = max(0.2, self.base_spread_offset_cents - skew_cents)

        # Raw desired limit prices
        raw_bid_price = mid - (s_bid_target / 100.0)
        raw_ask_price = mid + (s_ask_target / 100.0)

        # Round to valid tick size
        bid_price = round(math.floor(raw_bid_price / tick) * tick, 4)
        ask_price = round(math.ceil(raw_ask_price / tick) * tick, 4)

        # Post-Only Logic Enforcement:
        post_only_bid = True
        if bid_price >= best_ask:
            bid_price = round(min(best_bid, best_ask - tick), 4)
            post_only_bid = (bid_price < best_ask)

        post_only_ask = True
        if ask_price <= best_bid:
            ask_price = round(max(best_ask, best_bid + tick), 4)
            post_only_ask = (ask_price > best_bid)

        # Size logic: ensure at least rewards_min_size
        quote_size = max(mkt.rewards_min_size, self.default_quote_size)

        # If inventory is maxed out, pause buying or selling
        bid_size = quote_size if inv < self.max_inventory_shares else 0.0
        ask_size = quote_size if inv > -self.max_inventory_shares else 0.0

        # Calculate actual spread offset in cents
        actual_bid_spread = round((mid - bid_price) * 100.0, 2)
        actual_ask_spread = round((ask_price - mid) * 100.0, 2)

        # Calculate quadratic reward scores S(v, s)
        v = mkt.rewards_max_spread
        bid_score = compute_quadratic_score(v, actual_bid_spread, bid_size) if post_only_bid else 0.0
        ask_score = compute_quadratic_score(v, actual_ask_spread, ask_size) if post_only_ask else 0.0

        existing_quotes = self.virtual_quotes.get(asset_id, {})
        old_bid = existing_quotes.get("bid")
        old_ask = existing_quotes.get("ask")

        book_bids = book.get("bids", [])
        book_asks = book.get("asks", [])

        # Update BID quote
        v_bid = None
        if bid_size > 0 and 0.001 <= bid_price < 1.0 and post_only_bid:
            if old_bid is not None and abs(old_bid.price - bid_price) < 1e-6:
                # Same price level: retain queue priority
                bid_queue_ahead = old_bid.queue_ahead
                current_bid_size = old_bid.size if 0.0 < old_bid.size < bid_size else bid_size
            else:
                # Price changed or new quote: reset queue_ahead to competing volume resting at or better than quote price
                bid_queue_ahead = sum(
                    _parse_size(b) for b in book_bids
                    if _parse_price(b) >= bid_price - 1e-6
                )
                current_bid_size = bid_size

            v_bid = VirtualQuote(
                side="BUY",
                price=bid_price,
                size=current_bid_size,
                spread_from_mid_cents=actual_bid_spread,
                score=bid_score,
                post_only_valid=post_only_bid,
                queue_ahead=round(bid_queue_ahead, 4),
            )

        # Update ASK quote
        v_ask = None
        if ask_size > 0 and 0.001 < ask_price <= 0.999 and post_only_ask:
            if old_ask is not None and abs(old_ask.price - ask_price) < 1e-6:
                # Same price level: retain queue priority
                ask_queue_ahead = old_ask.queue_ahead
                current_ask_size = old_ask.size if 0.0 < old_ask.size < ask_size else ask_size
            else:
                # Price changed or new quote: reset queue_ahead to competing volume resting at or better than quote price
                ask_queue_ahead = sum(
                    _parse_size(a) for a in book_asks
                    if 0.0 < _parse_price(a) <= ask_price + 1e-6
                )
                current_ask_size = ask_size

            v_ask = VirtualQuote(
                side="SELL",
                price=ask_price,
                size=current_ask_size,
                spread_from_mid_cents=actual_ask_spread,
                score=ask_score,
                post_only_valid=post_only_ask,
                queue_ahead=round(ask_queue_ahead, 4),
            )

        self.virtual_quotes[asset_id] = {"bid": v_bid, "ask": v_ask}

    def on_trade(
        self,
        asset_id: str,
        trade_price: float,
        trade_size: float,
        trade_side: str,
        timestamp: Optional[float] = None,
    ) -> Optional[SimulatedFill]:
        """Process incoming trade and simulate fills adhering to FIFO queue priority.

        Args:
            asset_id: Polymarket asset/token ID.
            trade_price: Execution price of real trade.
            trade_size: Execution size in shares of real trade.
            trade_side: Aggressor / taker side ("BUY" or "SELL").
            timestamp: Trade timestamp (defaults to current time).
        """
        if asset_id not in self.asset_to_condition:
            return None

        quotes = self.virtual_quotes.get(asset_id, {})
        v_bid = quotes.get("bid")
        v_ask = quotes.get("ask")
        ts = timestamp or time.time()

        executed_fill: Optional[SimulatedFill] = None

        # 1. Taker SELL against our resting limit BUY
        if trade_side.upper() == "SELL" and v_bid:
            # Crossed through our quote price: instant aggressive fill
            if trade_price < v_bid.price - 1e-6:
                fill_sz = min(trade_size, v_bid.size)
                fill_px = v_bid.price
                v_bid.queue_ahead = 0.0
                trigger_reason = f"taker_sell_crossed_through_bid (px={trade_price} < quote={fill_px})"
                executed_fill = self._execute_buy_fill(asset_id, fill_px, fill_sz, trigger_reason, ts)

            # Printed at our quote price level
            elif abs(trade_price - v_bid.price) <= 1e-6:
                if trade_size <= v_bid.queue_ahead:
                    v_bid.queue_ahead -= trade_size
                    logger.info(
                        f"[QUEUE DEPLETED] Bid @ {v_bid.price:.3f} queue ahead reduced by "
                        f"{trade_size:.1f} to {v_bid.queue_ahead:.1f}"
                    )
                    return None
                else:
                    surplus = trade_size - v_bid.queue_ahead
                    fill_sz = min(v_bid.size, surplus)
                    fill_px = v_bid.price
                    v_bid.queue_ahead = 0.0
                    trigger_reason = f"taker_sell_depleted_queue_bid (px={trade_price} == quote={fill_px}, surplus={surplus:.2f})"
                    executed_fill = self._execute_buy_fill(asset_id, fill_px, fill_sz, trigger_reason, ts)

        # 2. Taker BUY against our resting limit SELL
        elif trade_side.upper() == "BUY" and v_ask:
            # Crossed through our quote price: instant aggressive fill
            if trade_price > v_ask.price + 1e-6:
                fill_sz = min(trade_size, v_ask.size)
                fill_px = v_ask.price
                v_ask.queue_ahead = 0.0
                trigger_reason = f"taker_buy_crossed_through_ask (px={trade_price} > quote={fill_px})"
                executed_fill = self._execute_sell_fill(asset_id, fill_px, fill_sz, trigger_reason, ts)

            # Printed at our quote price level
            elif abs(trade_price - v_ask.price) <= 1e-6:
                if trade_size <= v_ask.queue_ahead:
                    v_ask.queue_ahead -= trade_size
                    logger.info(
                        f"[QUEUE DEPLETED] Ask @ {v_ask.price:.3f} queue ahead reduced by "
                        f"{trade_size:.1f} to {v_ask.queue_ahead:.1f}"
                    )
                    return None
                else:
                    surplus = trade_size - v_ask.queue_ahead
                    fill_sz = min(v_ask.size, surplus)
                    fill_px = v_ask.price
                    v_ask.queue_ahead = 0.0
                    trigger_reason = f"taker_buy_depleted_queue_ask (px={trade_price} == quote={fill_px}, surplus={surplus:.2f})"
                    executed_fill = self._execute_sell_fill(asset_id, fill_px, fill_sz, trigger_reason, ts)

        return executed_fill

    def _execute_buy_fill(
        self,
        asset_id: str,
        fill_px: float,
        fill_sz: float,
        trigger_reason: str,
        ts: float,
    ) -> SimulatedFill:
        fill_val = fill_sz * fill_px
        cid = self.asset_to_condition[asset_id]
        outcome = self.asset_to_outcome.get(asset_id, "unknown")
        pos = self.positions[asset_id]

        self.cash_usd -= fill_val
        old_shares = pos.shares
        old_cost = pos.avg_cost
        realized_pnl = 0.0

        if old_shares >= 0:
            new_shares = old_shares + fill_sz
            new_cost = ((old_shares * old_cost) + fill_val) / new_shares if new_shares > 0 else 0.0
        else:
            # Covering short position
            covered = min(abs(old_shares), fill_sz)
            realized_pnl = covered * (old_cost - fill_px)
            new_shares = old_shares + fill_sz
            if new_shares < 0:
                new_cost = old_cost
            elif new_shares > 0:
                new_cost = fill_px
            else:
                new_cost = 0.0

        pos.shares = new_shares
        pos.avg_cost = new_cost
        pos.realized_pnl += realized_pnl
        pos.fills_count += 1
        book_mid = self.orderbooks[asset_id]["mid"]
        pos.update_mark(book_mid)

        executed_fill = SimulatedFill(
            fill_id=str(uuid.uuid4())[:8],
            timestamp=ts,
            condition_id=cid,
            asset_id=asset_id,
            outcome=outcome,
            side="BUY",
            fill_price=fill_px,
            fill_size=fill_sz,
            fill_value_usd=fill_val,
            cash_delta=-fill_val,
            position_shares_after=new_shares,
            avg_cost_after=new_cost,
            realized_pnl=realized_pnl,
            trigger_reason=trigger_reason,
        )
        self.fills.append(executed_fill)
        logger.info(
            f"[SIMULATED FILL] BUY {fill_sz:.1f} {outcome} @ ${fill_px:.3f} (${fill_val:.2f}) | "
            f"New Inventory: {new_shares:.1f} sh | Cash: ${self.cash_usd:,.2f}"
        )

        # Check automated CTF complete-set merge
        self.check_and_execute_merge(cid, timestamp=ts)

        # Update resting quote status
        v_bid = self.virtual_quotes.get(asset_id, {}).get("bid")
        if v_bid:
            if fill_sz >= v_bid.size - 1e-6:
                self.virtual_quotes[asset_id]["bid"] = None
            else:
                v_bid.size -= fill_sz

        self.generate_quotes(asset_id)
        return executed_fill

    def _execute_sell_fill(
        self,
        asset_id: str,
        fill_px: float,
        fill_sz: float,
        trigger_reason: str,
        ts: float,
    ) -> SimulatedFill:
        fill_val = fill_sz * fill_px
        cid = self.asset_to_condition[asset_id]
        outcome = self.asset_to_outcome.get(asset_id, "unknown")
        pos = self.positions[asset_id]

        self.cash_usd += fill_val
        old_shares = pos.shares
        old_cost = pos.avg_cost
        realized_pnl = 0.0

        if old_shares > 0:
            # Closing long position
            closed = min(old_shares, fill_sz)
            realized_pnl = closed * (fill_px - old_cost)
            new_shares = old_shares - fill_sz
            if new_shares > 0:
                new_cost = old_cost
            elif new_shares < 0:
                new_cost = fill_px
            else:
                new_cost = 0.0
        else:
            new_shares = old_shares - fill_sz
            new_cost = ((abs(old_shares) * old_cost) + fill_val) / abs(new_shares) if new_shares != 0 else 0.0

        pos.shares = new_shares
        pos.avg_cost = new_cost
        pos.realized_pnl += realized_pnl
        pos.fills_count += 1
        book_mid = self.orderbooks[asset_id]["mid"]
        pos.update_mark(book_mid)

        executed_fill = SimulatedFill(
            fill_id=str(uuid.uuid4())[:8],
            timestamp=ts,
            condition_id=cid,
            asset_id=asset_id,
            outcome=outcome,
            side="SELL",
            fill_price=fill_px,
            fill_size=fill_sz,
            fill_value_usd=fill_val,
            cash_delta=fill_val,
            position_shares_after=new_shares,
            avg_cost_after=new_cost,
            realized_pnl=realized_pnl,
            trigger_reason=trigger_reason,
        )
        self.fills.append(executed_fill)
        logger.info(
            f"[SIMULATED FILL] SELL {fill_sz:.1f} {outcome} @ ${fill_px:.3f} (${fill_val:.2f}) | "
            f"New Inventory: {new_shares:.1f} sh | Cash: ${self.cash_usd:,.2f}"
        )

        # Check automated CTF complete-set merge
        self.check_and_execute_merge(cid, timestamp=ts)

        # Update resting quote status
        v_ask = self.virtual_quotes.get(asset_id, {}).get("ask")
        if v_ask:
            if fill_sz >= v_ask.size - 1e-6:
                self.virtual_quotes[asset_id]["ask"] = None
            else:
                v_ask.size -= fill_sz

        self.generate_quotes(asset_id)
        return executed_fill

    def _get_yes_no_positions(self, condition_id: str) -> tuple[Optional[Position], Optional[Position]]:
        """Retrieve YES and NO positions for a condition if both exist."""
        mkt = self.markets.get(condition_id)
        if not mkt:
            return None, None
        yes_pos = None
        no_pos = None
        for tok in mkt.tokens:
            outcome = tok.outcome.strip().upper()
            if outcome == "YES":
                yes_pos = self.positions.get(tok.token_id)
            elif outcome == "NO":
                no_pos = self.positions.get(tok.token_id)
        if (yes_pos is None or no_pos is None) and len(mkt.tokens) == 2:
            p0 = self.positions.get(mkt.tokens[0].token_id)
            p1 = self.positions.get(mkt.tokens[1].token_id)
            return p0, p1
        return yes_pos, no_pos

    def check_and_execute_merge(
        self,
        condition_id: str,
        timestamp: Optional[float] = None,
    ) -> Optional[MergeEvent]:
        """Check if condition has reached auto_merge_threshold complete sets and execute merge."""
        yes_pos, no_pos = self._get_yes_no_positions(condition_id)
        if not yes_pos or not no_pos:
            return None

        yes_shares = max(0.0, yes_pos.shares)
        no_shares = max(0.0, no_pos.shares)
        mergeable = min(yes_shares, no_shares)

        if mergeable >= self.auto_merge_threshold:
            return self.execute_merge(condition_id, amount=mergeable, timestamp=timestamp)
        return None

    def execute_merge(
        self,
        condition_id: str,
        amount: Optional[float] = None,
        timestamp: Optional[float] = None,
    ) -> Optional[MergeEvent]:
        """Merge complete sets of YES + NO tokens into USDC cash, deducting gas fee."""
        yes_pos, no_pos = self._get_yes_no_positions(condition_id)
        if not yes_pos or not no_pos:
            return None

        yes_shares = max(0.0, yes_pos.shares)
        no_shares = max(0.0, no_pos.shares)
        mergeable = min(yes_shares, no_shares)

        merge_amount = min(mergeable, amount) if amount is not None else mergeable
        if merge_amount <= 0.0:
            return None

        # 1 YES + 1 NO = $1.00 USDC cash collateral
        unlocked_usd = merge_amount * 1.00
        gas_fee = self.merge_gas_fee_usd

        # Cash delta: +unlocked USDC - gas fee
        self.cash_usd += (unlocked_usd - gas_fee)
        self.total_gas_fees_usd += gas_fee
        self.total_merges_count += 1

        # Realize PnL from merging:
        merged_cost = merge_amount * (yes_pos.avg_cost + no_pos.avg_cost)
        net_merge_pnl = unlocked_usd - merged_cost - gas_fee
        yes_pos.realized_pnl += net_merge_pnl / 2.0
        no_pos.realized_pnl += net_merge_pnl / 2.0

        yes_pos.shares -= merge_amount
        no_pos.shares -= merge_amount

        if yes_pos.shares <= 1e-6:
            yes_pos.shares = 0.0
            yes_pos.avg_cost = 0.0
        if no_pos.shares <= 1e-6:
            no_pos.shares = 0.0
            no_pos.avg_cost = 0.0

        # Update marks
        if yes_pos.asset_id in self.orderbooks:
            yes_pos.update_mark(self.orderbooks[yes_pos.asset_id]["mid"])
        if no_pos.asset_id in self.orderbooks:
            no_pos.update_mark(self.orderbooks[no_pos.asset_id]["mid"])

        ts = timestamp or time.time()
        event = MergeEvent(
            timestamp=ts,
            condition_id=condition_id,
            shares_merged=round(merge_amount, 4),
            usdc_unlocked=round(unlocked_usd, 4),
            gas_fee_usd=round(gas_fee, 4),
            remaining_yes=round(yes_pos.shares, 4),
            remaining_no=round(no_pos.shares, 4),
            cash_after=round(self.cash_usd, 2),
        )
        self.merges.append(event)
        logger.info(
            f"[CTF MERGE] Merged {merge_amount:.1f} complete sets (YES/NO) in {condition_id[:10]}... | "
            f"Unlocked: +${unlocked_usd:.2f} | Gas Fee: -${gas_fee:.4f} | "
            f"Remaining: YES={yes_pos.shares:.1f}, NO={no_pos.shares:.1f} | Cash: ${self.cash_usd:,.2f}"
        )
        return event

    def run_epoch_sample(self) -> list[EpochSnapshot]:
        """Simulate a 1-minute reward sampling epoch across all registered head pools."""
        self.epoch_count += 1
        now = time.time()
        new_snapshots: list[EpochSnapshot] = []

        for cid, mkt in self.markets.items():
            for tok in mkt.tokens:
                asset_id = tok.token_id
                quotes = self.virtual_quotes.get(asset_id, {})
                v_bid = quotes.get("bid")
                v_ask = quotes.get("ask")
                book = self.orderbooks.get(asset_id, {})

                mid = book.get("mid", 0.50)
                comp_score = book.get("competing_score", 0.0)

                v_score = 0.0
                if v_bid and v_bid.post_only_valid:
                    v_score += v_bid.score
                if v_ask and v_ask.post_only_valid:
                    v_score += v_ask.score

                # Polymarket splits the daily market reward proportionally
                share_pct, epoch_payout, projected_daily = compute_epoch_payout(
                    v_score, comp_score, mkt.rate_per_day
                )

                self.total_rewards_earned_usd += epoch_payout

                snap = EpochSnapshot(
                    epoch_index=self.epoch_count,
                    timestamp=now,
                    condition_id=cid,
                    asset_id=asset_id,
                    outcome=tok.outcome,
                    mid_price=mid,
                    virtual_score=v_score,
                    competing_score=comp_score,
                    pool_share_pct=share_pct,
                    epoch_payout_usd=epoch_payout,
                    projected_daily_payout_usd=projected_daily,
                )
                self.epoch_snapshots.append(snap)
                new_snapshots.append(snap)

        logger.info(
            f"[REWARD EPOCH #{self.epoch_count}] Sampled {len(new_snapshots)} tokens | "
            f"Epoch Rewards: +${sum(s.epoch_payout_usd for s in new_snapshots):.4f} | "
            f"Cumulative Rewards: ${self.total_rewards_earned_usd:.4f}"
        )

        # Persist ledger on epoch
        self.save_ledger()
        return new_snapshots

    def get_portfolio_summary(self) -> dict[str, Any]:
        """Calculate total equity, unrealized PnL, and ROI."""
        positions_market_value = sum(p.market_value for p in self.positions.values())
        unrealized_pnl = sum(p.unrealized_pnl for p in self.positions.values())
        realized_pnl = sum(p.realized_pnl for p in self.positions.values())

        total_equity = self.cash_usd + positions_market_value
        net_trading_pnl = total_equity - self.initial_cash_usd
        total_pnl = net_trading_pnl + self.total_rewards_earned_usd
        roi_pct = (total_pnl / self.initial_cash_usd) * 100.0 if self.initial_cash_usd > 0 else 0.0

        # Calculate projected daily rewards across all markets
        latest_snaps = self.epoch_snapshots[-len(self.asset_to_condition):] if self.epoch_snapshots else []
        projected_daily_rewards = sum(s.projected_daily_payout_usd for s in latest_snaps)

        return {
            "initial_cash_usd": round(self.initial_cash_usd, 2),
            "current_cash_usd": round(self.cash_usd, 2),
            "positions_market_value_usd": round(positions_market_value, 2),
            "unrealized_pnl_usd": round(unrealized_pnl, 2),
            "realized_pnl_usd": round(realized_pnl, 2),
            "net_trading_pnl_usd": round(net_trading_pnl, 2),
            "total_rewards_earned_usd": round(self.total_rewards_earned_usd, 4),
            "total_equity_usd": round(total_equity, 2),
            "total_pnl_usd": round(total_pnl, 2),
            "roi_pct": round(roi_pct, 4),
            "total_gas_fees_usd": round(self.total_gas_fees_usd, 4),
            "total_merges_count": self.total_merges_count,
            "projected_daily_rewards_usd": round(projected_daily_rewards, 2),
            "projected_annual_rewards_usd": round(projected_daily_rewards * 365.0, 2),
        }

    def to_ledger_dict(self) -> dict[str, Any]:
        """Serialize complete engine state and metrics to JSON-compatible dictionary."""
        now = time.time()
        uptime = round(now - self.start_time, 1)

        markets_data: dict[str, Any] = {}
        for cid, mkt in self.markets.items():
            tokens_data: dict[str, Any] = {}
            for tok in mkt.tokens:
                aid = tok.token_id
                book = self.orderbooks.get(aid, {})
                quotes = self.virtual_quotes.get(aid, {})
                pos = self.positions.get(aid)

                v_bid = quotes.get("bid")
                v_ask = quotes.get("ask")

                # Get latest epoch snapshot for this token
                latest_snap = next(
                    (s for s in reversed(self.epoch_snapshots) if s.asset_id == aid), None
                )

                tokens_data[aid] = {
                    "outcome": tok.outcome,
                    "token_id": aid,
                    "mid_price": book.get("mid", 0.50),
                    "best_bid": book.get("best_bid", 0.0),
                    "best_ask": book.get("best_ask", 1.0),
                    "spread_cents": book.get("spread_cents", 0.0),
                    "virtual_quotes": {
                        "bid": v_bid.to_dict() if v_bid else None,
                        "ask": v_ask.to_dict() if v_ask else None,
                    },
                    "competing_depth": {
                        "competing_bids_usd": book.get("qualifying_bids_usd", 0.0),
                        "competing_asks_usd": book.get("qualifying_asks_usd", 0.0),
                        "competing_score": book.get("competing_score", 0.0),
                    },
                    "reward_metrics": {
                        "epoch_count": self.epoch_count,
                        "virtual_score": round((v_bid.score if v_bid else 0.0) + (v_ask.score if v_ask else 0.0), 4),
                        "competing_score": book.get("competing_score", 0.0),
                        "pool_share_pct": round(latest_snap.pool_share_pct, 4) if latest_snap else 0.0,
                        "epoch_payout_usd": round(latest_snap.epoch_payout_usd, 4) if latest_snap else 0.0,
                        "projected_daily_payout_usd": round(latest_snap.projected_daily_payout_usd, 2) if latest_snap else 0.0,
                    },
                    "position": pos.to_dict() if pos else {},
                }

            markets_data[cid] = {
                "question": mkt.question,
                "condition_id": cid,
                "rate_per_day": mkt.rate_per_day,
                "rewards_max_spread_cents": mkt.rewards_max_spread,
                "rewards_min_size": mkt.rewards_min_size,
                "tokens": tokens_data,
            }

        return {
            "system_info": {
                "version": "1.1.0",
                "status": "RUNNING",
                "start_time_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.start_time)),
                "last_updated_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                "uptime_seconds": uptime,
                "elapsed_epochs": self.epoch_count,
                "active_markets_count": len(self.markets),
                "active_tokens_count": len(self.asset_to_condition),
            },
            "portfolio": self.get_portfolio_summary(),
            "markets": markets_data,
            "recent_fills": [f.to_dict() for f in self.fills[-50:]],
            "recent_merges": [m.to_dict() for m in self.merges[-50:]],
            "recent_epochs": [s.to_dict() for s in self.epoch_snapshots[-50:]],
        }

    def save_ledger(self, custom_path: Optional[str | Path] = None) -> Path:
        """Persist state and live metrics atomically to ledger.json."""
        target = Path(custom_path) if custom_path else self.ledger_path
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_target = target.with_suffix(".tmp")

        data = self.to_ledger_dict()
        with open(tmp_target, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        tmp_target.replace(target)
        return target
