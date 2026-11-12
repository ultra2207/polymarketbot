"""Cash, inventory and equity for the paper pillars.

One invariant: every cash movement is a trade row in SQLite. Equity is
``cash + sum(shares * value)`` where ``value`` is the lot's protocol value
(par ``1.0`` once an oracle verdict exists, otherwise the last measured mark).
No lot is ever valued at its own purchase price when a market mark exists.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from polymarketbot.logs import get_logger
from polymarketbot.storage import open_journal_connection, run_with_busy_retry

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
    tag TEXT NOT NULL DEFAULT '',
    mark_live INTEGER NOT NULL DEFAULT 0
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
    par_value: float | None = None
    last_mark: float = 0.0
    tag: str = ""
    # True while ``last_mark`` is a live book measurement. ``False`` means the
    # lot has never been measured (``last_mark`` is its purchase price) *or*
    # its book disappeared after the last measurement; the flag is surfaced so
    # equity can never silently pretend a frozen price is current.
    mark_live: bool = False

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
        # WAL + busy timeout: the dashboard reads this file while the engine
        # writes it, and in rollback-journal mode a reader blocks the writer.
        self._conn = open_journal_connection(self._db_path, check_same_thread=False)
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()
        #: Baseline for the interval reconciliation: the last committed
        #: snapshot's per-strategy components plus the trades rowid at that
        #: instant. In memory only, so a resumed run starts checking from its
        #: second snapshot instead of guessing which journal rows the previous
        #: run had already counted.
        self._prev_snapshot: dict[str, Any] | None = None
        if resume:
            self._resume()

    def _migrate(self) -> None:
        """Add columns that older soak databases do not have.

        A resumed lot from before ``mark_live`` existed cannot say whether its
        persisted mark was measured or just its purchase price, so the column
        defaults to 0 (unmeasured) and the next tick re-measures it. That is the
        honest reading: an unlabelled price is not evidence of a live book.
        """
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(lots)")}
        if "mark_live" not in columns:
            self._conn.execute(
                "ALTER TABLE lots ADD COLUMN mark_live INTEGER NOT NULL DEFAULT 0"
            )
            log.info("ledger: added lots.mark_live; resumed marks start unmeasured")

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
        for strategy, condition_id, token_id, shares, cost_usd, par_value, mark, tag, live in \
                self._conn.execute(
                    "SELECT strategy, condition_id, token_id, shares, cost_usd, par_value,"
                    " last_mark, tag, mark_live FROM lots"
                ):
            book = self._books.get(strategy)
            if book is None or shares <= 1e-9:
                continue
            book.lots.append(Lot(strategy, condition_id, token_id, float(shares),
                                 float(cost_usd),
                                 None if par_value is None else float(par_value),
                                 float(mark), tag, bool(live)))
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
        def write() -> None:
            self._conn.execute(
                "INSERT INTO trades (ts, strategy, kind, condition_id, token_id, shares,"
                " price, fee, cash_delta, detail) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (time.time(), strategy, kind, condition_id, token_id, shares, price, fee, cash_delta, detail),
            )
            self._conn.commit()

        # Safe to retry: the insert and its commit are one unit, and no in-memory
        # state is mutated here, so a retry cannot double-apply.
        run_with_busy_retry(write)

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
        par_value: float | None = None,
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
            other = next(
                (lot for lot in book.lots
                 if lot.condition_id == condition_id
                 and lot.token_id != token_id
                 and lot.shares > 1e-9),
                None,
            )
            own_shares = sum(
                lot.shares for lot in book.lots
                if lot.token_id == token_id and lot.shares > 1e-9
            )
            # Neither leg may be touched unless the merge can complete: the
            # previous order removed the first leg and then discovered the
            # sibling was missing, burning inventory with no journal row.
            if (
                other is None
                or own_shares + 1e-9 < pair_shares
                or other.shares + 1e-9 < pair_shares
            ):
                return 0.0
            cost = self._remove_shares(book, token_id, pair_shares)
            removed = self._remove_shares(book, other.token_id, pair_shares)
            if cost is None or removed is None:
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
        ts: float | None = None,
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

    def mark(self, token_id: str, price: float,
             strategy: str | None = None) -> bool:
        """Record a live book measurement on every lot of ``token_id``.

        Returns True when at least one lot transitioned from unmeasured to
        measured, so a caller can log the transition once instead of on every
        tick. The price is never invented: a bid outside ``(0, 1)`` is refused
        and the lot keeps its honest unmeasured state.
        """
        if not (0.0 < price < 1.0):
            return False
        with self._lock:
            books = (self._books[strategy],) if strategy is not None else self._books.values()
            transitioned = False
            for book in books:
                for lot in book.lots:
                    if lot.token_id == token_id:
                        lot.last_mark = float(price)
                        transitioned = transitioned or not lot.mark_live
                        lot.mark_live = True
            return transitioned

    def mark_unmeasured(self, token_id: str,
                        strategy: str | None = None) -> bool:
        """Flag lots of ``token_id`` as unmeasured because their book is gone.

        ``last_mark`` is kept as the last real measurement (not a fabricated
        price) but ``mark_live`` is cleared so readers can see that the number
        is stale rather than current. Returns True on a measured -> unmeasured
        transition, which is the once-per-transition log trigger.
        """
        with self._lock:
            books = (self._books[strategy],) if strategy is not None else self._books.values()
            transitioned = False
            for book in books:
                for lot in book.lots:
                    if lot.token_id == token_id and lot.mark_live:
                        lot.mark_live = False
                        transitioned = True
            return transitioned

    def unmarked(self, strategy: str) -> list[Lot]:
        """Open lots whose value is not backed by a live market measurement."""
        with self._lock:
            return [
                lot for lot in self._books[strategy].lots
                if lot.shares > 1e-9 and lot.par_value is None and not lot.mark_live
            ]

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

    def reconcile(self) -> dict[str, dict[str, float]]:
        """Check every book against its lots and the journal since the snapshot.

        Returns the decomposition per strategy. This is the diagnostic that
        ``snapshot`` runs immediately before committing; it is public so a
        caller (or a test) can check the books without writing a snapshot and
        without changing the numbers it is checking.
        """
        now = time.time()
        with self._lock:
            return self._reconcile_locked(now)

    def snapshot(self) -> dict[str, float]:
        """Persist cash, inventory and equity, and reconcile them first.

        This is the accounting commit, run once per engine interval (and once
        at stop), not per tick: ``equity()`` is read every tick and stays a
        straight sum, while the checks below are O(lots) plus two journal
        aggregates. A defect is logged with every component rather than
        clamped, so the journal and the reported equity can never disagree
        silently.
        """
        now = time.time()
        with self._lock:
            trades_rowid = int(
                self._conn.execute(
                    "SELECT COALESCE(MAX(rowid), 0) FROM trades"
                ).fetchone()[0]
            )
            report = self._reconcile_locked(now)
            equity = {name: parts["equity"] for name, parts in report.items()}
            self._conn.execute("DELETE FROM lots")
            for name, parts in report.items():
                self._conn.execute(
                    "INSERT INTO snapshots (ts, strategy, cash, positions_value, equity)"
                    " VALUES (?,?,?,?,?)",
                    (now, name, parts["cash"], parts["positions"], parts["equity"]),
                )
                self._conn.executemany(
                    "INSERT INTO lots (strategy, condition_id, token_id, shares, cost_usd,"
                    " par_value, last_mark, tag, mark_live)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    [(name, lot.condition_id, lot.token_id, lot.shares, lot.cost_usd,
                      lot.par_value, lot.last_mark, lot.tag, int(lot.mark_live))
                     for lot in self._books[name].lots if lot.shares > 1e-9],
                )
            self._conn.commit()
            # The journal window is keyed by SQLite rowid, not timestamps: a
            # trade committed in the same clock tick as this snapshot must land
            # on exactly one side of the boundary.
            self._prev_snapshot = {
                "ts": now,
                "trades_rowid": trades_rowid,
                "books": {
                    name: (parts["cash"], parts["positions"], parts["equity"])
                    for name, parts in report.items()
                },
            }
        return equity

    def _reconcile_locked(self, now: float) -> dict[str, dict[str, float]]:
        previous = self._prev_snapshot
        report: dict[str, dict[str, float]] = {}
        for name, book in self._books.items():
            cash = float(book.cash)
            positions = float(book.positions_value())
            # Recompute the position sum from the lots themselves instead of
            # trusting the method under test: the two must agree.
            expected = sum(
                lot.shares * lot.mark() for lot in book.lots if lot.shares > 1e-9
            )
            value = cash + positions
            report[name] = {
                "cash": cash,
                "positions": positions,
                "expected_positions": expected,
                "equity": value,
            }
            self._check_book(name, cash, positions, expected, value)
            prior = previous["books"].get(name) if previous is not None else None
            if prior is not None:
                self._check_window(
                    name, cash, positions, value, prior,
                    previous["trades_rowid"], previous["ts"], now,
                )
        return report

    @staticmethod
    def _tolerance(*values: float) -> float:
        return 1e-6 + 1e-9 * max((abs(value) for value in values), default=0.0)

    def _check_book(self, name: str, cash: float, positions: float,
                    expected: float, equity: float) -> None:
        """Check ``equity == cash + sum(shares * mark)`` for one book."""
        components = (equity, cash, expected)
        if not all(math.isfinite(value) for value in components + (positions,)):
            log.error(
                f"accounting defect [{name}]: non-finite component "
                f"equity={equity!r} cash={cash!r} positions={positions!r} "
                f"lots={expected!r}"
            )
            return
        tol = self._tolerance(*components)
        if abs(equity - (cash + expected)) > tol:
            log.error(
                f"accounting defect [{name}]: equity=${equity:,.6f} != "
                f"cash=${cash:,.6f} + positions=${expected:,.6f} "
                f"(book reports positions=${positions:,.6f}, "
                f"residual=${equity - (cash + expected):+,.6f}, tol=${tol:.2e})"
            )

    def _check_window(self, name: str, cash: float, positions: float,
                      equity: float, prior: tuple[float, float, float],
                      trades_rowid: int, prev_ts: float, now: float) -> None:
        """Check one snapshot window: ``d_equity == d_cash + d_positions``.

        ``accrual_credited`` is reported separately but is *part of* ``d_cash``
        (every ``credit`` writes a trade row), so it is not added again; the
        independent check that makes this non-trivial is that ``d_cash`` must
        equal the journal's own ``SUM(cash_delta)`` for the window.
        """
        prev_cash, prev_positions, prev_equity = prior
        journaled, accrual = self._conn.execute(
            "SELECT COALESCE(SUM(cash_delta), 0.0),"
            " COALESCE(SUM(CASE WHEN kind = 'credit' AND detail = 'reward_accrual'"
            " THEN cash_delta ELSE 0.0 END), 0.0)"
            " FROM trades WHERE strategy = ? AND rowid > ?",
            (name, trades_rowid),
        ).fetchone()
        journaled = float(journaled)
        accrual = float(accrual)
        d_cash = cash - prev_cash
        d_positions = positions - prev_positions
        d_equity = equity - prev_equity
        tol = self._tolerance(d_equity, d_cash, d_positions, journaled, accrual)
        residual = d_equity - (d_cash + d_positions)
        cash_residual = d_cash - journaled
        if abs(residual) > tol or abs(cash_residual) > tol:
            log.error(
                f"accounting defect [{name}] window {now - prev_ts:.0f}s: "
                f"d_equity=${d_equity:+,.6f} != d_cash=${d_cash:+,.6f} + "
                f"d_positions=${d_positions:+,.6f} (residual=${residual:+,.6f}); "
                f"cash moved ${d_cash:+,.6f} but journal says ${journaled:+,.6f} "
                f"(residual=${cash_residual:+,.6f}); accrual credited in window="
                f"${accrual:+,.6f} (already inside d_cash); tol=${tol:.2e}"
            )

    # -- internals -------------------------------------------------------
    @staticmethod
    def _add_lot(
        book: StrategyBook,
        strategy: str,
        condition_id: str,
        token_id: str,
        shares: float,
        cost_usd: float,
        par_value: float | None,
        mark: float,
        tag: str,
    ) -> None:
        for lot in book.lots:
            if lot.token_id == token_id and lot.par_value == par_value and lot.tag == tag:
                lot.shares += shares
                lot.cost_usd += cost_usd
                # A live token-level mark already applies to the new shares; do
                # not reset it to this fill's price. An unmeasured lot keeps the
                # new fill as its cost basis fallback.
                if not lot.mark_live:
                    lot.last_mark = mark
                return
        book.lots.append(
            Lot(strategy, condition_id, token_id, shares, cost_usd, par_value, mark, tag)
        )

    @staticmethod
    def _remove_shares(book: StrategyBook, token_id: str, shares: float) -> float | None:
        # Pre-check before touching anything: this used to remove whatever it
        # could and *then* return None when the request was unsatisfiable,
        # which burned inventory with no journal row and no cash movement.
        remaining = float(shares)
        available = sum(
            lot.shares for lot in book.lots if lot.token_id == token_id and lot.shares > 1e-12
        )
        if available + 1e-9 < remaining:
            return None
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
