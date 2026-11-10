"""Cash, inventory and equity for the paper pillars.

One invariant: every cash movement is a trade row in SQLite. Equity is
``cash + sum(shares * value)`` where ``value`` is the lot's protocol value
(par ``1.0`` once an oracle verdict exists, otherwise the last measured mark).
No lot is ever valued at its own purchase price when a market mark exists.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from polymarketbot.logs import get_logger

log = get_logger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    ts REAL NOT NULL,
    strategy TEXT NOT NULL,
    kind TEXT NOT NULL,
    condition_id TEXT NOT NULL DEFAULT '',
    token_id TEXT NOT NULL DEFAULT '',
    shares REAL NOT NULL DEFAULT 0,
    price REAL NOT NULL DEFAULT 0,
    fee REAL NOT NULL DEFAULT 0,
    cash_delta REAL NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS trades_ts ON trades (ts);
CREATE TABLE IF NOT EXISTS snapshots (
    ts REAL NOT NULL,
    strategy TEXT NOT NULL,
    cash REAL NOT NULL,
    positions_value REAL NOT NULL,
    equity REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS snapshots_ts ON snapshots (ts);
CREATE TABLE IF NOT EXISTS lots (
    strategy TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    shares REAL NOT NULL,
    cost_usd REAL NOT NULL,
    par_value REAL,
    last_mark REAL NOT NULL DEFAULT 0,
    tag TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS reward_accrual (
    ts REAL NOT NULL,
    day TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    accrued_usd REAL NOT NULL,
    priced_markets INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS reward_accrual_day ON reward_accrual (day, condition_id);
"""


@dataclass
class Lot:
    strategy: str
    condition_id: str
    token_id: str
    shares: float
    cost_usd: float
    # Protocol value per share when it is known (par 1.0 for an oracle winner,
    # 0.0 for a loser). ``None`` means the mark is the measured market price.
    par_value: Optional[float] = None
    last_mark: float = 0.0
    tag: str = ""

    def mark(self) -> float:
        if self.par_value is not None:
            return float(self.par_value)
        return float(self.last_mark) if self.last_mark > 0.0 else (
            self.cost_usd / self.shares if self.shares > 0.0 else 0.0
        )


@dataclass
class StrategyBook:
    cash: float
    lots: list[Lot] = field(default_factory=list)

    def shares(self, token_id: str) -> float:
        return sum(lot.shares for lot in self.lots if lot.token_id == token_id)

    def positions_value(self) -> float:
        return sum(lot.shares * lot.mark() for lot in self.lots)


