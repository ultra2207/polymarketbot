"""SQLite schema and persistence helpers shared by the market-making engines.

Schema creation is additive (``IF NOT EXISTS`` plus ``ALTER TABLE``) and every
write is append-only and idempotent on its primary key, so an older database
keeps working. Mixed into ``BaseMarketMakingEngine``.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Optional

from polymarketbot.strategy.onchain.pending import (
    PENDING_SETTLEMENTS_DDL,
    PENDING_SETTLEMENTS_INDEX,
    PENDING_SETTLEMENTS_UNIQUE,
)


class StoreMixin:
    """Schema creation and row inserts for the engine's SQLite database."""

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
                quote_id TEXT NOT NULL,
                condition_id TEXT NOT NULL,
                token_side TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                size REAL NOT NULL,
                queue_ahead REAL NOT NULL,
                score REAL NOT NULL,
                status TEXT NOT NULL
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fill_id TEXT NOT NULL,
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
                reason TEXT
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

        # Signed settlement transactions, written before broadcast. The hash is
        # the identity of a settlement, so a retried send can never become a
        # second merge. Additive: only the merge guard and the startup replay
        # read it, both through PendingSettlementStore.
        cur.execute(PENDING_SETTLEMENTS_DDL)
        cur.execute(PENDING_SETTLEMENTS_INDEX)
        cur.execute(PENDING_SETTLEMENTS_UNIQUE)

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

        # Measurement-only schema. Anchored here (after ctf_splits) so the
        # yield-measurement block is independent of settlement-table edits.
        self._init_yield_measurement_schema(cur)

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

        # Performance indexes for high-frequency queries and monitoring
        cur.execute("CREATE INDEX IF NOT EXISTS idx_reward_epochs_ts ON reward_epochs(timestamp);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_positions_cond ON positions(condition_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_migrations_ts ON market_migrations(timestamp);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_deskew_ts ON deskew_events(timestamp);")
        self._conn.commit()

    def _init_yield_measurement_schema(self, cur: sqlite3.Cursor) -> None:
        """Additive measurement tables: fills, rebalances, reward_payouts.

        Nothing here is read by the execution path; every write is append-only
        and idempotent on its primary key. An older database may already hold a
        legacy ``fills`` table (venue-agnostic, keyed by an autoincrement id):
        the yield columns are added with ``ALTER TABLE`` and the insert helper
        filters values down to the columns that actually exist, so a legacy
        database and a fresh one both receive the same rows.
        """
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS fills (
                fill_key TEXT PRIMARY KEY,
                ts REAL NOT NULL,
                condition_id TEXT NOT NULL,
                token_side TEXT,
                side TEXT,
                price REAL,
                size REAL,
                is_maker INTEGER,
                order_id TEXT,
                fee_usd REAL,
                maker_rebate_usd REAL,
                mid_at_fill REAL,
                best_bid_at_fill REAL,
                best_ask_at_fill REAL,
                markout_30s REAL,
                markout_5m REAL,
                markout_30m REAL,
                realized_pnl REAL,
                mode TEXT NOT NULL,
                note TEXT
            );
        """)

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS rebalances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                condition_id TEXT NOT NULL,
                pathway TEXT NOT NULL,
                crossed_cents REAL,
                taker_fee_usd REAL,
                gas_usd REAL,
                reward_score_lost REAL,
                inventory_before REAL,
                inventory_after REAL,
                note TEXT
            );
        """)

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS reward_payouts (
                date TEXT NOT NULL,
                condition_id TEXT NOT NULL,
                usd REAL NOT NULL,
                source TEXT NOT NULL,
                observed_ts REAL NOT NULL,
                PRIMARY KEY (date, condition_id, source)
            );
        """)

        # Legacy ``fills`` upgrade: add any yield column the old table lacks.
        cur.execute("PRAGMA table_info(fills);")
        existing_columns = {str(row[1]) for row in cur.fetchall()}
        yield_columns = {
            "fill_key": "TEXT",
            "ts": "REAL",
            "token_side": "TEXT",
            "side": "TEXT",
            "price": "REAL",
            "size": "REAL",
            "is_maker": "INTEGER",
            "order_id": "TEXT",
            "fee_usd": "REAL",
            "maker_rebate_usd": "REAL",
            "mid_at_fill": "REAL",
            "best_bid_at_fill": "REAL",
            "best_ask_at_fill": "REAL",
            "markout_30s": "REAL",
            "markout_5m": "REAL",
            "markout_30m": "REAL",
            "realized_pnl": "REAL",
            "mode": "TEXT",
            "note": "TEXT",
        }
        for column, sql_type in yield_columns.items():
            if column not in existing_columns:
                cur.execute(f"ALTER TABLE fills ADD COLUMN {column} {sql_type};")
                existing_columns.add(column)
        # A legacy table's autoincrement id is its primary key, so idempotency
        # comes from a unique index on the deterministic fill key instead.
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_fills_fill_key ON fills(fill_key);"
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(ts);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_fills_cond_ts ON fills(condition_id, ts);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_rebalances_ts ON rebalances(ts);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_rebalances_cond_ts ON rebalances(condition_id, ts);")

        self._fills_table_columns: set[str] = set(existing_columns)

    def _insert_yield_fill_row(self, values: dict[str, Any]) -> None:
        """Idempotently insert one measured fill, filtered to the live schema."""
        columns = getattr(self, "_fills_table_columns", None)
        payload = {
            key: val for key, val in values.items() if columns is None or key in columns
        }
        # A pre-existing (legacy) fills table carries NOT NULL columns that the
        # yield schema does not need; fill them so the row is not silently
        # ignored, then drop anything the live table does not have.
        if columns is not None and "fill_id" in columns:
            payload.setdefault("fill_id", payload.get("fill_key", ""))
            payload.setdefault("timestamp", payload.get("ts", time.time()))
            payload.setdefault(
                "liquidity_type", "maker" if payload.get("is_maker") else "taker"
            )
            payload.setdefault("slippage_cents", 0.0)
            payload.setdefault("queue_depleted", 0)
            payload.setdefault("post_fill_drift", 0.0)
            payload.setdefault("cash_after", 0.0)
            payload.setdefault("realized_pnl", 0.0)
            payload.setdefault("notes", payload.get("note"))
            payload = {key: val for key, val in payload.items() if key in columns}
        if not payload:
            return
        names = ", ".join(payload)
        placeholders = ", ".join("?" for _ in payload)
        cur = self._conn.cursor()
        cur.execute(
            f"INSERT OR IGNORE INTO fills ({names}) VALUES ({placeholders})",
            tuple(payload.values()),
        )
        self._conn.commit()

    def _insert_rebalance_row(
        self,
        *,
        pathway: str,
        ts: Optional[float] = None,
        crossed_cents: float = 0.0,
        taker_fee_usd: float = 0.0,
        gas_usd: float = 0.0,
        reward_score_lost: float = 0.0,
        inventory_before: Optional[float] = None,
        inventory_after: Optional[float] = None,
        note: str = "",
        condition_id: Optional[str] = None,
    ) -> None:
        """Record one inventory-rebalancing cost observation (append-only)."""
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO rebalances (
                ts, condition_id, pathway, crossed_cents, taker_fee_usd, gas_usd,
                reward_score_lost, inventory_before, inventory_after, note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                float(ts if ts is not None else time.time()),
                str(condition_id or self.condition_id),
                str(pathway),
                float(crossed_cents),
                float(taker_fee_usd),
                float(gas_usd),
                float(reward_score_lost),
                None if inventory_before is None else float(inventory_before),
                None if inventory_after is None else float(inventory_after),
                note or None,
            ),
        )
        self._conn.commit()

    def _insert_reward_payout(
        self,
        *,
        date: str,
        condition_id: str,
        usd: float,
        source: str,
        observed_ts: Optional[float] = None,
    ) -> None:
        """Idempotently upsert one venue-reported daily reward payout."""
        if not date or not condition_id or not source:
            return
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT OR IGNORE INTO reward_payouts (
                date, condition_id, usd, source, observed_ts
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(date),
                str(condition_id),
                float(usd),
                str(source),
                float(observed_ts if observed_ts is not None else time.time()),
            ),
        )
        self._conn.commit()
