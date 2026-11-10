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
6. Persistence to SQLite (data/native_paper_simulator.db) and JSON ledger.

Prices come only from the live books: when a book has no two-sided quote, marks are
skipped (equity excludes the position), and decisions that need the price are aborted
via :class:`MissingBookPriceError` or an early return. No midpoint is ever invented.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from polymarketbot.fees import maker_fee_rebate, taker_fee
from polymarketbot.logs import get_logger
from polymarketbot.rewards.allocator import (
    ContinuousOptimizationGovernor,
    GovernorAction,
)
from polymarketbot.rewards.deskew import (
    DeSkewDecision,
    DeSkewPathway,
    DynamicDeSkewOptimizer,
)
from polymarketbot.rewards.microstructure import (
    MicrostructureEngine,
    compute_dynamic_cooldown_duration,
    compute_dynamic_volatility_breaker_threshold,
)
from polymarketbot.rewards.queue_valuation import (
    CapitalMigrationEngine,
    HurdleDecision,
    LiquidationRoute,
    SwitchingCostDecomposition,
)
from polymarketbot.rewards.scoring import (
    OrderQuote,
    compute_book_side_scores,
    compute_minimum_score,
    quadratic_order_score,
)
from polymarketbot.trading.base import BaseMarketMakingEngine
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


class MissingBookPriceError(RuntimeError):
    """A required price is unavailable because the live book has no two-sided quote.

    Raised instead of substituting a made-up midpoint (0.49/0.50/0.51) or the
    position's purchase price, so callers can skip the mark or abort the decision.
    """


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
    maker_rebate_usd: float = 0.0

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