class Ledger:
    """Thread-safe accounts plus an append-only SQLite journal."""

    def __init__(self, cash: dict[str, float], db_path: str | Path,
                 resume: bool = False) -> None:
        self._lock = threading.RLock()
        self._books = {name: StrategyBook(cash=float(value)) for name, value in cash.items()}
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        if resume:
            self._resume()

    def _resume(self) -> None:
        """Restore the last durable cash and inventory per strategy."""
        rows = self._conn.execute(
            "SELECT s.strategy, s.cash FROM snapshots s JOIN ("
            " SELECT strategy, MAX(ts) AS ts FROM snapshots GROUP BY strategy"
            ") last ON s.strategy = last.strategy AND s.ts = last.ts"
        ).fetchall()
        for strategy, cash_value in rows:
            if strategy in self._books:
                self._books[strategy].cash = float(cash_value)
        for strategy, condition_id, token_id, shares, cost_usd, par_value, mark, tag in \
                self._conn.execute(
                    "SELECT strategy, condition_id, token_id, shares, cost_usd, par_value,"
                    " last_mark, tag FROM lots"
                ):
            book = self._books.get(strategy)
            if book is None or shares <= 1e-9:
                continue
            book.lots.append(Lot(strategy, condition_id, token_id, float(shares),
                                 float(cost_usd),
                                 None if par_value is None else float(par_value),
                                 float(mark), tag))
        if rows:
            log.info(f"resumed ledger: {[(name, round(b.cash, 2)) for name, b in self._books.items()]}")

    # -- journal ---------------------------------------------------------
    def _row(
        self,
        strategy: str,
        kind: str,
        condition_id: str,
        token_id: str,
        shares: float,
        price: float,
        fee: float,
        cash_delta: float,
        detail: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO trades (ts, strategy, kind, condition_id, token_id, shares,"
            " price, fee, cash_delta, detail) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (time.time(), strategy, kind, condition_id, token_id, shares, price, fee, cash_delta, detail),
        )
        self._conn.commit()

    # -- mutations -------------------------------------------------------
    def buy(
        self,
        strategy: str,
        *,
        condition_id: str,
        token_id: str,
        shares: float,
        price: float,
        fee: float = 0.0,
        par_value: Optional[float] = None,
        tag: str = "",
    ) -> bool:
        if shares <= 0.0 or not 0.0 <= price <= 1.0:
            return False
        cost = shares * price + float(fee)
        with self._lock:
            book = self._books[strategy]
            if cost > book.cash + 1e-9:
                return False
            book.cash -= cost
            self._add_lot(book, strategy, condition_id, token_id, shares, shares * price,
                          par_value, price, tag)
            self._row(strategy, "buy", condition_id, token_id, shares, price, fee, -cost, tag)
        return True

    def sell(
        self,
        strategy: str,
        *,
        condition_id: str,
        token_id: str,
        shares: float,
        price: float,
        fee: float = 0.0,
        tag: str = "",
    ) -> float:
        """Sell shares FIFO. Returns the realised PnL of the sold lots."""
        with self._lock:
            book = self._books[strategy]
            sold_cost = self._remove_shares(book, token_id, shares)
            if sold_cost is None:
                return 0.0
            proceeds = shares * price - float(fee)
            book.cash += proceeds
            self._row(strategy, "sell", condition_id, token_id, shares, price, fee, proceeds, tag)
            return proceeds - sold_cost

    def merge(
        self,
        strategy: str,
        *,
        condition_id: str,
        token_id: str,
        pair_shares: float,
        gas: float = 0.0,
        tag: str = "ctf_merge",
    ) -> float:
        """Merge complete sets: each pair burns one share of each leg and pays $1.

        Both legs must be present; the caller passes the token whose paired
        complement is the other side of the same condition. The complement is
        resolved by the ledger from the recorded condition.
        """
        with self._lock:
            book = self._books[strategy]
            legs = [lot for lot in book.lots if lot.condition_id == condition_id]
            if not legs:
                return 0.0
            cost = self._remove_shares(book, token_id, pair_shares)
            if cost is None:
                return 0.0
            other = next(
                (lot for lot in legs if lot.token_id != token_id and lot.shares > 0.0), None
            )
            if other is None:
                return 0.0
            removed = self._remove_shares(book, other.token_id, pair_shares)
            if removed is None:
                return 0.0
            cost += removed
            proceeds = pair_shares * 1.0 - float(gas)
            book.cash += proceeds
            self._row(strategy, "merge", condition_id, token_id, pair_shares, 1.0, gas,
                      proceeds, tag)
            return proceeds - cost

    def redeem(
        self,
        strategy: str,
        *,
        condition_id: str,
        token_id: str,
        shares: float,
        gas: float = 0.0,
    ) -> float:
        with self._lock:
            book = self._books[strategy]
            cost = self._remove_shares(book, token_id, shares)
            if cost is None:
                return 0.0
            proceeds = shares * 1.0 - float(gas)
            book.cash += proceeds
            self._row(strategy, "redeem", condition_id, token_id, shares, 1.0, gas,
                      proceeds, "redeem")
            return proceeds - cost

    def write_off(
        self,
        strategy: str,
        *,
        condition_id: str,
        token_id: str,
        shares: float,
        detail: str = "loser",
    ) -> float:
        """Remove a losing lot at zero value and book the loss.

        The sibling token of an oracle verdict pays par and is redeemed; this
        one pays nothing. Leaving it marked at cost would hide the loss.
        """
        with self._lock:
            book = self._books[strategy]
            cost = self._remove_shares(book, token_id, shares)
            if cost is None:
                return 0.0
            self._row(strategy, "write_off", condition_id, token_id, shares, 0.0,
                      0.0, -cost, detail)
            return -cost

    def record_reward_accrual(
        self,
        *,
        day: str,
        condition_id: str,
        accrued_usd: float,
        priced_markets: int,
        ts: Optional[float] = None,
    ) -> None:
        """Journal one tick's per-market reward accrual.

        ``day`` is the UTC epoch day (``YYYY-MM-DD``), the period the venue pays
        on, and ``priced_markets`` is how many markets were priced in that tick
        (repeated on every row of the tick) so a reader can tell a measured zero
        from a market that was never priced. The row exists so the modelled
        accrual can be joined, per condition and per day, against the venue's
        own ``GET /rewards/user`` report: ``reward_accrual`` is the ledger side
        of that reconciliation, and ``trades`` only carries the aggregate
        credit.
        """
        with self._lock:
            self._conn.execute(
                "INSERT INTO reward_accrual"
                " (ts, day, condition_id, accrued_usd, priced_markets)"
                " VALUES (?,?,?,?,?)",
                (
                    time.time() if ts is None else float(ts),
                    str(day),
                    str(condition_id),
                    float(accrued_usd),
                    int(priced_markets),
                ),
            )
            self._conn.commit()

    def reward_accrual_by_condition(self, day: str) -> dict[str, float]:
        """Per-condition accrual summed over ``day`` (UTC epoch day)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT condition_id, SUM(accrued_usd) FROM reward_accrual"
                " WHERE day = ? GROUP BY condition_id",
                (str(day),),
            ).fetchall()
        return {str(condition_id): float(total or 0.0) for condition_id, total in rows}

    def rebates_by_condition(self, day: str) -> dict[str, float]:
        """Maker rebates credited on ``day``, per condition, from the journal."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT condition_id, SUM(cash_delta) FROM trades"
                " WHERE kind = 'credit' AND detail LIKE 'maker_rebate%'"
                " AND date(ts, 'unixepoch') = ? GROUP BY condition_id",
                (str(day),),
            ).fetchall()
        return {str(condition_id): float(total or 0.0) for condition_id, total in rows}

    # -- marks -----------------------------------------------------------
    def credit(
        self,
        strategy: str,
        *,
        amount: float,
        condition_id: str = "",
        token_id: str = "",
        detail: str = "rebate",
    ) -> None:
        """Credit a measured cash inflow (maker rebate) with a journal row."""
        if amount == 0.0:
            return
        with self._lock:
            self._books[strategy].cash += float(amount)
            self._row(strategy, "credit", condition_id, token_id, 0.0, 0.0, 0.0,
                      float(amount), detail)

    def mark(self, token_id: str, price: float) -> None:
        if not (0.0 < price < 1.0):
            return
        with self._lock:
            for book in self._books.values():
                for lot in book.lots:
                    if lot.token_id == token_id:
                        lot.last_mark = float(price)

    def set_par(self, condition_id: str, winning_token_id: str,
                par: float = 1.0) -> None:
        """Apply an oracle verdict: the winning token pays ``par``, the sibling zero.

        ``par`` is the verdict's own protocol value (1.00 for a final payout, the
        dispute-adjusted ``p_win`` for an undisputed proposal), never a blanket
        1.0 that would book unearned dispute risk as equity.
        """
        with self._lock:
            for book in self._books.values():
                for lot in book.lots:
                    if lot.condition_id != condition_id:
                        continue
                    lot.par_value = float(par) if lot.token_id == winning_token_id else 0.0

    # -- reads -----------------------------------------------------------
    def cash(self, strategy: str) -> float:
        with self._lock:
            return self._books[strategy].cash

    def positions(self, strategy: str) -> list[Lot]:
        with self._lock:
            return [lot for lot in self._books[strategy].lots if lot.shares > 1e-9]

    def equity(self) -> dict[str, float]:
        with self._lock:
            return {
                name: book.cash + book.positions_value()
                for name, book in self._books.items()
            }

    def snapshot(self) -> dict[str, float]:
        equity = self.equity()
        now = time.time()
        with self._lock:
            self._conn.execute("DELETE FROM lots")
            for name, book in self._books.items():
                self._conn.execute(
                    "INSERT INTO snapshots (ts, strategy, cash, positions_value, equity)"
                    " VALUES (?,?,?,?,?)",
                    (now, name, book.cash, book.positions_value(), equity[name]),
                )
                self._conn.executemany(
                    "INSERT INTO lots (strategy, condition_id, token_id, shares, cost_usd,"
                    " par_value, last_mark, tag) VALUES (?,?,?,?,?,?,?,?)",
                    [(name, lot.condition_id, lot.token_id, lot.shares, lot.cost_usd,
                      lot.par_value, lot.last_mark, lot.tag)
                     for lot in book.lots if lot.shares > 1e-9],
                )
            self._conn.commit()
        return equity

    # -- internals -------------------------------------------------------
    @staticmethod
    def _add_lot(
        book: StrategyBook,
        strategy: str,
        condition_id: str,
        token_id: str,
        shares: float,
        cost_usd: float,
        par_value: Optional[float],
        mark: float,
        tag: str,
    ) -> None:
        for lot in book.lots:
            if lot.token_id == token_id and lot.par_value == par_value and lot.tag == tag:
                lot.shares += shares
                lot.cost_usd += cost_usd
                lot.last_mark = mark
                return
        book.lots.append(
            Lot(strategy, condition_id, token_id, shares, cost_usd, par_value, mark, tag)
        )

    @staticmethod
    def _remove_shares(book: StrategyBook, token_id: str, shares: float) -> Optional[float]:
        remaining = float(shares)
        removed_cost = 0.0
        for lot in book.lots:
            if remaining <= 1e-12:
                break
            if lot.token_id != token_id or lot.shares <= 1e-12:
                continue
            take = min(lot.shares, remaining)
            removed_cost += lot.cost_usd * (take / lot.shares)
            lot.shares -= take
            lot.cost_usd -= lot.cost_usd * (take / (lot.shares + take))
            remaining -= take
        if remaining > 1e-9:
            return None
        book.lots = [lot for lot in book.lots if lot.shares > 1e-9]
        return removed_cost

    def close(self) -> None:
        with self._lock:
            self._conn.close()
