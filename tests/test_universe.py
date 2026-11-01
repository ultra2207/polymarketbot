"""Gate 2 invariants for the point-in-time universe.

These are the cheap, non-negotiable checks. They are not a substitute for the
20 market spot-check against polymarket.com (scripts/check_universe.py), but
they catch the failure mode that actually matters: lookahead.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from polymarketbot.universe.markets import universe_sql

PIT = Path("data/processed/markets_pit.parquet")

pytestmark = pytest.mark.skipif(
    not PIT.exists(), reason="markets_pit.parquet not built yet"
)


@pytest.fixture(scope="module")
def con():
    c = duckdb.connect()
    c.execute("SET threads = 4")
    c.execute(f"CREATE VIEW pit AS SELECT * FROM read_parquet('{PIT.as_posix()}')")
    return c


DATES = [
    "2024-01-01", "2024-07-01", "2025-01-01", "2025-07-01",
    "2025-10-14", "2026-01-01", "2026-04-13", "2026-06-01", "2026-08-10",
]


@pytest.mark.parametrize("when", DATES)
def test_no_market_before_creation(con, when):
    n = con.sql(f"""
        SELECT count(*) FROM ({universe_sql(when)})
        WHERE created_at > TIMESTAMPTZ '{when}'
    """).fetchall()[0][0]
    assert n == 0, f"{n} markets in universe_at({when}) were created after it"


@pytest.mark.parametrize("when", DATES)
def test_no_market_before_first_fill(con, when):
    n = con.sql(f"""
        SELECT count(*) FROM ({universe_sql(when)})
        WHERE first_fill_ts IS NULL OR to_timestamp(first_fill_ts) > TIMESTAMPTZ '{when}'
    """).fetchall()[0][0]
    assert n == 0, f"{n} markets in universe_at({when}) had not traded yet"


@pytest.mark.parametrize("when", DATES)
def test_zero_volume_excluded(con, when):
    n = con.sql(f"""
        SELECT count(*) FROM ({universe_sql(when)}) WHERE is_zero_volume
    """).fetchall()[0][0]
    assert n == 0, f"{n} zero-volume markets leaked into universe_at({when})"


@pytest.mark.parametrize("when", DATES)
def test_expired_markets_excluded(con, when):
    n = con.sql(f"""
        SELECT count(*) FROM ({universe_sql(when)})
        WHERE end_date IS NOT NULL AND end_date <= TIMESTAMPTZ '{when}'
    """).fetchall()[0][0]
    assert n == 0, f"{n} expired markets leaked into universe_at({when})"


def test_tradable_excludes_voids(con):
    when = "2026-03-01"
    n = con.sql(f"""
        SELECT count(*) FROM ({universe_sql(when, exclude_void=True)})
        WHERE is_void
    """).fetchall()[0][0]
    assert n == 0


def test_resolved_payouts_sum_to_one(con):
    bad = con.sql("""
        SELECT count(*) FROM pit
        WHERE resolved AND payout_0 IS NOT NULL AND payout_1 IS NOT NULL
          AND abs(payout_0 + payout_1 - 1.0) > 1e-6
    """).fetchall()[0][0]
    assert bad == 0, f"{bad} resolved binary markets have payouts that do not sum to 1"


def test_voids_pay_equal(con):
    bad = con.sql("""
        SELECT count(*) FROM pit
        WHERE is_void AND abs(payout_0 - payout_1) > 1e-9
    """).fetchall()[0][0]
    assert bad == 0


def test_created_at_is_never_null(con):
    n = con.sql("SELECT count(*) FROM pit WHERE created_at IS NULL").fetchall()[0][0]
    assert n == 0


def test_outcome_labels_are_exhaustive(con):
    vals = {r[0] for r in con.sql(
        "SELECT DISTINCT outcome FROM pit"
    ).fetchall()}
    assert vals <= {"token1", "token2", "void", "fractional", "unresolved"}
