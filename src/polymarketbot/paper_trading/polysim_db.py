"""SQLite Database for PolySimulator Paper Trading & Liquidity Rewards Tracking.

Because PolySimulator provides realistic CLOB execution and fee accounting but
honestly stubs out Polymarket's quadratic liquidity rewards engine, this module
manages a local SQLite database (data/polysim_paper_trading.db) to track:
1. Every resting maker order submitted to PolySimulator.
2. 1-minute quadratic reward sampling epochs against live Polymarket qualifying depth.
3. Daily accumulated rewards and enforcement of the $1.00 daily payout floor.
4. Account equity snapshots combining PolySimulator trading PnL with simulated rewards.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from polymarketbot.logs import get_logger

log = get_logger("polysim_db")

DEFAULT_DB_PATH = Path("data/polysim_paper_trading.db")


class PolySimDatabase:
    """SQLite storage for PolySimulator orders and quadratic reward epochs."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        """Create tables and indexes if they do not exist."""
        with self._get_connection() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                client_order_id TEXT,
                market_id TEXT,
                token_id TEXT,
                outcome TEXT,
                side TEXT,
                price REAL,
                size REAL,
                status TEXT,
                placed_at REAL,
                updated_at REAL
            );

            CREATE TABLE IF NOT EXISTS reward_epochs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL,
                market_id TEXT,
                token_id TEXT,
                mid_price REAL,
                spread_offset_cents REAL,
                max_spread_cents REAL,
                quote_size REAL,
                user_score REAL,
                competing_score REAL,
                daily_reward_pool REAL,
                epoch_reward_usd REAL
            );

            CREATE TABLE IF NOT EXISTS daily_rewards (
                date TEXT PRIMARY KEY,
                total_epochs INTEGER,
                accumulated_reward_usd REAL,
                cleared_floor INTEGER,
                payout_usd REAL,
                trading_pnl_usd REAL,
                net_pnl_usd REAL
            );

            CREATE TABLE IF NOT EXISTS account_snapshots (
                timestamp REAL PRIMARY KEY,
                cash_balance REAL,
                total_equity REAL,
                unrealized_pnl REAL,
                cumulative_rewards REAL
            );

            CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
            CREATE INDEX IF NOT EXISTS idx_reward_epochs_ts ON reward_epochs(timestamp);
            """)

    # ------------------------------------------------------------------
    # Order Tracking
    # ------------------------------------------------------------------

    def record_order(
        self,
        order_id: str,
        token_id: str,
        side: str,
        price: float,
        size: float,
        status: str = "live",
        market_id: Optional[str] = None,
        outcome: Optional[str] = None,
        client_order_id: Optional[str] = None,
    ) -> None:
        """Insert or update an active order."""
        now = time.time()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO orders (
                    order_id, client_order_id, market_id, token_id, outcome,
                    side, price, size, status, placed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                    status = excluded.status,
                    price = excluded.price,
                    size = excluded.size,
                    updated_at = excluded.updated_at
                """,
                (
                    str(order_id),
                    client_order_id or "",
                    market_id or "",
                    token_id,
                    outcome or "",
                    side.upper(),
                    price,
                    size,
                    status,
                    now,
                    now,
                ),
            )

    def update_order_status(self, order_id: str, status: str) -> None:
        """Update order lifecycle status (e.g. matched, cancelled)."""
        now = time.time()
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE orders SET status = ?, updated_at = ? WHERE order_id = ?",
                (status, now, str(order_id)),
            )

    def get_live_orders(self) -> list[dict[str, Any]]:
        """Fetch all orders currently marked as live."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM orders WHERE status = 'live'")
            return [dict(row) for row in cursor.fetchall()]

    # ------------------------------------------------------------------
    # Rewards Epoch Recording
    # ------------------------------------------------------------------

    def record_reward_epoch(
        self,
        market_id: str,
        token_id: str,
        mid_price: float,
        spread_offset_cents: float,
        max_spread_cents: float,
        quote_size: float,
        user_score: float,
        competing_score: float,
        daily_reward_pool: float,
        epoch_reward_usd: float,
        timestamp: Optional[float] = None,
    ) -> None:
        """Record a single 1-minute reward sampling epoch."""
        ts = timestamp or time.time()
        date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO reward_epochs (
                    timestamp, market_id, token_id, mid_price, spread_offset_cents,
                    max_spread_cents, quote_size, user_score, competing_score,
                    daily_reward_pool, epoch_reward_usd
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    market_id,
                    token_id,
                    mid_price,
                    spread_offset_cents,
                    max_spread_cents,
                    quote_size,
                    user_score,
                    competing_score,
                    daily_reward_pool,
                    epoch_reward_usd,
                ),
            )

            # Update daily rollup
            conn.execute(
                """
                INSERT INTO daily_rewards (
                    date, total_epochs, accumulated_reward_usd, cleared_floor,
                    payout_usd, trading_pnl_usd, net_pnl_usd
                ) VALUES (?, 1, ?, ?, ?, 0.0, ?)
                ON CONFLICT(date) DO UPDATE SET
                    total_epochs = total_epochs + 1,
                    accumulated_reward_usd = accumulated_reward_usd + excluded.accumulated_reward_usd,
                    cleared_floor = CASE WHEN (accumulated_reward_usd + excluded.accumulated_reward_usd) >= 1.0 THEN 1 ELSE 0 END,
                    payout_usd = CASE WHEN (accumulated_reward_usd + excluded.accumulated_reward_usd) >= 1.0
                                      THEN (accumulated_reward_usd + excluded.accumulated_reward_usd)
                                      ELSE 0.0 END,
                    net_pnl_usd = (CASE WHEN (accumulated_reward_usd + excluded.accumulated_reward_usd) >= 1.0
                                        THEN (accumulated_reward_usd + excluded.accumulated_reward_usd)
                                        ELSE 0.0 END) + trading_pnl_usd
                """,
                (
                    date_str,
                    epoch_reward_usd,
                    1 if epoch_reward_usd >= 1.0 else 0,
                    epoch_reward_usd if epoch_reward_usd >= 1.0 else 0.0,
                    epoch_reward_usd if epoch_reward_usd >= 1.0 else 0.0,
                ),
            )

    def get_daily_summary(self, date_str: Optional[str] = None) -> Optional[dict[str, Any]]:
        """Fetch daily reward summary for a specific date (default: today UTC)."""
        d = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM daily_rewards WHERE date = ?", (d,))
            row = cursor.fetchone()
            return dict(row) if row else None

    # ------------------------------------------------------------------
    # Account Snapshots
    # ------------------------------------------------------------------

    def record_account_snapshot(
        self,
        cash_balance: float,
        total_equity: float,
        unrealized_pnl: float,
        cumulative_rewards: float,
    ) -> None:
        """Record account state checkpoint."""
        now = time.time()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO account_snapshots (
                    timestamp, cash_balance, total_equity, unrealized_pnl, cumulative_rewards
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (now, cash_balance, total_equity, unrealized_pnl, cumulative_rewards),
            )

    def get_total_cumulative_rewards(self) -> float:
        """Get sum of all cleared daily payout rewards."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT COALESCE(SUM(payout_usd), 0.0) FROM daily_rewards")
            val = cursor.fetchone()[0]
            return float(val)
