"""Production Native Paper Trading Engine for Polymarket CLOB.

Orchestrates:
1. SyntheticOrderBook: Ingestion of live L2 books and injection of virtual maker quotes.
2. MarketImpactEngine: FIFO queue priority, trade depletion, and Kyle's lambda post-fill price drift.
3. SimulatedPortfolio: Cash, YES/NO token inventory, average cost basis, realized/unrealized PnL,
   and real-world CTF Split and Merge mechanics ($1.00 = 1 YES + 1 NO, with $0.015 gas fee).
4. 2-Tier DynamicDeSkewOptimizer (deskew.py):
   - Tier 1: Passive Avellaneda-Stoikov quote fading.
   - Tier 2: Emergency active synthetic complete-set cross-hedging.
5. Polymarket Quadratic Liquidity Rewards Scoring:
   - S(v, s) = ((v - s)/v)^2 * size across 1,440 daily epochs with $1.00 daily payout floor enforcement.
6. Persistence to SQLite (data/native_paper_trading.db) and JSON ledger.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from polymarketbot.logs import get_logger
from polymarketbot.rewards.deskew import (
    DeSkewDecision,
    DeSkewPathway,
    DynamicDeSkewOptimizer,
)
from polymarketbot.rewards.scoring import quadratic_order_score
from polymarketbot.simulator.book import (
    DepthInfo,
    SyntheticOrderBook,
    VirtualMakerQuote,
)
from polymarketbot.simulator.impact import (
    MarketImpactEngine,
    QuoteSide,
    TradeImpactResult,
)
from polymarketbot.simulator.portfolio import (
    MergeResult,
    PortfolioTransaction,
    Position,
    SimulatedPortfolio,
    SplitResult,
)

log = get_logger("simulator.engine")


# ---------------------------------------------------------------------------
# Matcher & Feed Abstractions (Self-Contained & Modular)
# ---------------------------------------------------------------------------

@dataclass
class FillRecord:
    """Record of an executed fill against our resting or taker quote."""

    fill_id: str
    timestamp: float
    condition_id: str
    token_side: str  # "YES" or "NO"
    side: str  # "BUY" or "SELL"
    fill_price: float
    fill_size: float
    is_maker: bool
    slippage_cents: float = 0.0
    queue_depleted: float = 0.0
    post_fill_drift: float = 0.0
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class OrderMatcherBridge:
    """Matches incoming trades against virtual quotes in SyntheticOrderBook via MarketImpactEngine."""

    def __init__(self, condition_id: str) -> None:
        self.condition_id = condition_id

    def match_market_trade(
        self,
        book: SyntheticOrderBook,
        impact_engine: MarketImpactEngine,
        token_side: str,  # "YES" or "NO"
        trade_price: float,
        trade_size: float,
        trade_side: str,  # "BUY" or "SELL" (taker side)
        timestamp: Optional[float] = None,
    ) -> List[FillRecord]:
        """Match incoming market trade against resting virtual maker quotes."""
        fills: List[FillRecord] = []
        ts = timestamp or time.time()

        impact_results = impact_engine.on_trade(
            trade_price=trade_price,
            trade_size=trade_size,
            trade_side=trade_side,
            tau=1.0,
        )

        for res in impact_results:
            if res.fill_size > 0:
                record = FillRecord(
                    fill_id=str(uuid.uuid4())[:8],
                    timestamp=ts,
                    condition_id=self.condition_id,
                    token_side=token_side,
                    side=res.side,
                    fill_price=res.quote_price,
                    fill_size=res.fill_size,
                    is_maker=True,
                    slippage_cents=0.0,
                    queue_depleted=max(0.0, res.queue_ahead_before - res.queue_ahead_after),
                    post_fill_drift=res.post_fill_drift,
                    notes=f"Passive maker fill via market trade ({trade_size:.2f} @ ${trade_price:.3f})",
                )
                fills.append(record)

        return fills


# ---------------------------------------------------------------------------
# Native Paper Trading Engine
# ---------------------------------------------------------------------------

class NativePaperTradingEngine:
    """Production Polymarket paper trading orchestrator and execution simulator."""

    def __init__(
        self,
        condition_id: str,
        asset_id_yes: str = "TOKEN_YES",
        asset_id_no: str = "TOKEN_NO",
        initial_cash: float = 100.0,
        quote_size: float = 20.0,
        spread_offset_cents: float = 0.50,
        daily_reward_rate: float = 20.0,
        rewards_max_spread_cents: float = 1.0,
        rewards_min_size: float = 20.0,
        auto_merge_threshold: float = 10.0,
        gas_fee_merge: float = 0.015,
        institutional_mode: bool = True,
        gamma: float = 0.02,
        db_path: Union[str, Path] = "data/native_paper_trading.db",
        ledger_path: Union[str, Path] = "data/native_paper_trading_ledger.json",
        deskew_optimizer: Optional[DynamicDeSkewOptimizer] = None,
    ) -> None:
        self.condition_id = condition_id
        self.asset_id_yes = asset_id_yes
        self.asset_id_no = asset_id_no
        self.quote_size = float(quote_size)
        self.spread_offset_cents = float(spread_offset_cents)
        self.daily_reward_rate = float(daily_reward_rate)
        self.rewards_max_spread = float(rewards_max_spread_cents)
        self.rewards_min_size = float(rewards_min_size)
        self.auto_merge_threshold = float(auto_merge_threshold)
        self.gas_fee_merge = float(gas_fee_merge)
        self.institutional_mode = bool(institutional_mode)
        self.gamma = float(gamma)

        # Paths
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.ledger_path = Path(ledger_path)
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)

        # Core Simulator Modules
        self.portfolio = SimulatedPortfolio(
            initial_cash=initial_cash,
            gas_fee_merge=gas_fee_merge,
            taker_fee_bps=0.0,  # 0 bps default on Polymarket binary contracts
        )
        self.book_yes = SyntheticOrderBook(asset_id=asset_id_yes)
        self.book_no = SyntheticOrderBook(asset_id=asset_id_no)
        self.impact_yes = MarketImpactEngine(book=self.book_yes)
        self.impact_no = MarketImpactEngine(book=self.book_no)
        self.matcher = OrderMatcherBridge(condition_id=self.condition_id)

        # 2-Tier Dynamic De-Skew Optimizer
        self.deskew_optimizer = deskew_optimizer or DynamicDeSkewOptimizer(
            gamma=0.02,
            gas_cost_merge_usd=self.gas_fee_merge,
            q_soft_threshold=15.0,
            q_hard_threshold=100.0,
            max_holding_seconds=300.0,
        )

        # Virtual Maker Quote Registry: quote_id -> dict
        self.active_quotes: Dict[str, dict[str, Any]] = {}

        # Epoch & Telemetry State
        self.epoch_index: int = 0
        self.last_epoch_ts: float = time.time()
        self.total_rewards_credited: float = 0.0
        self.latest_deskew_decision: Optional[DeSkewDecision] = None
        self.fills_history: List[FillRecord] = []
        self.merges_history: List[MergeResult] = []
        self.splits_history: List[SplitResult] = []

        # SQLite Connection
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._init_sqlite()

        log.info(
            f"NativePaperTradingEngine initialized for condition {self.condition_id[:12]}... "
            f"Initial Cash: ${initial_cash:.2f} | DB: {self.db_path}"
        )

    # -------------------------------------------------------------------------
    # Database Initialization & Persistence
    # -------------------------------------------------------------------------

    def _init_sqlite(self) -> None:
        """Create high-performance SQLite schema with WAL journal mode."""
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode = WAL;")
        cur.execute("PRAGMA synchronous = NORMAL;")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                cash REAL NOT NULL,
                market_value REAL NOT NULL,
                equity REAL NOT NULL,
                unrealized_pnl REAL NOT NULL,
                realized_pnl REAL NOT NULL,
                total_pnl REAL NOT NULL,
                total_rewards REAL NOT NULL,
                total_gas_fees REAL NOT NULL,
                total_merges INTEGER NOT NULL,
                total_splits INTEGER NOT NULL,
                total_volume REAL NOT NULL
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                condition_id TEXT NOT NULL,
                shares_yes REAL NOT NULL,
                shares_no REAL NOT NULL,
                avg_cost_yes REAL NOT NULL,
                avg_cost_no REAL NOT NULL,
                net_exposure REAL NOT NULL,
                complete_sets REAL NOT NULL,
                realized_pnl REAL NOT NULL
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS active_quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                condition_id TEXT NOT NULL,
                quote_id TEXT NOT NULL,
                token_side TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                size REAL NOT NULL,
                queue_ahead REAL NOT NULL,
                quadratic_score REAL NOT NULL,
                status TEXT NOT NULL
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fill_id TEXT UNIQUE NOT NULL,
                timestamp REAL NOT NULL,
                condition_id TEXT NOT NULL,
                token_side TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                size REAL NOT NULL,
                liquidity_type TEXT NOT NULL,
                slippage_cents REAL NOT NULL,
                queue_depleted REAL NOT NULL,
                post_fill_drift REAL NOT NULL,
                realized_pnl REAL NOT NULL,
                cash_after REAL NOT NULL,
                notes TEXT
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS ctf_merges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                condition_id TEXT NOT NULL,
                complete_sets REAL NOT NULL,
                usdc_gross REAL NOT NULL,
                gas_fee REAL NOT NULL,
                net_cash REAL NOT NULL,
                realized_gain REAL NOT NULL,
                cash_after REAL NOT NULL
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS ctf_splits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                condition_id TEXT NOT NULL,
                amount_split REAL NOT NULL,
                cash_after REAL NOT NULL
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS reward_epochs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                epoch_index INTEGER NOT NULL,
                timestamp REAL NOT NULL,
                condition_id TEXT NOT NULL,
                user_score REAL NOT NULL,
                competing_score REAL NOT NULL,
                pool_share_pct REAL NOT NULL,
                epoch_payout_usd REAL NOT NULL,
                projected_daily_usd REAL NOT NULL,
                floor_met INTEGER NOT NULL,
                cumulative_rewards_usd REAL NOT NULL
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS deskew_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                condition_id TEXT NOT NULL,
                pathway TEXT NOT NULL,
                urgency TEXT NOT NULL,
                target_token TEXT NOT NULL,
                action_side TEXT NOT NULL,
                price REAL NOT NULL,
                size REAL NOT NULL,
                savings_cents REAL NOT NULL,
                reason TEXT NOT NULL
            );
        """)

        self._conn.commit()

    # -------------------------------------------------------------------------
    # Ingestion of Live Feed Events
    # -------------------------------------------------------------------------

    def update_orderbook_snapshot(
        self,
        token_side: str,  # "YES" or "NO"
        bids: Sequence[Any],
        asks: Sequence[Any],
        timestamp: Optional[float] = None,
    ) -> None:
        """Ingest live order book snapshot for YES or NO outcome token."""
        ts = timestamp or time.time()
        token_norm = token_side.upper()

        if token_norm == "YES":
            self.book_yes.apply_snapshot(bids=bids, asks=asks, timestamp=ts)
        elif token_norm == "NO":
            self.book_no.apply_snapshot(bids=bids, asks=asks, timestamp=ts)
        else:
            raise ValueError(f"Unknown token side: {token_side}")

        # Re-evaluate quotes whenever the book updates
        self.evaluate_quotes(timestamp=ts)

    def process_public_trade(
        self,
        token_side: str,  # "YES" or "NO"
        price: float,
        size: float,
        side: str,  # "BUY" or "SELL"
        timestamp: Optional[float] = None,
    ) -> List[FillRecord]:
        """Process incoming market trade against our resting virtual quotes."""
        ts = timestamp or time.time()
        token_norm = token_side.upper()
        book = self.book_yes if token_norm == "YES" else self.book_no
        impact = self.impact_yes if token_norm == "YES" else self.impact_no

        # Match trade
        fills = self.matcher.match_market_trade(
            book=book,
            impact_engine=impact,
            token_side=token_norm,
            trade_price=price,
            trade_size=size,
            trade_side=side,
            timestamp=ts,
        )

        for fill in fills:
            # Process fill into portfolio ledger
            tx = self.portfolio.process_fill(
                condition_id=self.condition_id,
                side=fill.side,
                outcome=fill.token_side,
                price=fill.fill_price,
                size=fill.fill_size,
                is_maker=fill.is_maker,
                notes=fill.notes,
            )

            # Record fill to SQLite
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO fills (
                    fill_id, timestamp, condition_id, token_side, side, price, size,
                    liquidity_type, slippage_cents, queue_depleted, post_fill_drift,
                    realized_pnl, cash_after, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fill.fill_id,
                    fill.timestamp,
                    fill.condition_id,
                    fill.token_side,
                    fill.side,
                    fill.fill_price,
                    fill.fill_size,
                    "MAKER" if fill.is_maker else "TAKER",
                    fill.slippage_cents,
                    fill.queue_depleted,
                    fill.post_fill_drift,
                    tx.realized_pnl,
                    tx.cash_after,
                    fill.notes,
                ),
            )
            self._conn.commit()
            self.fills_history.append(fill)

            # Check CTF Merge condition
            pos = self.portfolio.get_position(self.condition_id)
            if pos.complete_sets >= self.auto_merge_threshold:
                self.trigger_ctf_merge()

        if fills:
            self.evaluate_quotes(timestamp=ts)
            self.flush_ledger()

        return fills

    # -------------------------------------------------------------------------
    # Quoting Logic & 2-Tier De-Skewing Execution
    # -------------------------------------------------------------------------

    def evaluate_quotes(self, timestamp: Optional[float] = None) -> None:
        """Run 2-Tier Dynamic De-Skewing Optimizer and generate virtual maker quotes."""
        ts = timestamp or time.time()
        pos = self.portfolio.get_position(self.condition_id)

        # Get top-of-book prices
        b_yes = self.book_yes.best_bid_real or 0.49
        a_yes = self.book_yes.best_ask_real or 0.51
        b_no = self.book_no.best_bid_real or (1.0 - a_yes)
        a_no = self.book_no.best_ask_real or (1.0 - b_yes)

        mid_yes = (b_yes + a_yes) / 2.0

        # Evaluate skew using DynamicDeSkewOptimizer
        decision = self.deskew_optimizer.evaluate_skew(
            shares_yes=pos.shares_yes,
            shares_no=pos.shares_no,
            best_bid_yes=b_yes,
            best_ask_yes=a_yes,
            best_bid_no=b_no,
            best_ask_no=a_no,
            time_to_resolution_days=7.0,
        )
        self.latest_deskew_decision = decision

        # ---------------------------------------------------------------------
        # TIER 2: Active Emergency Taker Execution
        # ---------------------------------------------------------------------
        if decision.pathway in (DeSkewPathway.SYNTHETIC_MERGE, DeSkewPathway.DIRECT_CROSS):
            self._execute_tier2_taker_deskew(decision, ts)
            return

        # ---------------------------------------------------------------------
        # TIER 1: Passive Avellaneda-Stoikov Quote Fading
        # ---------------------------------------------------------------------
        if decision.pathway == DeSkewPathway.PASSIVE_FADE:
            self._execute_tier1_passive_fade(decision, mid_yes, b_yes, a_yes, ts)
            return

        # ---------------------------------------------------------------------
        # NEUTRAL: Two-Sided Maker Quoting around Mid
        # ---------------------------------------------------------------------
        self._execute_neutral_maker_quoting(mid_yes, b_yes, a_yes, ts)

    def _execute_tier1_passive_fade(
        self,
        decision: DeSkewDecision,
        mid_yes: float,
        best_bid_yes: float,
        best_ask_yes: float,
        ts: float,
    ) -> None:
        """Tier 1: Avellaneda-Stoikov inventory quote fading."""
        # Record deskew event
        self._record_deskew_event(decision, ts)

        if self.institutional_mode:
            pos = self.portfolio.get_position(self.condition_id)
            net_q = pos.shares_yes - pos.shares_no
            p = max(0.01, min(0.99, mid_yes))
            variance = (p * (1.0 - p)) / 7.0
            r_yes = max(0.01, min(0.99, mid_yes - (net_q * self.gamma * variance)))
            offset = self.spread_offset_cents / 100.0

            if net_q > 0:
                # Long YES -> tighten NO bid to complete sets; fade/widen YES bid
                target_bid_no = round(min(0.98, max(0.01, 1.00 - max(best_bid_yes + 0.001, decision.target_price))), 3)
                faded_bid_yes = round(max(0.01, min(best_ask_yes - 0.001, r_yes - offset - 0.01)), 3)
                self._set_virtual_quote("NO", "BUY", target_bid_no, self.quote_size, ts)
                self._set_virtual_quote("YES", "BUY", faded_bid_yes, self.quote_size, ts)
            else:
                # Long NO -> tighten YES bid to complete sets; fade/widen NO bid
                target_bid_yes = round(max(0.01, min(best_ask_yes - 0.001, decision.target_price)), 3)
                faded_bid_no = round(max(0.01, min(0.98, 1.00 - (r_yes + offset + 0.01))), 3)
                self._set_virtual_quote("YES", "BUY", target_bid_yes, self.quote_size, ts)
                self._set_virtual_quote("NO", "BUY", faded_bid_no, self.quote_size, ts)

            self._cancel_virtual_quote("SELL_YES")
            self._cancel_virtual_quote("SELL_NO")
            return

        # Legacy Single-Token Mode
        if decision.target_token == "YES" and decision.action_side == "SELL":
            faded_ask = min(best_ask_yes, max(best_bid_yes + 0.001, decision.target_price))
            self._set_virtual_quote("YES", "SELL", faded_ask, self.quote_size, ts)
            self._cancel_virtual_quote("BID_YES")
        elif decision.target_token == "YES" and decision.action_side == "BUY":
            faded_bid = max(best_bid_yes, min(best_ask_yes - 0.001, decision.target_price))
            self._set_virtual_quote("YES", "BUY", faded_bid, self.quote_size, ts)
            self._cancel_virtual_quote("ASK_YES")

    def _execute_tier2_taker_deskew(self, decision: DeSkewDecision, ts: float) -> None:
        """Tier 2: Emergency cross-spread taker execution."""
        self._record_deskew_event(decision, ts)
        target_token = decision.target_token
        target_side = decision.action_side
        target_size = min(decision.target_size, self.quote_size * 2.0)
        target_price = decision.target_price

        log.warning(
            f"EXECUTING TIER 2 EMERGENCY DE-SKEW ({decision.pathway.value}): "
            f"{target_side} {target_size:.2f} {target_token} @ ${target_price:.3f} | {decision.reason}"
        )

        try:
            # Execute taker fill against portfolio
            tx = self.portfolio.process_fill(
                condition_id=self.condition_id,
                side=target_side,
                outcome=target_token,
                price=target_price,
                size=target_size,
                is_maker=False,
                notes=f"Tier 2 Taker {decision.pathway.value}: {decision.reason}",
            )

            fill_rec = FillRecord(
                fill_id=str(uuid.uuid4())[:8],
                timestamp=ts,
                condition_id=self.condition_id,
                token_side=target_token,
                side=target_side,
                fill_price=target_price,
                fill_size=target_size,
                is_maker=False,
                notes=f"Tier 2 Emergency Taker Fill ({decision.pathway.value})",
            )
            self.fills_history.append(fill_rec)

            # If synthetic merge pathway: trigger immediate CTF Merge
            if decision.pathway == DeSkewPathway.SYNTHETIC_MERGE:
                self.trigger_ctf_merge()

        except Exception as e:
            log.error(f"Failed to execute Tier 2 de-skew taker fill: {e}")

    def _execute_neutral_maker_quoting(
        self,
        mid_yes: float,
        best_bid_yes: float,
        best_ask_yes: float,
        ts: float,
    ) -> None:
        """Standard two-sided virtual maker quotes inside the qualifying spread."""
        offset = self.spread_offset_cents / 100.0

        if self.institutional_mode:
            # Jane Street Institutional Dual-Cash Quoting (BUY YES + BUY NO)
            pos = self.portfolio.get_position(self.condition_id)
            net_q = pos.shares_yes - pos.shares_no
            p = max(0.01, min(0.99, mid_yes))
            variance = (p * (1.0 - p)) / 7.0
            r_yes = max(0.01, min(0.99, mid_yes - (net_q * self.gamma * variance)))

            skew_adj = net_q * 0.0005
            target_bid_yes = r_yes - offset - max(0.0, skew_adj)
            target_ask_yes = r_yes + offset - min(0.0, skew_adj)

            target_bid_yes = min(best_ask_yes - 0.001, target_bid_yes)
            target_ask_yes = max(best_bid_yes + 0.001, target_ask_yes)

            target_bid_yes = round(max(0.01, min(0.98, target_bid_yes)), 3)
            target_ask_yes = round(max(target_bid_yes + 0.002, min(0.99, target_ask_yes)), 3)
            target_bid_no = round(1.00 - target_ask_yes, 3)

            # Enforce cash sizing bounds
            req_cash = (target_bid_yes + target_bid_no) * self.quote_size
            eff_size = self.quote_size if self.portfolio.cash >= req_cash else max(5.0, math.floor(self.portfolio.cash / (target_bid_yes + target_bid_no)))

            # Place pure dual-cash bids
            self._set_virtual_quote("YES", "BUY", target_bid_yes, eff_size, ts)
            self._set_virtual_quote("NO", "BUY", target_bid_no, eff_size, ts)
            self._cancel_virtual_quote("SELL_YES")
            self._cancel_virtual_quote("SELL_NO")
            return

        # Legacy Single-Token Mode
        target_bid = round(max(0.01, mid_yes - offset), 3)
        target_ask = round(min(0.99, mid_yes + offset), 3)

        if target_bid >= best_ask_yes:
            target_bid = round(best_ask_yes - 0.001, 3)
        if target_ask <= best_bid_yes:
            target_ask = round(best_bid_yes + 0.001, 3)

        self._set_virtual_quote("YES", "BUY", target_bid, self.quote_size, ts)
        self._set_virtual_quote("YES", "SELL", target_ask, self.quote_size, ts)

    def _set_virtual_quote(
        self,
        token_side: str,
        quote_side: str,
        price: float,
        size: float,
        ts: float,
    ) -> None:
        """Inject or update a virtual maker quote in book and tracker."""
        qid = f"{quote_side}_{token_side}".upper()
        book = self.book_yes if token_side == "YES" else self.book_no
        impact = self.impact_yes if token_side == "YES" else self.impact_no

        # Check existing quote
        existing = self.active_quotes.get(qid)
        if existing and abs(existing["price"] - price) < 1e-4 and abs(existing["size"] - size) < 1e-4:
            # Price unchanged: retain FIFO priority
            tracker = impact.quotes.get(qid)
            q_ahead = tracker.queue_ahead if tracker is not None else impact.compute_queue_ahead(quote_side, price, book=book)
            existing["queue_ahead"] = q_ahead
            return

        # Price revision or new quote: queue resets
        tracker = impact.place_quote(side=quote_side, price=price, size=size, quote_id=qid, timestamp=ts)
        q_ahead = tracker.queue_ahead

        # Compute quadratic score: S(v, s) = ((v - s)/v)^2 * size
        mid = book.mid_price or (self.book_yes.best_bid_real or 0.5)
        spread_cents = abs(price - mid) * 100.0
        score = quadratic_order_score(
            max_spread_cents=self.rewards_max_spread,
            spread_cents=spread_cents,
            size=size,
            min_size=self.rewards_min_size,
        )

        quote_data = {
            "quote_id": qid,
            "token_side": token_side,
            "side": quote_side,
            "price": price,
            "size": size,
            "queue_ahead": round(q_ahead, 2),
            "score": round(score, 4),
            "timestamp": ts,
            "status": "ACTIVE",
        }
        self.active_quotes[qid] = quote_data

    def _cancel_virtual_quote(self, qid: str) -> None:
        """Cancel an active virtual quote."""
        if qid in self.active_quotes:
            quote_data = self.active_quotes.pop(qid)
            tok = quote_data.get("token_side", "YES")
            impact = self.impact_yes if tok == "YES" else self.impact_no
            impact.cancel_quote(qid)

    def _record_deskew_event(self, decision: DeSkewDecision, ts: float) -> None:
        """Persist a dynamic de-skew decision to SQLite."""
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

    # -------------------------------------------------------------------------
    # CTF Split and Merge Mechanics
    # -------------------------------------------------------------------------

    def trigger_ctf_split(self, amount_usdc: float) -> SplitResult:
        """Execute CTF Split: $1.00 USDC -> 1 YES + 1 NO."""
        res = self.portfolio.split(self.condition_id, amount_usdc)
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO ctf_splits (timestamp, condition_id, amount_split, cash_after)
            VALUES (?, ?, ?, ?)
            """,
            (time.time(), self.condition_id, res.cash_split, res.cash_after),
        )
        self._conn.commit()
        self.splits_history.append(res)
        self.flush_ledger()
        return res

    def trigger_ctf_merge(self, amount_sets: Optional[float] = None) -> Optional[MergeResult]:
        """Execute CTF Merge: min(YES, NO) complete sets -> $1.00 USDC (-$0.015 gas)."""
        res = self.portfolio.merge(self.condition_id, amount_sets)
        if res is not None:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO ctf_merges (
                    timestamp, condition_id, complete_sets, usdc_gross, gas_fee,
                    net_cash, realized_gain, cash_after
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time(),
                    self.condition_id,
                    res.complete_sets_merged,
                    res.usdc_gross,
                    res.gas_fee_deducted,
                    res.net_cash_unlocked,
                    res.realized_gain,
                    res.cash_after,
                ),
            )
            self._conn.commit()
            self.merges_history.append(res)
            self.flush_ledger()
        return res

    # -------------------------------------------------------------------------
    # Polymarket Quadratic Rewards 1-Minute Epoch Scoring
    # -------------------------------------------------------------------------

    def process_reward_epoch(self, timestamp: Optional[float] = None) -> dict[str, Any]:
        """Compute 1-minute reward epoch payout with $1.00 daily payout floor enforcement.

        Formula:
            S(v, s) = ((v - s) / v)^2 * size
            Payout_epoch = (S_user / (S_user + S_competing)) * (R_daily / 1440)
            Enforces: Projected daily payout must be >= $1.00; otherwise payout = $0.00.
        """
        ts = timestamp or time.time()
        self.epoch_index += 1
        self.last_epoch_ts = ts

        # Sum user scores across active resting quotes
        total_user_score = sum(q.get("score", 0.0) for q in self.active_quotes.values())

        # Estimate competing depth score from real order book inside qualifying spread
        competing_score = 0.0
        mid = self.book_yes.mid_price or 0.50
        for px, sz in self.book_yes._real_bids.items():
            spread_c = (mid - px) * 100.0
            if 0 <= spread_c <= self.rewards_max_spread:
                competing_score += quadratic_order_score(self.rewards_max_spread, spread_c, sz)
        for px, sz in self.book_yes._real_asks.items():
            spread_c = (px - mid) * 100.0
            if 0 <= spread_c <= self.rewards_max_spread:
                competing_score += quadratic_order_score(self.rewards_max_spread, spread_c, sz)

        # Baseline floor for competing depth
        competing_score = max(0.0, competing_score)

        # Pool Share
        total_pool_score = total_user_score + competing_score
        pool_share_pct = (total_user_score / total_pool_score) * 100.0 if total_pool_score > 0 else 0.0

        # Daily rate divided across 1,440 daily epochs
        epoch_budget_usd = self.daily_reward_rate / 1440.0
        raw_epoch_payout = (total_user_score / total_pool_score) * epoch_budget_usd if total_pool_score > 0 else 0.0
        projected_daily_usd = raw_epoch_payout * 1440.0

        # $1.00 Daily Payout Floor Rule
        floor_met = projected_daily_usd >= 1.00
        actual_epoch_payout = raw_epoch_payout if floor_met else 0.0

        if actual_epoch_payout > 0:
            self.portfolio.credit_reward(
                actual_epoch_payout,
                condition_id=self.condition_id,
                note=f"Epoch {self.epoch_index} Reward ({pool_share_pct:.2f}% share)",
            )
            self.total_rewards_credited += actual_epoch_payout

        # Record to SQLite
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO reward_epochs (
                epoch_index, timestamp, condition_id, user_score, competing_score,
                pool_share_pct, epoch_payout_usd, projected_daily_usd, floor_met,
                cumulative_rewards_usd
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.epoch_index,
                ts,
                self.condition_id,
                total_user_score,
                competing_score,
                pool_share_pct,
                actual_epoch_payout,
                projected_daily_usd,
                1 if floor_met else 0,
                self.total_rewards_credited,
            ),
        )
        self._conn.commit()

        log.info(
            f"Epoch {self.epoch_index} processed: Score={total_user_score:.2f} | Share={pool_share_pct:.3f}% "
            f"| Payout=${actual_epoch_payout:.4f} (Projected Daily: ${projected_daily_usd:.2f}, Floor Met: {floor_met}) "
            f"| Total Rewards: ${self.total_rewards_credited:.4f}"
        )

        return {
            "epoch_index": self.epoch_index,
            "timestamp": ts,
            "user_score": round(total_user_score, 4),
            "competing_score": round(competing_score, 2),
            "pool_share_pct": round(pool_share_pct, 4),
            "epoch_payout_usd": round(actual_epoch_payout, 6),
            "projected_daily_usd": round(projected_daily_usd, 2),
            "floor_met": floor_met,
            "total_rewards_usd": round(self.total_rewards_credited, 4),
        }

    # -------------------------------------------------------------------------
    # Ledger Flushing & Telemetry
    # -------------------------------------------------------------------------

    def flush_ledger(self) -> None:
        """Write atomic state snapshot to SQLite and JSON ledger."""
        marks = {
            self.condition_id: (
                self.book_yes.mid_price or 0.5,
                self.book_no.mid_price or 0.5,
            )
        }
        state = self.portfolio.get_state(marks)

        # Snapshot in SQLite
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO portfolio_snapshots (
                timestamp, cash, market_value, equity, unrealized_pnl, realized_pnl,
                total_pnl, total_rewards, total_gas_fees, total_merges, total_splits, total_volume
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                time.time(),
                state["cash"],
                state["market_value"],
                state["equity"],
                state["unrealized_pnl"],
                state["realized_pnl"],
                state["total_pnl"],
                state["total_rewards_earned"],
                state["total_gas_fees"],
                state["total_merges_count"],
                state["total_splits_count"],
                state["total_volume_traded_usd"],
            ),
        )

        pos = self.portfolio.get_position(self.condition_id)
        cur.execute(
            """
            INSERT INTO positions (
                timestamp, condition_id, shares_yes, shares_no, avg_cost_yes,
                avg_cost_no, net_exposure, complete_sets, realized_pnl
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                time.time(),
                self.condition_id,
                pos.shares_yes,
                pos.shares_no,
                pos.avg_cost_yes,
                pos.avg_cost_no,
                pos.net_exposure,
                pos.complete_sets,
                pos.realized_pnl,
            ),
        )
        self._conn.commit()

        # Write JSON ledger
        ledger_data = {
            "updated_at": time.time(),
            "condition_id": self.condition_id,
            "portfolio": state,
            "active_quotes": list(self.active_quotes.values()),
            "last_deskew": asdict(self.latest_deskew_decision) if self.latest_deskew_decision else None,
            "epoch_index": self.epoch_index,
            "recent_fills": [f.to_dict() for f in self.fills_history[-10:]],
            "recent_merges": [m.to_dict() for m in self.merges_history[-10:]],
        }

        try:
            with open(self.ledger_path, "w", encoding="utf-8") as f:
                json.dump(ledger_data, f, indent=2)
        except Exception as e:
            log.warning(f"Failed to write JSON ledger: {e}")

    def get_telemetry(self) -> dict[str, Any]:
        """Comprehensive real-time telemetry dictionary for dashboards and CLI runners."""
        marks = {
            self.condition_id: (
                self.book_yes.mid_price or 0.50,
                self.book_no.mid_price or 0.50,
            )
        }
        port_state = self.portfolio.get_state(marks)
        pos = self.portfolio.get_position(self.condition_id)

        mid_yes = self.book_yes.mid_price or 0.50
        bb_yes = self.book_yes.best_bid_real or 0.0
        ba_yes = self.book_yes.best_ask_real or 1.0

        return {
            "timestamp": time.time(),
            "condition_id": self.condition_id,
            "equity": port_state["equity"],
            "cash": port_state["cash"],
            "market_value": port_state["market_value"],
            "realized_pnl": port_state["realized_pnl"],
            "unrealized_pnl": port_state["unrealized_pnl"],
            "total_pnl": port_state["total_pnl"],
            "shares_yes": pos.shares_yes,
            "shares_no": pos.shares_no,
            "net_exposure": pos.net_exposure,
            "complete_sets": pos.complete_sets,
            "avg_cost_yes": pos.avg_cost_yes,
            "avg_cost_no": pos.avg_cost_no,
            "best_bid_yes": bb_yes,
            "best_ask_yes": ba_yes,
            "mid_price_yes": mid_yes,
            "active_quotes": list(self.active_quotes.values()),
            "latest_deskew": asdict(self.latest_deskew_decision) if self.latest_deskew_decision else None,
            "epoch_index": self.epoch_index,
            "total_rewards": self.total_rewards_credited,
            "total_gas_fees": port_state["total_gas_fees"],
            "total_merges": port_state["total_merges_count"],
            "total_splits": port_state["total_splits_count"],
            "total_volume_usd": port_state["total_volume_traded_usd"],
            "fills_count": len(self.fills_history),
        }

    def close(self) -> None:
        """Close SQLite database connection and release file locks."""
        if hasattr(self, "_conn") and self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
