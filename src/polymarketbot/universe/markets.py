"""Point-in-time market universe.

The central rule: a market may only appear in `universe_at(t)` if information
available *at t* says it existed and was tradable. Concretely:

* `created_at <= t`
* at least one non-zero fill with `timestamp <= t`  (the zero-volume filter,
  computed from the tape rather than the leaking `volume` snapshot column)
* `end_date > t` (or null)

What this deliberately does **not** use:

* `volume` from `markets.parquet` — a latest-snapshot value, leaks the future
* `closed` — likewise latest-only
* `is_void` / `payout_*` — resolution is only known at the end; excluding
  voids from a *simulation* universe would be lookahead

`tradable_at()` adds the ex-post void exclusion for analysis populations, and
is documented as such. Never use it to drive a simulation.

Known leak, documented rather than hidden: `end_date` can be extended after
creation, and SII only ships the latest value. A market whose deadline was
extended would appear in `universe_at(t)` for longer than it truly was.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

PIT = Path("data/processed/markets_pit.parquet")


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def universe_sql(when: str, *, exclude_void: bool = False, min_fills: int = 1) -> str:
    """SQL selecting the universe at an ISO timestamp."""
    ts = _lit(when)
    sql = f"""
        SELECT *
        FROM read_parquet('{PIT.as_posix()}')
        WHERE created_at <= TIMESTAMPTZ {ts}
          AND first_fill_ts IS NOT NULL
          AND to_timestamp(first_fill_ts) <= TIMESTAMPTZ {ts}
          AND fills >= {min_fills}
          AND (end_date IS NULL OR end_date > TIMESTAMPTZ {ts})
    """
    if exclude_void:
        sql += " AND NOT coalesce(is_void, false)"
    return sql


def universe_at(
    con: duckdb.DuckDBPyConnection,
    when: str,
    *,
    exclude_void: bool = False,
    min_fills: int = 1,
) -> duckdb.DuckDBPyRelation:
    """Point-in-time universe. Safe to drive a simulation."""
    return con.sql(universe_sql(when, exclude_void=exclude_void, min_fills=min_fills))


def tradable_at(
    con: duckdb.DuckDBPyConnection,
    when: str,
    *,
    min_fills: int = 1,
) -> duckdb.DuckDBPyRelation:
    """Ex-post tradable population: excludes markets that ultimately voided.

    This is for analysis (hit rates, calibration) where a void has no win/loss
    interpretation. It is **not** point-in-time and must not drive a simulation.
    """
    return universe_at(con, when, exclude_void=True, min_fills=min_fills)


def settle(con: duckdb.DuckDBPyConnection) -> duckdb.DuckDBPyRelation:
    """Resolution labels in payout form, with voids marked.

    `payout_0` is the per-share payout of `token1`, proven to be on-chain
    outcome slot 0 (see scripts/build_labels.py). A void pays 0.5 per share for
    a standard binary market; it is neither a win nor a loss.
    """
    return con.sql(f"""
        SELECT
            condition_id,
            token1,
            token2,
            resolved,
            payout_0,
            payout_1,
            is_void,
            label_source,
            CASE
                WHEN NOT coalesce(resolved, false) THEN 'unresolved'
                WHEN coalesce(is_void, false)      THEN 'void'
                WHEN payout_0 >= 0.999             THEN 'token1'
                WHEN payout_1 >= 0.999             THEN 'token2'
                ELSE 'fractional'
            END AS outcome
        FROM read_parquet('{PIT.as_posix()}')
    """)