class NativePaperTradingEngine(BaseMarketMakingEngine):
    """Production Polymarket paper trading orchestrator and execution simulator."""

    def __init__(
        self,
        condition_id: str,
        asset_id_yes: str = "TOKEN_YES",
        asset_id_no: str = "TOKEN_NO",
        initial_cash: float = 100.0,
        quote_size: Optional[float] = None,
        spread_offset_cents: Optional[float] = None,
        daily_reward_rate: Optional[float] = None,
        rewards_max_spread_cents: Optional[float] = None,
        rewards_min_size: Optional[float] = None,
        auto_merge_threshold: Optional[float] = None,
        gas_fee_merge: Optional[float] = None,
        institutional_mode: bool = True,
        gamma: Optional[float] = None,
        competing_score_override: Optional[float] = None,
        db_path: Union[str, Path] = "data/native_paper_simulator.db",
        ledger_path: Union[str, Path] = "data/native_paper_simulator_ledger.json",
        deskew_optimizer: Optional[DynamicDeSkewOptimizer] = None,
        migration_engine: Optional[CapitalMigrationEngine] = None,
        governor: Optional[ContinuousOptimizationGovernor] = None,
    ) -> None:
        super().__init__(
            condition_id=condition_id,
            asset_id_yes=asset_id_yes,
            asset_id_no=asset_id_no,
            quote_size=quote_size,
            spread_offset_cents=spread_offset_cents,
            daily_reward_rate=daily_reward_rate,
            rewards_max_spread_cents=rewards_max_spread_cents,
            rewards_min_size=rewards_min_size,
            auto_merge_threshold=auto_merge_threshold,
            gas_fee_merge=gas_fee_merge,
            institutional_mode=institutional_mode,
            gamma=gamma,
            competing_score_override=competing_score_override,
            db_path=db_path,
            ledger_path=ledger_path,
            deskew_optimizer=deskew_optimizer,
            migration_engine=migration_engine,
            governor=governor,
        )

        # Core Simulator Modules
        self.portfolio = SimulatedPortfolio(
            initial_cash=initial_cash,
            gas_fee_merge=self.gas_fee_merge,
            taker_fee_bps=0.0,  # 0 bps default on Polymarket binary contracts
        )
        self.book_yes = SyntheticOrderBook(asset_id=asset_id_yes)
        self.book_no = SyntheticOrderBook(asset_id=asset_id_no)
        self.impact_yes = MarketImpactEngine(book=self.book_yes)
        self.impact_no = MarketImpactEngine(book=self.book_no)
        self.matcher = OrderMatcherBridge(condition_id=self.condition_id)

        if self.condition_id not in self.governor.current_allocations:
            init_reserve_pct = getattr(self.governor.allocator, "reserve_pct", 0.25)
            self.governor.current_allocations[self.condition_id] = self.portfolio.cash * (1.0 - init_reserve_pct)

        self.fills_history: List[FillRecord] = []
        self.merges_history: List[MergeResult] = []
        self.splits_history: List[SplitResult] = []
        self._last_toxicity_log_time: float = 0.0

        log.info(
            f"NativePaperTradingEngine initialized for condition {self.condition_id[:12]}... "
            f"Initial Cash: ${initial_cash:.2f} | DB: {self.db_path}"
        )


    @property
    def fills_count(self) -> int:
        return len(self.fills_history)

    def get_cash(self) -> float:
        return self.portfolio.cash

    def get_position(self, condition_id: Optional[str] = None):
        return self.portfolio.get_position(condition_id or self.condition_id)

    # -------------------------------------------------------------------------
    # Price Discovery (no fabricated fallbacks)
    # -------------------------------------------------------------------------

    def _book_top(self, token_side: str) -> Optional[Tuple[float, float]]:
        """Two-sided top of book for one outcome token, or ``None`` when unpriced.

        A missing side is completed from the binary complement of the opposite
        token's live quote (YES ask = 1 - NO bid, and vice versa). Nothing is
        invented: if neither book quotes the missing side, ``None`` is returned.
        """
        tok = str(token_side).upper()
        book = self.book_yes if tok == "YES" else self.book_no
        comp = self.book_no if tok == "YES" else self.book_yes
        bid = book.best_bid_real
        ask = book.best_ask_real
        if bid is None and comp.best_ask_real is not None:
            bid = round(1.0 - comp.best_ask_real, 4)
        if ask is None and comp.best_bid_real is not None:
            ask = round(1.0 - comp.best_bid_real, 4)
        if bid is None or ask is None:
            return None
        return float(bid), float(ask)

    def _mid_price(self, token_side: str) -> Optional[float]:
        """Mid-price of one outcome token, or ``None`` when no two-sided quote exists.

        Prefers the book's own effective mid (which includes our resting virtual
        quotes), then the opposite book's mid complement, then the derived top.
        """
        tok = str(token_side).upper()
        book = self.book_yes if tok == "YES" else self.book_no
        if book.mid_price is not None:
            return float(book.mid_price)
        comp = self.book_no if tok == "YES" else self.book_yes
        if comp.mid_price is not None:
            return 1.0 - float(comp.mid_price)
        top = self._book_top(tok)
        if top is None:
            return None
        return (top[0] + top[1]) / 2.0

    def _current_marks(self) -> Optional[Tuple[float, float]]:
        """``(mid_yes, mid_no)`` for the traded market, or ``None`` when unpriced."""
        mid_yes = self._mid_price("YES")
        mid_no = self._mid_price("NO")
        if mid_yes is None and mid_no is None:
            return None
        if mid_yes is None:
            mid_yes = 1.0 - float(mid_no)
        if mid_no is None:
            mid_no = 1.0 - float(mid_yes)
        return float(mid_yes), float(mid_no)

    def _marked_portfolio_state(self) -> dict[str, Any]:
        """Portfolio state marked only from live books.

        Positions without a live two-sided price are left out of market value,
        equity and unrealized PnL rather than being priced at cost or at an
        invented midpoint; their condition ids are reported in
        ``unmarked_positions`` and ``mark_basis`` states which basis was used.
        """
        marks: Dict[str, Tuple[float, float]] = {}
        current = self._current_marks()
        if current is not None:
            marks[self.condition_id] = current

        state = self.portfolio.get_state(marks)
        market_value = 0.0
        unrealized = 0.0
        unmarked: List[str] = []
        for cond_id, pos in self.portfolio.positions.items():
            mark = marks.get(cond_id)
            if mark is None:
                if pos.shares_yes > 1e-9 or pos.shares_no > 1e-9:
                    unmarked.append(cond_id)
                continue
            market_value += pos.get_market_value(*mark)
            unrealized += pos.get_unrealized_pnl(*mark)

        state["market_value"] = round(market_value, 4)
        state["unrealized_pnl"] = round(unrealized, 4)
        state["equity"] = round(self.portfolio.cash + market_value, 4)
        state["total_pnl"] = round(state["equity"] - self.portfolio.initial_cash, 4)
        state["mark_basis"] = "book_mid" if not unmarked else "book_mid_unpriced_positions_excluded"
        state["unmarked_positions"] = unmarked
        return state

    def get_equity(self, prices: Optional[dict[str, tuple[float, float]]] = None) -> float:
        if prices is not None:
            return self.portfolio.get_equity(prices)
        return self._marked_portfolio_state()["equity"]

    def _position_equity(self) -> Tuple[float, str]:
        """Equity using live book mids, with the basis stated in the label.

        Positions with a live two-sided book are marked at mid. Any position without
        one is valued at its own cost basis, and the returned label says so instead of
        passing that value off as a market mark.
        """
        marks: Dict[str, Tuple[float, float]] = {}
        current = self._current_marks()
        if current is not None:
            marks[self.condition_id] = current

        equity = self.portfolio.cash
        marked = 0
        cost_based = 0
        for cond_id, pos in self.portfolio.positions.items():
            if pos.shares_yes <= 1e-9 and pos.shares_no <= 1e-9:
                continue
            mark = marks.get(cond_id)
            if mark is not None:
                equity += pos.get_market_value(*mark)
                marked += 1
            else:
                equity += pos.shares_yes * pos.avg_cost_yes + pos.shares_no * pos.avg_cost_no
                cost_based += 1

        if cost_based and marked:
            return equity, "book_mid_and_cost_basis"
        if cost_based:
            return equity, "cost_basis"
        return equity, "book_mid" if marked else "cash_only"

    def get_best_bid_ask(self, token_side: str) -> Tuple[float, float]:
        """Live two-sided top of book, raising when the market has no quote."""
        top = self._book_top(token_side)
        if top is None:
            raise MissingBookPriceError(
                f"condition {self.condition_id}: no two-sided quote for {token_side} token"
            )
        return top

    def credit_reward(self, amount: float, condition_id: str, note: str = "") -> None:
        self.portfolio.credit_reward(amount, condition_id=condition_id, note=note)

    def _set_quote(
        self,
        token_side: str,
        quote_side: str,
        price: float,
        size: float,
        ts: float,
    ) -> None:
        self._set_virtual_quote(token_side, quote_side, price, size, ts)

    def _cancel_quote(self, qid: str) -> None:
        self._cancel_virtual_quote(qid)

    def cancel_all_quotes(self) -> None:
        self.cancel_all_virtual_quotes()

    # -------------------------------------------------------------------------
    # Database Initialization & Persistence
    # -------------------------------------------------------------------------

    def _init_sqlite(self) -> None:
        """Backward-compatible alias for _init_db."""
        self._init_db()

    def _init_db(self) -> None:
        """Create high-performance SQLite schema with WAL journal mode and indices."""
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

        cur.execute("""
            CREATE TABLE IF NOT EXISTS rebalances (
                ts REAL NOT NULL,
                condition_id TEXT NOT NULL,
                crossed_cents REAL NOT NULL,
                taker_fee_usd REAL NOT NULL,
                gas_usd REAL NOT NULL,
                inventory_before REAL,
                inventory_after REAL,
                reason TEXT
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS market_migrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                from_condition_id TEXT NOT NULL,
                to_condition_id TEXT NOT NULL,
                delta_yield_daily REAL NOT NULL,
                switching_cost REAL NOT NULL,
                net_alpha_holding REAL NOT NULL,
                unlocked_cash REAL NOT NULL,
                reason TEXT NOT NULL
            );
        """)

        # Indexes for high-frequency queries and monitoring
        cur.execute("CREATE INDEX IF NOT EXISTS idx_reward_epochs_ts ON reward_epochs(timestamp);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_positions_cond ON positions(condition_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_migrations_ts ON market_migrations(timestamp);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_deskew_ts ON deskew_events(timestamp);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_rebalances_ts ON rebalances(ts);")

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

        def _get_lvl(lvl: Any) -> tuple[float, float]:
            if hasattr(lvl, "price") and hasattr(lvl, "size"):
                return float(lvl.price), float(lvl.size)
            if isinstance(lvl, dict):
                return float(lvl.get("price", 0.0) or 0.0), float(lvl.get("size", 0.0) or 0.0)
            if isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
                return float(lvl[0] or 0.0), float(lvl[1] or 0.0)
            return 0.0, 0.0

        if token_norm == "YES":
            self.book_yes.apply_snapshot(bids=bids, asks=asks, timestamp=ts)
            if bids and asks:
                b_px, b_sz = _get_lvl(bids[0])
                a_px, a_sz = _get_lvl(asks[0])
                if b_px > 0 and a_px > 0:
                    self.microstructure_yes.update(
                        best_bid_price=b_px,
                        best_bid_size=b_sz,
                        best_ask_price=a_px,
                        best_ask_size=a_sz,
                        timestamp=ts,
                    )
        elif token_norm == "NO":
            self.book_no.apply_snapshot(bids=bids, asks=asks, timestamp=ts)
            if bids and asks:
                b_px, b_sz = _get_lvl(bids[0])
                a_px, a_sz = _get_lvl(asks[0])
                if b_px > 0 and a_px > 0:
                    self.microstructure_no.update(
                        best_bid_price=b_px,
                        best_bid_size=b_sz,
                        best_ask_price=a_px,
                        best_ask_size=a_sz,
                        timestamp=ts,
                    )
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
            rebate = 0.0
            sched = getattr(self, "fee_schedule", None)
            if fill.is_maker and sched is not None and getattr(sched, "rate", 0.0) > 0:
                rebate = maker_fee_rebate(fill.fill_price, fill.fill_size, sched)
            fill.maker_rebate_usd = rebate
            # Process fill into portfolio ledger
            tx = self.portfolio.process_fill(
                condition_id=self.condition_id,
                side=fill.side,
                outcome=fill.token_side,
                price=fill.fill_price,
                size=fill.fill_size,
                is_maker=fill.is_maker,
                notes=fill.notes,
                maker_rebate_usd=rebate,
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

    @property
    def is_volatility_cooldown_active(self) -> bool:
        """Check if volatility cooldown is currently active."""
        if self._rolling_price_ticks:
            latest_tick_ts = self._rolling_price_ticks[-1][0]
            if latest_tick_ts < 1e8:  # Simulated relative timestamp
                return latest_tick_ts < self._volatility_cooldown_until
        return time.time() < self._volatility_cooldown_until

    @property
    def volatility_cooldown_until(self) -> float:
        """Timestamp until which volatility cooldown remains active."""
        return self._volatility_cooldown_until

    def _check_volatility_breaker(self, mid_yes: float, ts: Optional[float] = None) -> bool:
        """Track rolling price ticks in _check_volatility_breaker(mid_yes, ts).

        If mid-price jumps by > 2.0c within 60s, cancel active virtual quotes and enter a 300s cooldown.

        Args:
            mid_yes: Current YES token midpoint price.
            ts: Optional timestamp of the observation.

        Returns:
            True if volatility breaker triggered or cooldown currently active, False otherwise.
        """
        now = ts if ts is not None else time.time()

        # 1. If currently in cooldown, cancel/suppress quotes and return True
        if now < self._volatility_cooldown_until:
            if self.active_quotes:
                self.cancel_all_virtual_quotes()
            # Throttle repetitive logging to once every 5.0 seconds
            if now - self._last_cooldown_log_time >= 5.0:
                self._last_cooldown_log_time = now
                log.warning(
                    f"[ASYMMETRIC TOXICITY DEFENSE] Volatility cooldown active for condition {self.condition_id} "
                    f"({self._volatility_cooldown_until - now:.1f}s remaining). Virtual quotes cancelled/suppressed."
                )
            return True

        # 2. Prune rolling price ticks older than 60 seconds
        cutoff = now - 60.0
        while self._rolling_price_ticks and self._rolling_price_ticks[0][0] < cutoff:
            self._rolling_price_ticks.popleft()

        # 3. Add current tick (avoid appending redundant duplicate tick with same timestamp and price)
        if not (self._rolling_price_ticks and self._rolling_price_ticks[-1] == (now, float(mid_yes))):
            self._rolling_price_ticks.append((now, float(mid_yes)))

        # 4. Check price jump within the 60s window
        prices = [p for _, p in self._rolling_price_ticks]
        max_px = max(prices)
        min_px = min(prices)
        jump_cents = (max_px - min_px) * 100.0

        if self.price_jump_threshold_cents is not None:
            dyn_thresh = self.price_jump_threshold_cents
        else:
            dyn_thresh = compute_dynamic_volatility_breaker_threshold(mid_price=float(mid_yes), spread_cents=1.0)

        if jump_cents > dyn_thresh:
            if self.cooldown_duration_seconds is not None:
                cooldown_dur = self.cooldown_duration_seconds
            else:
                cooldown_dur = compute_dynamic_cooldown_duration(
                    jump_cents_observed=jump_cents,
                    breaker_threshold_cents=dyn_thresh,
                )
            self._volatility_cooldown_until = now + cooldown_dur
            self._trigger_instant_scan = True
            self._last_cooldown_log_time = now
            self.cancel_all_virtual_quotes()
            log.warning(
                f"[ASYMMETRIC TOXICITY DEFENSE] Dynamic price jump volatility breaker triggered: "
                f"mid-price jump {jump_cents:.2f}c > {dyn_thresh:.2f}c within 60s. "
                f"Active virtual quotes cancelled; entering {cooldown_dur:.0f}s cooldown until ts={self._volatility_cooldown_until:.1f}. "
                f"Triggering instant opportunity scanner for market rotation."
            )
            return True

        return False

    def evaluate_quotes(self, timestamp: Optional[float] = None) -> None:
        """Run 2-Tier Dynamic De-Skewing Optimizer and generate virtual maker quotes."""
        ts = timestamp or time.time()
        pos = self.portfolio.get_position(self.condition_id)

        # Get top-of-book prices (live two-sided quotes only; no invented midpoints)
        top_yes = self._book_top("YES")
        top_no = self._book_top("NO")
        if top_yes is None or top_no is None:
            if self.active_quotes:
                self.cancel_all_virtual_quotes()
            log.debug(
                f"Skipping quote evaluation for condition {self.condition_id}: "
                f"no live two-sided book (YES={'priced' if top_yes else 'unpriced'}, "
                f"NO={'priced' if top_no else 'unpriced'})"
            )
            return

        b_yes, a_yes = top_yes
        b_no, a_no = top_no

        mid_yes = (b_yes + a_yes) / 2.0

        # Check price jump volatility circuit breaker
        if self._check_volatility_breaker(mid_yes, ts):
            return

        # Extract live microstructure signals from Cont-Kukanov-Stoikov OFI and Stoikov micro-price
        state_yes = self.microstructure_yes.latest_state
        rolling_ofi = state_yes.ofi_10_events if state_yes else 0.0
        drift_cents = state_yes.drift_cents if state_yes else 0.0

        # Evaluate skew using DynamicDeSkewOptimizer with live microstructure signals
        decision = self.deskew_optimizer.evaluate_skew(
            shares_yes=pos.shares_yes,
            shares_no=pos.shares_no,
            best_bid_yes=b_yes,
            best_ask_yes=a_yes,
            best_bid_no=b_no,
            best_ask_no=a_no,
            rolling_ofi=rolling_ofi,
            micro_price_drift_cents=drift_cents,
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
            net_q = pos.shares_yes - pos.shares_no
            if abs(net_q) >= self.deskew_optimizer.q_soft:
                self._execute_tier1_passive_fade(decision, mid_yes, b_yes, a_yes, ts)
                return
            else:
                self._record_deskew_event(decision, ts)
                if ts - self._last_toxicity_log_time >= 5.0:
                    self._last_toxicity_log_time = ts
                    log.warning(
                        f"[ASYMMETRIC TOXICITY DEFENSE] {decision.reason} "
                        f"(fading {decision.action_side} {decision.target_token})"
                    )


        # ---------------------------------------------------------------------
        # NEUTRAL: Two-Sided Maker Quoting around Mid (with Asymmetric Toxicity Defense)
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
                # Size neutralizing leg to net_q to completely flatten position on fill
                deskew_size = max(self.quote_size, net_q)
                self._set_virtual_quote("NO", "BUY", target_bid_no, deskew_size, ts)
                self._set_virtual_quote("YES", "BUY", faded_bid_yes, self.quote_size, ts)
            elif net_q < 0:
                # Long NO -> tighten YES bid to complete sets; fade/widen NO bid
                target_bid_yes = round(max(0.01, min(best_ask_yes - 0.001, decision.target_price)), 3)
                faded_bid_no = round(max(0.01, min(0.98, 1.00 - (r_yes + offset + 0.01))), 3)
                # Size neutralizing leg to abs(net_q) to completely flatten position on fill
                deskew_size = max(self.quote_size, abs(net_q))
                self._set_virtual_quote("YES", "BUY", target_bid_yes, deskew_size, ts)
                self._set_virtual_quote("NO", "BUY", faded_bid_no, self.quote_size, ts)
            else:
                # Neutral inventory: route to neutral maker quoting with asymmetric toxicity defense
                self._execute_neutral_maker_quoting(mid_yes, best_bid_yes, best_ask_yes, ts)
                return

            self._cancel_virtual_quote("SELL_YES")
            self._cancel_virtual_quote("SELL_NO")
            return

        # Legacy Single-Token Mode
        if decision.target_token == "YES" and decision.action_side == "SELL":
            faded_ask = min(best_ask_yes, max(best_bid_yes + 0.001, decision.target_price))
            self._set_virtual_quote("YES", "SELL", faded_ask, self.quote_size, ts)
            self._cancel_virtual_quote("BID_YES")
            self._cancel_virtual_quote("BUY_YES")
        elif decision.target_token == "YES" and decision.action_side == "BUY":
            faded_bid = max(best_bid_yes, min(best_ask_yes - 0.001, decision.target_price))
            self._set_virtual_quote("YES", "BUY", faded_bid, self.quote_size, ts)
            self._cancel_virtual_quote("ASK_YES")
            self._cancel_virtual_quote("SELL_YES")

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
            pos_before = self.portfolio.get_position(self.condition_id)
            inventory_before = pos_before.shares_yes - pos_before.shares_no
            mid_ref = self._deskew_mid_price(target_token)
            if mid_ref is None:
                log.error(
                    f"Tier 2 de-skew aborted for condition {self.condition_id}: "
                    f"no live two-sided price for {target_token} token"
                )
                return
            crossed_cents = abs(target_price - mid_ref) * 100.0

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
            merge: Optional[MergeResult] = None
            if decision.pathway == DeSkewPathway.SYNTHETIC_MERGE:
                merge = self.trigger_ctf_merge()

            pos_after = self.portfolio.get_position(self.condition_id)
            taker_fee_usd, fee_note = self._taker_fee_usd(target_size, target_price)
            self._record_rebalance(
                ts=ts,
                crossed_cents=crossed_cents,
                taker_fee_usd=taker_fee_usd,
                gas_usd=self._merge_gas_usd(merge),
                inventory_before=inventory_before,
                inventory_after=pos_after.shares_yes - pos_after.shares_no,
                reason=f"Tier 2 {decision.pathway.value}: {decision.reason}{fee_note}",
            )

        except Exception as e:
            log.error(f"Failed to execute Tier 2 de-skew taker fill: {e}")

    def _execute_neutral_maker_quoting(
        self,
        mid_yes: float,
        best_bid_yes: float,
        best_ask_yes: float,
        ts: float,
    ) -> None:
        """Standard two-sided virtual maker quotes inside the qualifying spread with asymmetric toxicity defense."""
        if self._check_volatility_breaker(mid_yes, ts):
            return

        offset = self.spread_offset_cents / 100.0

        # Query microstructure quote actions for BUY and SELL
        action_buy = self.microstructure_yes.evaluate_quote_action("BUY")
        action_sell = self.microstructure_yes.evaluate_quote_action("SELL")

        bid_offset = offset
        ask_offset = offset
        suppress_bid = False
        suppress_ask = False

        should_log_tox = (ts - self._last_toxicity_log_time) >= 5.0
        if action_sell.should_pull:
            suppress_ask = True
            if should_log_tox:
                self._last_toxicity_log_time = ts
                log.warning(
                    f"[ASYMMETRIC TOXICITY DEFENSE] Suppressing ask quote (toxic buying flow): {action_sell.reason}"
                )
        elif action_sell.should_widen:
            widen_val = action_sell.spread_offset_cents / 100.0
            ask_offset += widen_val
            if should_log_tox:
                self._last_toxicity_log_time = ts
                log.warning(
                    f"[ASYMMETRIC TOXICITY DEFENSE] Widening ask offset by +{action_sell.spread_offset_cents:.2f}c: {action_sell.reason}"
                )

        if action_buy.should_pull:
            suppress_bid = True
            if should_log_tox:
                self._last_toxicity_log_time = ts
                log.warning(
                    f"[ASYMMETRIC TOXICITY DEFENSE] Suppressing bid quote (toxic selling flow): {action_buy.reason}"
                )
        elif action_buy.should_widen:
            widen_val = action_buy.spread_offset_cents / 100.0
            bid_offset += widen_val
            if should_log_tox:
                self._last_toxicity_log_time = ts
                log.warning(
                    f"[ASYMMETRIC TOXICITY DEFENSE] Widening bid offset by +{action_buy.spread_offset_cents:.2f}c: {action_buy.reason}"
                )


        if self.institutional_mode:
            # Jane Street Institutional Dual-Cash Quoting (BUY YES + BUY NO)
            pos = self.portfolio.get_position(self.condition_id)
            net_q = pos.shares_yes - pos.shares_no
            p = max(0.01, min(0.99, mid_yes))
            variance = (p * (1.0 - p)) / 7.0
            r_yes = max(0.01, min(0.99, mid_yes - (net_q * self.gamma * variance)))

            skew_adj = net_q * 0.0005
            target_bid_yes = r_yes - bid_offset - max(0.0, skew_adj)
            target_ask_yes = r_yes + ask_offset - min(0.0, skew_adj)

            target_bid_yes = min(best_ask_yes - 0.001, target_bid_yes)
            target_ask_yes = max(best_bid_yes + 0.001, target_ask_yes)

            target_bid_yes = round(max(0.01, min(0.98, target_bid_yes)), 3)
            target_ask_yes = round(max(target_bid_yes + 0.002, min(0.99, target_ask_yes)), 3)
            target_bid_no = round(1.00 - target_ask_yes, 3)

            # Enforce cash sizing bounds
            active_price_sum = 0.0
            if not suppress_bid:
                active_price_sum += target_bid_yes
            if not suppress_ask:
                active_price_sum += target_bid_no

            if active_price_sum > 0:
                req_cash = active_price_sum * self.quote_size
                eff_size = self.quote_size if self.portfolio.cash >= req_cash else max(5.0, math.floor(self.portfolio.cash / active_price_sum))
            else:
                eff_size = self.quote_size

            # Place pure dual-cash bids or cancel if suppressed
            if not suppress_bid:
                self._set_virtual_quote("YES", "BUY", target_bid_yes, eff_size, ts)
            else:
                self._cancel_virtual_quote("BUY_YES")

            if not suppress_ask:
                self._set_virtual_quote("NO", "BUY", target_bid_no, eff_size, ts)
            else:
                self._cancel_virtual_quote("BUY_NO")

            self._cancel_virtual_quote("SELL_YES")
            self._cancel_virtual_quote("SELL_NO")
            return

        # Legacy Single-Token Mode
        target_bid = round(max(0.01, mid_yes - bid_offset), 3)
        target_ask = round(min(0.99, mid_yes + ask_offset), 3)

        if target_bid >= best_ask_yes:
            target_bid = round(best_ask_yes - 0.001, 3)
        if target_ask <= best_bid_yes:
            target_ask = round(best_bid_yes + 0.001, 3)

        if not suppress_bid:
            self._set_virtual_quote("YES", "BUY", target_bid, self.quote_size, ts)
        else:
            self._cancel_virtual_quote("BUY_YES")

        if not suppress_ask:
            self._set_virtual_quote("YES", "SELL", target_ask, self.quote_size, ts)
        else:
            self._cancel_virtual_quote("SELL_YES")

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
            existing_mid = self._mid_price(token_side)
            if existing_mid is not None:
                vq_res = self.migration_engine.queue_model.evaluate_queue_value(
                    quote_size=existing["size"],
                    queue_ahead=q_ahead,
                    half_spread_cents=max(0.1, abs(existing["price"] - existing_mid) * 100.0),
                    token_mid_price=existing_mid,
                )
                existing["v_queue"] = round(vq_res.net_queue_value_usd, 4)
            return

        # Price revision or new quote: queue resets
        mid = self._mid_price(token_side)
        if mid is None:
            log.debug(
                f"Skipping virtual quote {qid} for condition {self.condition_id}: "
                f"no mid-price on the {token_side} book"
            )
            return

        tracker = impact.place_quote(side=quote_side, price=price, size=size, quote_id=qid, timestamp=ts)
        q_ahead = tracker.queue_ahead

        # Compute quadratic score: S(v, s) = ((v - s)/v)^2 * size
        spread_cents = abs(price - mid) * 100.0
        score = quadratic_order_score(
            max_spread_cents=self.rewards_max_spread,
            spread_cents=spread_cents,
            size=size,
            min_size=self.rewards_min_size,
        )

        vq_res = self.migration_engine.queue_model.evaluate_queue_value(
            quote_size=size,
            queue_ahead=q_ahead,
            half_spread_cents=max(0.1, spread_cents),
            token_mid_price=mid,
        )

        quote_data = {
            "quote_id": qid,
            "token_side": token_side,
            "side": quote_side,
            "price": price,
            "size": size,
            "queue_ahead": round(q_ahead, 2),
            "score": round(score, 4),
            "v_queue": round(vq_res.net_queue_value_usd, 4),
            "timestamp": ts,
            "status": "ACTIVE",
        }
        self.active_quotes[qid] = quote_data

    def _cancel_virtual_quote(self, qid: str) -> None:
        """Cancel an active virtual quote from book, tracker, and active registry."""
        if qid in self.active_quotes:
            quote_data = self.active_quotes.pop(qid)
            tok = quote_data.get("token_side", "YES")
            impact = self.impact_yes if tok == "YES" else self.impact_no
            book = self.book_yes if tok == "YES" else self.book_no
            impact.cancel_quote(qid)
            book.cancel_quote(qid)

    def cancel_all_virtual_quotes(self) -> None:
        """Cancel all resting virtual maker quotes across all sides and tokens."""
        qids = list(self.active_quotes.keys())
        for qid in qids:
            self._cancel_virtual_quote(qid)

    def update_quote_size(self, new_quote_size: float, timestamp: Optional[float] = None) -> bool:
        """Safely update self.quote_size and re-evaluate active resting virtual quotes if size changes by >= 5.0 shares.

        Args:
            new_quote_size: Target new quote size in shares.
            timestamp: Optional timestamp for quote placement.

        Returns:
            True if quote size changed by >= 5.0 shares and quotes were re-evaluated, False otherwise.
        """
        new_size = round(max(5.0, float(new_quote_size)), 1)
        size_diff = abs(new_size - self.quote_size)
        old_size = self.quote_size
        self.quote_size = new_size

        if size_diff >= 5.0:
            ts = timestamp or time.time()
            log.info(
                f"Quote size compounded: {old_size:.1f} -> {new_size:.1f} shares "
                f"(diff={size_diff:.1f} >= 5.0). Re-evaluating active resting quotes."
            )
            # Re-evaluate active resting virtual quotes with updated size
            if self.active_quotes:
                for qid, qdata in list(self.active_quotes.items()):
                    tok_side = qdata.get("token_side", "YES")
                    side = qdata.get("side", "BUY")
                    px = qdata.get("price")
                    if px is None:
                        continue
                    self._set_virtual_quote(tok_side, side, float(px), self.quote_size, ts)
            self.evaluate_quotes(timestamp=ts)
            return True

        return False

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

    def _record_rebalance(
        self,
        *,
        ts: float,
        crossed_cents: float,
        taker_fee_usd: float,
        gas_usd: float,
        inventory_before: Optional[float],
        inventory_after: Optional[float],
        reason: str,
    ) -> None:
        """Persist one inventory-crossing rebalance cost observation (append-only)."""
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO rebalances (
                ts, condition_id, crossed_cents, taker_fee_usd, gas_usd,
                inventory_before, inventory_after, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                float(ts),
                self.condition_id,
                float(crossed_cents),
                float(taker_fee_usd),
                float(gas_usd),
                None if inventory_before is None else float(inventory_before),
                None if inventory_after is None else float(inventory_after),
                reason,
            ),
        )
        self._conn.commit()

    def _deskew_mid_price(self, token_side: str) -> Optional[float]:
        """Mid-price of the rebalanced token from the live book, or ``None`` when unpriced."""
        top = self._book_top(token_side)
        if top is None:
            return None
        return (top[0] + top[1]) / 2.0

    @staticmethod
    def _merge_gas_usd(merge: Optional[MergeResult]) -> float:
        """Gas for a merge/split executed as part of the same rebalancing action.

        The portfolio charges the merge estimate captured at engine start-up
        (``trading.ctf.estimate_merge_cost_usd``); fall back to a fresh estimate
        only when the merge reported no gas at all.
        """
        if merge is None:
            return 0.0
        charged = float(getattr(merge, "gas_fee_deducted", 0.0) or 0.0)
        if charged > 0.0:
            return charged
        from polymarketbot.trading.ctf import estimate_merge_cost_usd

        return float(estimate_merge_cost_usd())

    def _taker_fee_usd(self, size: float, price: float) -> Tuple[float, str]:
        """Taker fee for a crossing fill, plus a reason suffix when unavailable.

        The simulator holds no per-market fee schedule, so it never invents a
        fee: an absent schedule records 0.0 and says so.
        """
        schedule = getattr(self, "fee_schedule", None)
        if schedule is None:
            return 0.0, "; fee schedule missing"
        return float(taker_fee(size, price, schedule)), ""

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

    def liquidate_all_positions(self, timestamp: Optional[float] = None) -> dict[str, Any]:
        """Liquidate all open positions into live bid depth, accounting for real closing costs.

        Rules:
        1. Cancels all active resting maker quotes across all tokens.
        2. Merges complete sets first: min(YES, NO) -> $1.00 USDC (-$0.015 Polygon PoS gas fee).
        3. For any residual directional tokens (unpaired YES or NO):
           - Sells aggressively into real CLOB bids by walking the order book depth ladder.
           - Deducts the full bid-ask spread crossing friction, depth slippage, and taker fees.
           - Sells only the depth that exists: an unscathed lot with no live bids stays in
             inventory and is reported under ``unliquidated_shares``; no clearing price is
             invented to force a 100% cash result.
        4. Recomputes the ledger so every sold lot is cash and reports the pre-liquidation
           equity with its mark basis (``pre_liquidation_equity_basis``).
        5. Returns a comprehensive liquidation report.
        """
        ts = timestamp or time.time()
        self.cancel_all_virtual_quotes()

        initial_cash = self.portfolio.initial_cash
        pre_liq_cash = self.portfolio.cash
        pos = self.portfolio.get_position(self.condition_id)

        pre_equity, pre_equity_basis = self._position_equity()

        merges_executed = []
        liquidation_fills = []
        total_gas_fees_liq = 0.0
        spread_crossing_loss = 0.0

        # Step 1: Merge any complete sets first (zero spread crossing cost, $0.015 gas)
        if pos.complete_sets > 0:
            m_res = self.trigger_ctf_merge(pos.complete_sets)
            if m_res is not None:
                merges_executed.append(m_res)
                total_gas_fees_liq += m_res.gas_fee_deducted

        # Step 2: Liquidate residual directional tokens into live bids only.
        # Never invent a clearing price: unsold depth is reported, not priced.
        liquidated: Dict[str, float] = {"YES": 0.0, "NO": 0.0}
        unliquidated: Dict[str, float] = {}
        for outcome, book in (("YES", self.book_yes), ("NO", self.book_no)):
            residual = self.portfolio.get_position(self.condition_id)
            shares_to_sell = residual.shares_yes if outcome == "YES" else residual.shares_no
            if shares_to_sell <= 1e-4:
                continue

            sale = self._sell_residual_into_bids(outcome, shares_to_sell, book, ts)
            shares_sold = sale["shares_sold"]
            if shares_sold > 1e-6:
                liquidated[outcome] = shares_sold
                spread_crossing_loss += sale["spread_crossing_loss"]
                liquidation_fills.append({
                    "outcome": outcome,
                    "shares": shares_sold,
                    "avg_price": sale["avg_price"],
                    "gross_proceeds": sale["gross_proceeds"],
                    "spread_crossing_loss": sale["spread_crossing_loss"],
                })
            if sale["unsold_shares"] > 1e-6:
                unliquidated[outcome] = sale["unsold_shares"]
                log.warning(
                    f"Shutdown liquidation left {sale['unsold_shares']:.2f} {outcome} unsold for "
                    f"condition {self.condition_id}: no live bid depth (no price is invented)"
                )

        self.flush_ledger()
        final_cash = self.portfolio.cash
        # Unsold lots keep their cost basis in the total: reporting cash minus initial
        # cash alone would count their purchase cost as a realized loss.
        remaining_inventory_cost = sum(
            held.shares_yes * held.avg_cost_yes + held.shares_no * held.avg_cost_no
            for held in self.portfolio.positions.values()
        )
        net_realized_pnl = final_cash + remaining_inventory_cost - initial_cash
        net_return_pct = (net_realized_pnl / initial_cash) * 100.0 if initial_cash > 0 else 0.0
        pos_after = self.portfolio.get_position(self.condition_id)
        open_positions = 1 if (pos_after.shares_yes > 1e-4 or pos_after.shares_no > 1e-4) else 0

        return {
            "initial_cash": initial_cash,
            "pre_liquidation_equity": pre_equity,
            "pre_liquidation_equity_basis": pre_equity_basis,
            "pre_liquidation_cash": pre_liq_cash,
            "shares_yes_liquidated": liquidated["YES"],
            "shares_no_liquidated": liquidated["NO"],
            "unliquidated_shares": unliquidated,
            "remaining_inventory_cost": remaining_inventory_cost,
            "final_cash_settlement": final_cash,
            "final_open_positions_count": open_positions,
            "total_realized_pnl": net_realized_pnl,
            "net_return_pct": net_return_pct,
            "spread_crossing_loss": spread_crossing_loss,
            "total_gas_fees_liq": total_gas_fees_liq,
            "liquidation_costs_total": spread_crossing_loss + total_gas_fees_liq,
            "merges_executed": len(merges_executed),
            "fills_executed": len(liquidation_fills),
        }

    def _sell_residual_into_bids(
        self,
        outcome: str,
        shares: float,
        book: SyntheticOrderBook,
        ts: float,
    ) -> dict[str, Any]:
        """Sell residual inventory into the live bid ladder, without inventing prices.

        Walks the visible real bids and sells only what that depth can absorb. A lot
        with no live bids, or depth that runs out, leaves the remainder in inventory
        (reported as ``unsold_shares``) instead of clearing it at a made-up price.
        """
        real_bids = sorted(book._real_bids.items(), key=lambda x: x[0], reverse=True)
        shares_remaining = float(shares)
        total_proceeds = 0.0

        for px, sz in real_bids:
            if sz <= 1e-6 or shares_remaining <= 1e-6:
                continue
            fill_amt = min(shares_remaining, sz)
            total_proceeds += fill_amt * px
            shares_remaining -= fill_amt
            if shares_remaining <= 1e-6:
                break

        shares_sold = float(shares) - shares_remaining
        if shares_sold <= 1e-6:
            return {
                "shares_sold": 0.0,
                "unsold_shares": float(shares),
                "avg_price": None,
                "gross_proceeds": 0.0,
                "spread_crossing_loss": 0.0,
            }

        avg_sell_px = total_proceeds / shares_sold
        mid_ref = book.mid_price
        spread_loss = (
            max(0.0, (mid_ref - avg_sell_px) * shares_sold) if mid_ref is not None else 0.0
        )
        slippage_cents = (mid_ref - avg_sell_px) * 100.0 if mid_ref is not None else 0.0
        note_suffix = "" if mid_ref is not None else "; mid unavailable"

        inv_before = self.portfolio.get_position(self.condition_id)
        inventory_before = inv_before.shares_yes - inv_before.shares_no
        self.portfolio.process_fill(
            condition_id=self.condition_id,
            side="SELL",
            outcome=outcome,
            price=avg_sell_px,
            size=shares_sold,
            is_maker=False,
            notes=f"Shutdown Taker Liquidation: Sold {shares_sold:.1f} {outcome} @ avg ${avg_sell_px:.3f}",
        )
        pos_after = self.portfolio.get_position(self.condition_id)
        self._record_rebalance(
            ts=ts,
            crossed_cents=abs(slippage_cents),
            taker_fee_usd=self._taker_fee_usd(shares_sold, avg_sell_px)[0],
            gas_usd=0.0,
            inventory_before=inventory_before,
            inventory_after=pos_after.shares_yes - pos_after.shares_no,
            reason=f"Shutdown Taker Liquidation: sold residual {outcome}{note_suffix}",
        )
        self.fills_history.append(
            FillRecord(
                fill_id=str(uuid.uuid4())[:8],
                timestamp=ts,
                condition_id=self.condition_id,
                token_side=outcome,
                side="SELL",
                fill_price=avg_sell_px,
                fill_size=shares_sold,
                is_maker=False,
                slippage_cents=slippage_cents,
                notes="Shutdown Taker Liquidation",
            )
        )
        return {
            "shares_sold": shares_sold,
            "unsold_shares": shares_remaining,
            "avg_price": avg_sell_px,
            "gross_proceeds": total_proceeds,
            "spread_crossing_loss": spread_loss,
        }

    # -------------------------------------------------------------------------
    # Dynamic Opportunistic Switching & Capital Migration
    # -------------------------------------------------------------------------

    def compute_resting_queue_value(self) -> float:
        """Compute the total economic value ($V_queue) of resting FIFO queue priority across all active quotes."""
        total_v = 0.0
        for q in self.active_quotes.values():
            sz = float(q.get("size", 0.0))
            if sz <= 0.0:
                continue
            q_ahead = float(q.get("queue_ahead", 0.0))
            tok = q.get("token_side", "YES")
            mid = self._mid_price(tok)
            if mid is None:
                # No live mid: the quote's queue value is unknown, so it contributes nothing.
                continue
            px = float(q.get("price", mid))
            spread_c = abs(px - mid) * 100.0
            val = self.migration_engine.queue_model.evaluate_queue_value(
                quote_size=sz,
                queue_ahead=q_ahead,
                half_spread_cents=max(0.1, spread_c),
                token_mid_price=mid,
            )
            total_v += val.net_queue_value_usd
        return round(total_v, 4)

    def get_current_daily_yield(self) -> float:
        """Calculate current daily reward payout ($/day) based on active quote score or empirical run-rate."""
        user_score = sum(q.get("score", 0.0) for q in self.active_quotes.values())
        if user_score <= 0.0 and self.quote_size > 0.0 and self.rewards_max_spread > 0.0:
            # Expected target quote score for current market
            v = self.rewards_max_spread
            s = self.spread_offset_cents
            if s < v:
                eff_score = ((v - s) / v) ** 2 * self.quote_size
                # Since dual-cash quotes 2 sides (YES and NO)
                user_score = eff_score * 2.0

        comp_score = self.competing_score_override if self.competing_score_override is not None else 1000.0
        tot = user_score + comp_score
        share_payout = (user_score / tot) * self.daily_reward_rate if tot > 0 else 0.0
        if self.epoch_index >= 5 and self.total_rewards_credited > 0:
            proj = (self.total_rewards_credited / self.epoch_index) * 1440.0
            return max(share_payout, proj)
        return share_payout

    def compute_switching_cost(
        self,
        daily_yield: Optional[float] = None,
        transit_latency_seconds: float = 4.5,
    ) -> SwitchingCostDecomposition:
        """Compute exact economic switching friction to migrate away from active market M_A.

        Accounts for:
        1. Resting FIFO queue priority value surrendered (V_queue)
        2. Inventory liquidation friction (direct crossing vs synthetic completion)
        3. Complete set CTF merge gas
        4. Opportunity cost during unquoted transit / qualification downtime

        Raises:
            MissingBookPriceError: when the live books do not quote both tokens.
        """
        pos = self.portfolio.get_position(self.condition_id)
        net_q = pos.shares_yes - pos.shares_no

        top_yes = self._book_top("YES")
        top_no = self._book_top("NO")
        if top_yes is None or top_no is None:
            raise MissingBookPriceError(
                f"condition {self.condition_id}: switching cost needs a live two-sided "
                f"quote on both tokens (YES={'priced' if top_yes else 'unpriced'}, "
                f"NO={'priced' if top_no else 'unpriced'})"
            )

        mid_yes = (top_yes[0] + top_yes[1]) / 2.0
        bid_yes = top_yes[0]
        ask_no = top_no[1]

        bid_quote = self.active_quotes.get("BUY_YES")
        ask_quote = self.active_quotes.get("BUY_NO") or self.active_quotes.get("SELL_YES")

        resting_bid_sz = float(bid_quote.get("size", 0.0)) if bid_quote else 0.0
        q_ahead_bid = float(bid_quote.get("queue_ahead", 0.0)) if bid_quote else 0.0

        resting_ask_sz = float(ask_quote.get("size", 0.0)) if ask_quote else 0.0
        q_ahead_ask = float(ask_quote.get("queue_ahead", 0.0)) if ask_quote else 0.0

        y_a = daily_yield if daily_yield is not None else self.get_current_daily_yield()

        decomp = self.migration_engine.compute_switching_cost(
            q_a=net_q,
            p_mid_a=mid_yes,
            p_bid_a=bid_yes,
            p_ask_opp_a=ask_no,
            resting_bid_size=resting_bid_sz,
            resting_ask_size=resting_ask_sz,
            queue_ahead_bid=q_ahead_bid,
            queue_ahead_ask=q_ahead_ask,
            half_spread_cents_a=self.spread_offset_cents,
            daily_yield_a=y_a,
            has_matched_complete_sets=(pos.complete_sets > 0),
            transit_latency_seconds=transit_latency_seconds,
        )
        self.latest_switching_cost = decomp
        return decomp

    def evaluate_migration_hurdle(
        self,
        candidate_daily_yield: float,
        holding_horizon_hours: float = 4.0,
        daily_yield_a: Optional[float] = None,
    ) -> Tuple[bool, HurdleDecision, SwitchingCostDecomposition]:
        """Evaluate Net Alpha Hurdle: Delta_Yield * Tau >= Cost_switch.

        Raises:
            MissingBookPriceError: propagated from :meth:`compute_switching_cost` when
                the live books do not quote both tokens.
        """
        y_a = daily_yield_a if daily_yield_a is not None else self.get_current_daily_yield()
        cost = self.compute_switching_cost(daily_yield=y_a)
        decision = self.migration_engine.evaluate_switching_hurdle(
            y_net_a_daily=y_a,
            y_net_b_daily=candidate_daily_yield,
            switching_cost_usd=cost.total_switching_cost_usd,
            holding_horizon_hours=holding_horizon_hours,
        )
        self.latest_hurdle_decision = decision
        return decision.should_switch, decision, cost

    def migrate_to_market(
        self,
        new_condition_id: str,
        new_asset_id_yes: str,
        new_asset_id_no: str,
        new_daily_reward_rate: float,
        new_rewards_max_spread_cents: float,
        new_rewards_min_size: float,
        new_competing_score: Optional[float] = None,
        new_quote_size: Optional[float] = None,
        new_spread_offset_cents: Optional[float] = None,
        reason: str = "",
        timestamp: Optional[float] = None,
    ) -> dict[str, Any]:
        """Execute clean atomic market migration:

        1. Cancel quotes on M_A.
        2. Merge complete sets on M_A to unlock cash.
        3. Register M_B and reset synthetic order books and impact trackers.
        4. Flush persistence and return migration audit record.
        """
        ts = timestamp or time.time()
        old_cid = self.condition_id

        # 1. Cleanly cancel all quotes on M_A
        self.cancel_all_virtual_quotes()

        # 2. Merge complete sets on M_A
        pos = self.portfolio.get_position(old_cid)
        unlocked_cash = 0.0
        if pos.complete_sets > 0:
            m_res = self.trigger_ctf_merge(pos.complete_sets)
            if m_res is not None:
                unlocked_cash = m_res.net_cash_unlocked

        # 3. Register M_B
        self.condition_id = new_condition_id
        self.asset_id_yes = new_asset_id_yes
        self.asset_id_no = new_asset_id_no
        self.daily_reward_rate = float(new_daily_reward_rate)
        self.rewards_max_spread = float(new_rewards_max_spread_cents)
        self.rewards_min_size = float(new_rewards_min_size)
        if new_competing_score is not None:
            self.competing_score_override = float(new_competing_score)
        if new_quote_size is not None:
            self.quote_size = float(new_quote_size)
        if new_spread_offset_cents is not None:
            self.spread_offset_cents = float(new_spread_offset_cents)

        # Re-initialize books, impacts, and matchers
        self.book_yes = SyntheticOrderBook(asset_id=new_asset_id_yes)
        self.book_no = SyntheticOrderBook(asset_id=new_asset_id_no)
        self.impact_yes = MarketImpactEngine(book=self.book_yes)
        self.impact_no = MarketImpactEngine(book=self.book_no)
        self.matcher = OrderMatcherBridge(condition_id=self.condition_id)
        self.microstructure_yes = MicrostructureEngine(ofi_threshold=850.0, drift_threshold_cents=1.5)
        self.microstructure_no = MicrostructureEngine(ofi_threshold=850.0, drift_threshold_cents=1.5)
        self.active_quotes.clear()
        self._rolling_price_ticks.clear()
        self._volatility_cooldown_until = 0.0
        self._trigger_instant_scan = False
        self._last_cooldown_log_time = 0.0

        # Update governor tracking for migration
        self.governor.market_entry_times[new_condition_id] = ts
        init_reserve_pct = getattr(self.governor.allocator, "reserve_pct", 0.25)
        self.governor.current_allocations[new_condition_id] = self.portfolio.cash * (1.0 - init_reserve_pct)
        self.governor.current_allocations.pop(old_cid, None)
        self.governor.market_entry_times.pop(old_cid, None)

        # Record event
        delta_y = self.latest_hurdle_decision.delta_yield_daily_usd if self.latest_hurdle_decision else 0.0
        cost_val = self.latest_switching_cost.total_switching_cost_usd if self.latest_switching_cost else 0.0
        net_alpha = self.latest_hurdle_decision.net_alpha_holding_usd if self.latest_hurdle_decision else 0.0
        reason_str = reason or (self.latest_hurdle_decision.reason if self.latest_hurdle_decision else "Clean market migration")

        evt = {
            "timestamp": ts,
            "from_condition_id": old_cid,
            "to_condition_id": new_condition_id,
            "delta_yield_daily": delta_y,
            "switching_cost": cost_val,
            "net_alpha_holding": net_alpha,
            "unlocked_cash": unlocked_cash,
            "reason": reason_str,
        }
        self.migration_history.append(evt)

        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO market_migrations (
                timestamp, from_condition_id, to_condition_id, delta_yield_daily,
                switching_cost, net_alpha_holding, unlocked_cash, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                old_cid,
                new_condition_id,
                delta_y,
                cost_val,
                net_alpha,
                unlocked_cash,
                reason_str,
            ),
        )
        self._conn.commit()
        self.flush_ledger()

        log.info(
            f"[MARKET MIGRATION] Executed migration: {old_cid[:12]} -> {new_condition_id[:12]} | "
            f"Unlocked: ${unlocked_cash:.2f} USDC | {reason_str}"
        )
        return evt

    # -------------------------------------------------------------------------
    # Polymarket Quadratic Rewards 1-Minute Epoch Scoring
    # -------------------------------------------------------------------------

    def _epoch_maker_quotes(self) -> List[OrderQuote]:
        """Build official-scoring quotes from currently resting virtual maker quotes.

        Each resting quote contributes exactly once: YES bids / NO asks feed Q_one,
        YES asks / NO bids feed Q_two (see ``compute_book_side_scores``). Every quote is
        treated as having rested the full sampled minute (``duration_seconds=60``).
        """
        quotes: List[OrderQuote] = []
        for q in self.active_quotes.values():
            price = float(q.get("price", 0.0) or 0.0)
            size = float(q.get("size", 0.0) or 0.0)
            if size <= 0.0 or not (0.0 < price < 1.0):
                continue
            quotes.append(
                OrderQuote(
                    side=str(q.get("side", "BUY")).upper(),
                    outcome=str(q.get("token_side", "YES")).upper(),
                    price=price,
                    size=size,
                    duration_seconds=60.0,
                )
            )
        return quotes

    def _epoch_competitor_quotes(self) -> List[OrderQuote]:
        """Build official-scoring quotes from real (non-virtual) CLOB depth.

        Both outcome-token books are included so competing liquidity is scored on the
        same Q_one/Q_two basis as our own quotes instead of being summed across sides.
        Levels are price-aggregated depth, so the per-order ``min_size`` filter is not
        applied (``min_size=0`` at the call site), which keeps the competing score
        conservative.
        """
        quotes: List[OrderQuote] = []
        for book, outcome in ((self.book_yes, "YES"), (self.book_no, "NO")):
            for px, sz in book._real_bids.items():
                if sz > 0.0:
                    quotes.append(
                        OrderQuote(side="BUY", outcome=outcome, price=float(px), size=float(sz), duration_seconds=60.0)
                    )
            for px, sz in book._real_asks.items():
                if sz > 0.0:
                    quotes.append(
                        OrderQuote(side="SELL", outcome=outcome, price=float(px), size=float(sz), duration_seconds=60.0)
                    )
        return quotes

    def process_reward_epoch(self, timestamp: Optional[float] = None) -> dict[str, Any]:
        """Compute 1-minute reward epoch payout with $1.00 daily payout floor enforcement.

        Formula:
            S(v, s) = ((v - s) / v)^2 * size
            Q_one  = YES bids + NO asks scores;  Q_two = YES asks + NO bids scores
            Q_min  = max(min(Q_one, Q_two), max(Q_one, Q_two)/3)  for mid in [0.10, 0.90]
                     min(Q_one, Q_two)                            outside [0.10, 0.90]
            Payout_epoch = (Q_min_user / (Q_min_user + Q_min_competing)) * (R_daily / 1440)
            Enforces: Projected daily payout must be >= $1.00; otherwise payout = $0.00.

        Raises:
            MissingBookPriceError: when the YES book has no live mid-price; scoring is
                skipped instead of being run against an invented midpoint.
        """
        ts = timestamp or time.time()

        mid_yes = self._mid_price("YES")
        if mid_yes is None:
            raise MissingBookPriceError(
                f"condition {self.condition_id}: reward epoch needs a live YES mid-price"
            )

        self.epoch_index += 1
        self.last_epoch_ts = ts

        # Official two-sided scoring for our own resting quotes (never Q1 + Q2)
        q_one, q_two = compute_book_side_scores(
            quotes=self._epoch_maker_quotes(),
            mid_price_yes=mid_yes,
            max_spread_cents=self.rewards_max_spread,
            min_size=self.rewards_min_size,
        )
        total_user_score = compute_minimum_score(q_one=q_one, q_two=q_two, midpoint=mid_yes)

        # Competing depth scored on the same Q_min basis from the real book
        comp_q_one, comp_q_two = compute_book_side_scores(
            quotes=self._epoch_competitor_quotes(),
            mid_price_yes=mid_yes,
            max_spread_cents=self.rewards_max_spread,
            min_size=0.0,
        )
        competing_score = compute_minimum_score(q_one=comp_q_one, q_two=comp_q_two, midpoint=mid_yes)

        # Baseline floor for competing depth (override with true L2 depth if provided)
        if self.competing_score_override is not None and self.competing_score_override > 0.0:
            competing_score = max(self.competing_score_override, competing_score)
        else:
            competing_score = max(0.0, competing_score)

        # Pool Share
        total_pool_score = total_user_score + competing_score
        pool_share_pct = (total_user_score / total_pool_score) * 100.0 if total_pool_score > 0 else 0.0

        # Daily rate divided across 1,440 daily epochs
        epoch_budget_usd = self.daily_reward_rate / 1440.0
        raw_epoch_payout = (total_user_score / total_pool_score) * epoch_budget_usd if total_pool_score > 0 else 0.0
        projected_daily_usd = raw_epoch_payout * 1440.0

        # $1.00 daily floor: Polymarket pays a maker only when that maker's
        # earnings in this market on that day reach $1; project this epoch across
        # the day and gate on the maker's share, not on the pool size.
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
            f"Epoch {self.epoch_index} processed: Q1={q_one:.2f} Q2={q_two:.2f} Q_min={total_user_score:.2f} "
            f"| Competing Q_min={competing_score:.2f} | Share={pool_share_pct:.3f}% "
            f"| Payout=${actual_epoch_payout:.4f} (Projected Daily: ${projected_daily_usd:.2f}, Floor Met: {floor_met}) "
            f"| Total Rewards: ${self.total_rewards_credited:.4f}"
        )

        # ---------------------------------------------------------------------
        # Continuous Optimization Governor: Dynamic Quote Size Compounding
        # ---------------------------------------------------------------------
        cand = {
            "condition_id": self.condition_id,
            "question": f"Market {self.condition_id[:12]}",
            "daily_pool": self.daily_reward_rate,
            "competing_score": max(1.0, competing_score),
            "max_spread": self.rewards_max_spread,
            "min_size": self.rewards_min_size,
            "mid_yes": mid_yes,
            "real_mid": mid_yes,
        }
        gov_actions = self.governor.evaluate_governor_cycle(
            current_active_markets=[self.condition_id],
            live_bankroll_usd=self.portfolio.cash,
            scanned_universe=[cand],
            current_time=ts,
        )
        for act in gov_actions:
            if act.action_type == "RESIZE_QUOTE" and act.condition_id == self.condition_id:
                log.info(
                    f"ContinuousOptimizationGovernor recommended RESIZE_QUOTE for {self.condition_id[:12]}: "
                    f"New target size={act.target_quote_size:.1f} shares. Compounding into resting quotes."
                )
                self.update_quote_size(act.target_quote_size, timestamp=ts)

        return {
            "epoch_index": self.epoch_index,
            "timestamp": ts,
            "q_one": round(q_one, 4),
            "q_two": round(q_two, 4),
            "q_min": round(total_user_score, 4),
            "user_score": round(total_user_score, 4),
            "competing_score": round(competing_score, 2),
            "pool_share_pct": round(pool_share_pct, 4),
            "epoch_payout_usd": round(actual_epoch_payout, 6),
            "projected_daily_usd": round(projected_daily_usd, 2),
            "floor_met": floor_met,
            "total_rewards_usd": round(self.total_rewards_credited, 4),
            "quote_size": self.quote_size,
            "governor_actions": [act.action_type for act in gov_actions],
        }

    # -------------------------------------------------------------------------
    # Ledger Flushing & Telemetry
    # -------------------------------------------------------------------------

    def flush_ledger(self) -> None:
        """Write atomic state snapshot to SQLite and JSON ledger."""
        state = self._marked_portfolio_state()

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

        # Write JSON ledger atomically
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
        self._save_ledger(ledger_data)

    def _save_ledger(self, ledger_data: Optional[dict[str, Any]] = None) -> None:
        """Atomically persist ledger data to JSON file via temporary file replacement.

        Writes to a temporary file (<path>.tmp) and atomically renames it to <path>
        using os.replace to guarantee zero file corruption if the process is killed
        or interrupted mid-write.
        """
        if ledger_data is None:
            state = self._marked_portfolio_state()
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

        tmp_path = Path(f"{self.ledger_path}.tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(ledger_data, f, indent=2)
            os.replace(tmp_path, self.ledger_path)
        except Exception as e:
            log.warning(f"Failed to write JSON ledger atomically: {e}")
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def get_telemetry(self) -> dict[str, Any]:
        """Comprehensive real-time telemetry dictionary for dashboards and CLI runners.

        Price fields are ``None`` when the live book has no two-sided quote; no
        stand-in midpoint is reported.
        """
        port_state = self._marked_portfolio_state()
        pos = self.portfolio.get_position(self.condition_id)

        mid_yes = self._mid_price("YES")
        bb_yes = self.book_yes.best_bid_real
        ba_yes = self.book_yes.best_ask_real

        v_queue_total = self.compute_resting_queue_value()
        equity_val = max(1e-4, port_state["equity"])
        cash_val = port_state["cash"]
        reserve_ratio_pct = (cash_val / equity_val) * 100.0
        daily_yield = self.get_current_daily_yield()
        init_cash = max(1.0, self.portfolio.initial_cash)
        nominal_apr = (daily_yield / init_cash) * 365.25 * 100.0
        r_daily = max(0.0, daily_yield / init_cash)
        try:
            compounding_apr = (
                (math.pow(1.0 + r_daily, 365.25) - 1.0) * 100.0
                if (0.0 < r_daily <= 5.0)
                else (99999.9 if r_daily > 5.0 else 0.0)
            )
        except (OverflowError, ValueError):
            compounding_apr = 99999.9

        return {
            "timestamp": time.time(),
            "condition_id": self.condition_id,
            "equity": port_state["equity"],
            "cash": port_state["cash"],
            "market_value": port_state["market_value"],
            "realized_pnl": port_state["realized_pnl"],
            "unrealized_pnl": port_state["unrealized_pnl"],
            "total_pnl": port_state["total_pnl"],
            "mark_basis": port_state["mark_basis"],
            "unmarked_positions": port_state["unmarked_positions"],
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
            "resting_queue_value_usd": v_queue_total,
            "reserve_cash_usd": round(cash_val, 2),
            "reserve_ratio_pct": round(reserve_ratio_pct, 1),
            "daily_yield_usd": round(daily_yield, 2),
            "nominal_apr_pct": round(nominal_apr, 1),
            "compounding_apr_pct": round(compounding_apr, 1),
            "migration_count": len(self.migration_history),
            "latest_hurdle_decision": asdict(self.latest_hurdle_decision) if self.latest_hurdle_decision else None,
            "latest_switching_cost": self.latest_switching_cost.to_dict() if self.latest_switching_cost else None,
        }

    def flush_wal(self) -> None:
        """Checkpoint SQLite WAL journal into main database file."""
        if hasattr(self, "_conn") and self._conn:
            try:
                cur = self._conn.cursor()
                cur.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                self._conn.commit()
            except Exception as e:
                log.warning(f"Error checkpointing WAL journal: {e}")

    def close(self) -> None:
        """Flush WAL journal, close SQLite database connection and release file locks."""
        if hasattr(self, "_conn") and self._conn:
            try:
                self.flush_wal()
            except Exception:
                pass
            try:
                self._conn.close()
            except Exception:
                pass
