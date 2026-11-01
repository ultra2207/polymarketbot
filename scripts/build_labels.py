"""Build the canonical market-label table from on-chain condition data.

Why this exists
---------------
`outcome_prices` in SII `markets.parquet` is a *final traded price*, not a
payout. It is close to the payout for most markets but differs materially for
some (e.g. price 0.5109 vs on-chain payout 0.50387), and it encodes "void" in
three different ways (`0.5/0.5`, `1/1`, `0/0`). Thresholding it is wrong.

The authoritative label is the ConditionalTokens `payoutNumerators` /
`payoutDenominator` pair, available from `moose-code/condition.parquet`.
Slot order is proven, not assumed: `positionIds[0] == token1` for 934,889 of
934,957 binary conditions (the remaining 68 are stale/renamed ids).

Outputs
-------
data/processed/labels_chain.parquet
    One row per condition with a normalised payout vector.
data/processed/market_labels.parquet
    SII markets LEFT JOIN the payouts, with a label_source column.

Usage
    uv run scripts/build_labels.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, "src")

import duckdb

from polymarketbot.db import connect as _connect

RAW = Path("data/raw")
OUT = Path("data/processed")
CHAIN = RAW / "chain/condition.parquet"
MARKETS = RAW / "sii/markets.parquet"


def connect() -> duckdb.DuckDBPyConnection:
    return _connect(threads=12, memory_limit="8GB")


def build_chain_labels(con: duckdb.DuckDBPyConnection) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    dest = OUT / "labels_chain.parquet"
    con.execute(f"""
        COPY (
            WITH base AS (
                SELECT
                    id AS condition_id,
                    len(payoutNumerators)                    AS n_outcomes,
                    TRY_CAST(payoutDenominator AS DOUBLE)    AS payout_denominator,
                    payoutNumerators                         AS payout_numerators,
                    positionIds                              AS position_ids
                FROM read_parquet('{CHAIN.as_posix()}')
            ),
            norm AS (
                SELECT
                    *,
                    payout_denominator > 0 AND n_outcomes > 0 AS resolved,
                    CASE WHEN payout_denominator > 0 THEN
                        list_transform(
                            payout_numerators,
                            lambda x: TRY_CAST(x AS DOUBLE) / payout_denominator
                        )
                    END AS payouts
                FROM base
            )
            SELECT
                condition_id,
                n_outcomes,
                resolved,
                payouts,
                CASE WHEN resolved AND n_outcomes = 2 THEN payouts[1] END AS payout_0,
                CASE WHEN resolved AND n_outcomes = 2 THEN payouts[2] END AS payout_1,
                CASE WHEN resolved AND n_outcomes = 2
                     THEN abs(payouts[1] - payouts[2]) < 1e-9 END       AS is_void,
                payout_numerators,
                payout_denominator,
                position_ids
            FROM norm
        ) TO '{dest.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    n = con.sql(f"SELECT count(*) FROM read_parquet('{dest.as_posix()}')").fetchall()[0][0]
    print(f"wrote {dest}  ({n:,} conditions)")


def build_market_labels(con: duckdb.DuckDBPyConnection) -> None:
    dest = OUT / "market_labels.parquet"
    con.execute(f"""
        COPY (
            SELECT
                m.condition_id,
                m.id                AS market_id,
                m.question,
                m.slug,
                m.event_id,
                m.event_slug,
                m.token1,
                m.token2,
                m.answer1,
                m.answer2,
                m.created_at,
                m.end_date,
                m.closed,
                m.active,
                m.archived,
                m.neg_risk,
                m.volume,
                m.outcome_prices,
                -- NOTE: price, not payout. Kept only for reconciliation.
                l.resolved,
                l.payout_0,
                l.payout_1,
                l.is_void,
                l.n_outcomes,
                l.payout_numerators,
                l.payout_denominator,
                CASE
                    WHEN l.condition_id IS NULL           THEN 'missing'
                    WHEN l.resolved                       THEN 'chain'
                    ELSE 'chain_unresolved'
                END AS label_source
            FROM read_parquet('{MARKETS.as_posix()}') m
            LEFT JOIN read_parquet('{(OUT / "labels_chain.parquet").as_posix()}') l
                   ON l.condition_id = m.condition_id
        ) TO '{dest.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    n = con.sql(f"SELECT count(*) FROM read_parquet('{dest.as_posix()}')").fetchall()[0][0]
    print(f"wrote {dest}  ({n:,} markets)")


def report(con: duckdb.DuckDBPyConnection) -> None:
    src = (OUT / "market_labels.parquet").as_posix()
    print("\nlabel availability by creation month:")
    print(f"  {'month':8} {'markets':>10} {'chain':>10} {'missing':>10} {'resolved':>10} {'void':>8}")
    for row in con.sql(f"""
        SELECT strftime(created_at, '%Y-%m') mo,
               count(*) n,
               count(*) FILTER (WHERE label_source = 'chain') chain,
               count(*) FILTER (WHERE label_source = 'missing') missing,
               count(*) FILTER (WHERE resolved) resolved,
               count(*) FILTER (WHERE is_void) voids
        FROM read_parquet('{src}')
        WHERE created_at >= TIMESTAMPTZ '2025-10-01'
        GROUP BY 1 ORDER BY 1
    """).fetchall():
        print(f"  {row[0]:8} {row[1]:>10,} {row[2]:>10,} {row[3]:>10,} {row[4]:>10,} {row[5]:>8,}")

    print("\nforward-test window (2026-06-01 .. 2026-08-10), markets with volume > 0:")
    row = con.sql(f"""
        SELECT count(*) n,
               count(*) FILTER (WHERE resolved) resolved,
               count(*) FILTER (WHERE label_source = 'missing') missing
        FROM read_parquet('{src}')
        WHERE created_at >= TIMESTAMPTZ '2026-06-01'
          AND created_at <  TIMESTAMPTZ '2026-08-11'
          AND volume > 0
    """).fetchall()[0]
    print(f"  markets={row[0]:,}  resolved={row[1]:,}  missing_label={row[2]:,}")

    print("\ntradable universe proxy (closed, volume > 0) with/without label:")
    row = con.sql(f"""
        SELECT count(*) n,
               count(*) FILTER (WHERE resolved) resolved,
               count(*) FILTER (WHERE is_void) voids
        FROM read_parquet('{src}')
        WHERE closed = 1 AND volume > 0
    """).fetchall()[0]
    print(f"  n={row[0]:,}  resolved={row[1]:,}  voids={row[2]:,}")


def main() -> int:
    con = connect()
    build_chain_labels(con)
    build_market_labels(con)
    report(con)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
