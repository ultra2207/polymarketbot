"""The reconciler must never print an unknown as a zero.

"The venue paid nothing" and "we could not ask the venue" are different facts,
and so are "the ledger recorded no accrual that day" and "the ledger was not
recording". Only the first of each pair is evidence about the accrual model;
printing ``$0.000000`` for the second turns an absent measurement into a
refutation, which is the mistake this file exists to prevent.
"""

from __future__ import annotations

import sqlite3


from polymarketbot.strategy.reward.reconciler import format_report, reconcile_day

_SCHEMA = """
CREATE TABLE trades (ts REAL, condition_id TEXT, kind TEXT, detail TEXT, cash_delta REAL);
CREATE TABLE reward_accrual (
    ts REAL NOT NULL, day TEXT NOT NULL, condition_id TEXT NOT NULL,
    accrued_usd REAL NOT NULL, priced_markets INTEGER NOT NULL
);
"""


class _UnmeasuredClient:
    """A client that cannot read the venue, as in dry-run with no credentials."""

    dry_run = True
    maker_address = "0xmaker"

    def get_rewards_for_date(self, day):
        return {"source": "dry_run", "error": "no L2 credentials"}

    def get_current_rebates(self, day):
        return {"source": "dry_run", "error": "no L2 credentials"}

    def get_daily_rewards(self, day):
        return {"source": "dry_run", "error": "no L2 credentials"}


def _journal(tmp_path, *, accrual_rows=(), trade_rows=()):
    path = tmp_path / "ledger.db"
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    for day, cid, usd in accrual_rows:
        conn.execute(
            "INSERT INTO reward_accrual VALUES (?, ?, ?, ?, ?)", (1.0, day, cid, usd, 1)
        )
    for ts, cid, kind, detail, delta in trade_rows:
        conn.execute("INSERT INTO trades VALUES (?, ?, ?, ?, ?)", (ts, cid, kind, detail, delta))
    conn.commit()
    conn.close()
    return path


def _report(path, day="2026-11-10"):
    return format_report(reconcile_day(day, client=_UnmeasuredClient(), db_path=path))


def test_journal_with_no_accrual_rows_is_unmeasured_not_zero(tmp_path):
    report = _report(_journal(tmp_path))
    assert "ledger: unmeasured (this journal has no reward_accrual rows at all" in report
    assert "ledger total: $0.000000 over 0 condition(s)" not in report


def test_missing_journal_is_unmeasured_not_zero(tmp_path):
    report = _report(tmp_path / "does_not_exist.db")
    assert "ledger: unmeasured (ledger journal" in report
    assert "ledger total: $0.000000" not in report


def test_recorded_accrual_is_reported_as_a_measurement(tmp_path):
    path = _journal(tmp_path, accrual_rows=[("2026-11-10", "0xabc", 12.5)])
    report = _report(path)
    assert "ledger total: $12.500000 over 1 condition(s)" in report


def test_journal_with_no_trades_cannot_claim_zero_rebates(tmp_path):
    path = _journal(tmp_path, accrual_rows=[("2026-11-10", "0xabc", 1.0)])
    report = _report(path)
    assert "ledger: unmeasured (this journal recorded no trades at all" in report


def test_a_live_journal_without_rebates_truthfully_reports_zero(tmp_path):
    """One recorded trade and no rebate credit is a measurement, not an absence."""
    path = _journal(
        tmp_path,
        accrual_rows=[("2026-11-10", "0xabc", 1.0)],
        trade_rows=[(1.0, "0xabc", "credit", "settlement_payout", 5.0)],
    )
    report = _report(path)
    assert "ledger total: $0.000000" in report


def test_venue_side_unreadable_never_becomes_zero(tmp_path):
    path = _journal(tmp_path, accrual_rows=[("2026-11-10", "0xabc", 1.0)])
    report = _report(path)
    assert "venue: unmeasured" in report
    assert "ratio venue/ledger: unmeasured" in report
