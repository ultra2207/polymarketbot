"""Assemble the point-in-time market table.

Joins three derived sources:

* `market_labels.parquet`     - SII metadata + authoritative payout labels
* `market_categories.parquet` - slug-derived category (rule-based)
* `market_trade_stats.parquet`- point-in-time activity from the tape

and adds the flags Phase 2 needs: zero-volume, void, metadata anomalies, and
the V1/V2 protocol era.

Usage
    uv run python -m polymarketbot.data.builders.build_universe
"""

from __future__ import annotations

from pathlib import Path

from polymarketbot.db import connect
from polymarketbot.logs import get_logger
log = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[4]

OUT = REPO_ROOT / "data" / "processed"
LABELS = OUT / "market_labels.parquet"
CATS = OUT / "market_categories.parquet"
STATS = OUT / "market_trade_stats.parquet"
TICKS = OUT / "market_tick.parquet"
GAMMA = OUT / "market_metadata_gamma.parquet"
DEST = OUT / "markets_pit.parquet"


def main() -> int:
    con = connect(threads=12, memory_limit="8GB")

    # Gamma enrichment is optional so the pipeline still builds from the core
    # set alone. It adds position system, per-market fees and true tick sizes.
    if GAMMA.exists():
        gamma_join = f"""
            LEFT JOIN read_parquet('{GAMMA.as_posix()}') g
                   ON g.condition_id = l.condition_id
        """
        gamma_cols = """
                    g.version AS position_version,
                    g.fee_type,
                    g.fee_rate,
                    g.fee_exponent,
                    g.fee_rebate,
                    g.fees_enabled,
                    g.min_order_size,
                    g.tick_size AS gamma_tick_size,
                    g.uma_resolution_statuses,
                    g.tag_labels,
        """
    else:
        gamma_join = ""
        gamma_cols = """
                    NULL::VARCHAR AS position_version,
                    NULL::VARCHAR AS fee_type,
                    NULL::DOUBLE  AS fee_rate,
                    NULL::DOUBLE  AS fee_exponent,
                    NULL::DOUBLE  AS fee_rebate,
                    NULL::BOOLEAN AS fees_enabled,
                    NULL::DOUBLE  AS min_order_size,
                    NULL::DOUBLE  AS gamma_tick_size,
                    NULL::VARCHAR[] AS uma_resolution_statuses,
                    NULL::VARCHAR[] AS tag_labels,
        """

    con.execute(f"""
        COPY (
            WITH base AS (
                SELECT
                    l.condition_id,
                    l.market_id,
                    l.question,
                    l.slug,
                    l.event_id,
                    l.event_slug,
                    l.token1,
                    l.token2,
                    l.answer1,
                    l.answer2,
                    l.created_at,
                    l.end_date,
                    l.closed,
                    l.active,
                    l.archived,
                    l.neg_risk,
                    l.volume,
                    l.outcome_prices,
                    l.resolved,
                    l.payout_0,
                    l.payout_1,
                    l.is_void,
                    l.label_source,
                    c.category,
                    s.first_fill_ts,
                    s.last_fill_ts,
                    s.fills,
                    s.traded_usd,
                    s.traded_shares,
                    s.v1_fills,
                    s.v2_fills,
                    d.tick_size AS dome_tick_size,
                    -- coalesced here, not after the `*`: a second `AS n_tick_sizes`
                    -- in the outer projection silently produced `n_tick_sizes_1`.
                    coalesce(d.n_tick_sizes, 0) AS n_tick_sizes,
                    {gamma_cols}
                    row_number() OVER (PARTITION BY l.condition_id ORDER BY l.created_at) AS rn
                FROM read_parquet('{LABELS.as_posix()}') l
                LEFT JOIN read_parquet('{CATS.as_posix()}') c
                       ON c.condition_id = l.condition_id
                LEFT JOIN read_parquet('{STATS.as_posix()}') s
                       ON s.condition_id = l.condition_id
                LEFT JOIN read_parquet('{TICKS.as_posix()}') d
                       ON d.market_id = l.market_id
                {gamma_join}
                WHERE l.condition_id IS NOT NULL AND l.condition_id <> ''
            )
            SELECT
                * EXCLUDE (rn),
                -- zero-volume: no non-zero fill ever, not "volume = 0" in the snapshot
                coalesce(fills, 0) = 0                                AS is_zero_volume,
                -- 6,386 markets have fills before their recorded created_at
                -- (median 3.6 h, six cases are ~3 years off = metadata errors).
                -- We keep created_at as the birth time: it is conservative and
                -- cannot introduce lookahead. Flagged, not silently "fixed".
                (first_fill_ts IS NOT NULL
                 AND to_timestamp(first_fill_ts) < created_at - INTERVAL 1 HOUR) AS traded_before_created,
                -- metadata bug seen in 44.6% of 2022 markets
                (end_date IS NOT NULL AND end_date < created_at)      AS end_before_created,
                -- Two independent axes, previously conflated:
                --   exchange_generation - which exchange contract settled the fill
                --     (v1 pre-2026-04-28, v2 after; SII's *_V2 contracts)
                --   position_system     - where outcome balances live
                --     (CTF v1 vs PositionManager v2). Gamma `version` decides.
                CASE
                    WHEN coalesce(v2_fills,0) > 0 AND coalesce(v1_fills,0) > 0 THEN 'mixed'
                    WHEN coalesce(v2_fills,0) > 0 THEN 'v2'
                    WHEN coalesce(v1_fills,0) > 0 THEN 'v1'
                    ELSE 'unknown'
                END                                                    AS exchange_generation,
                CASE
                    WHEN position_version = 'v2' THEN 'position_manager'
                    WHEN position_version = 'v1' THEN 'ctf'
                    ELSE 'ctf_inferred'
                END                                                    AS position_system,
                -- tick size precedence: Gamma (authoritative, per market) ->
                -- Dome snapshots (2025-10..2026-03) -> explicit 0.01 assumption.
                -- 165/200 sampled markets are 0.001, so the old 0.01 default was
                -- often wrong; `tick_size_source` makes this auditable.
                coalesce(gamma_tick_size, dome_tick_size, 0.01)        AS tick_size,
                CASE
                    WHEN gamma_tick_size IS NOT NULL THEN 'gamma'
                    WHEN dome_tick_size  IS NOT NULL THEN 'dome'
                    ELSE 'assumed'
                END                                                    AS tick_size_source,
                CASE
                    WHEN NOT coalesce(resolved,false) THEN 'unresolved'
                    WHEN coalesce(is_void,false)      THEN 'void'
                    WHEN payout_0 >= 0.999            THEN 'token1'
                    WHEN payout_1 >= 0.999            THEN 'token2'
                    ELSE 'fractional'
                END                                                    AS outcome
            FROM base
            WHERE rn = 1
        ) TO '{DEST.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    n = con.sql(f"SELECT count(*) FROM read_parquet('{DEST.as_posix()}')").fetchall()[0][0]
    log.info(f"wrote {DEST}  ({n:,} markets)\n")

    log.info("composition:")
    row = con.sql(f"""
        SELECT
            count(*) n,
            count(*) FILTER (WHERE is_zero_volume) zero_vol,
            count(*) FILTER (WHERE is_void) voids,
            count(*) FILTER (WHERE resolved) resolved,
            count(*) FILTER (WHERE end_before_created) bad_dates,
            count(*) FILTER (WHERE label_source = 'missing') no_label
        FROM read_parquet('{DEST.as_posix()}')
    """).fetchall()[0]
    log.info(f"  markets          {row[0]:>12,}")
    log.info(f"  zero volume      {row[1]:>12,}  ({100*row[1]/row[0]:.1f}%)")
    log.info(f"  void             {row[2]:>12,}  ({100*row[2]/row[0]:.1f}%)")
    log.info(f"  resolved         {row[3]:>12,}")
    log.info(f"  end<created      {row[4]:>12,}  ({100*row[4]/row[0]:.1f}%)")
    log.info(f"  no label         {row[5]:>12,}")
    pre = con.sql(
        f"SELECT count(*) FROM read_parquet('{DEST.as_posix()}') WHERE traded_before_created"
    ).fetchall()[0][0]
    log.info(f"  traded pre-create {pre:>11,}")

    log.info("\ntick size:")
    for r in con.sql(f"""
        SELECT tick_size_source, tick_size, count(*) n
        FROM read_parquet('{DEST.as_posix()}')
        GROUP BY 1,2 ORDER BY n DESC LIMIT 6
    """).fetchall():
        log.info(f"  {r[0]:8} tick={r[1]:<6} {r[2]:>12,}")

    log.info("\nexchange generation (which contract settled the fill):")
    for r in con.sql(f"""
        SELECT exchange_generation, count(*) n, round(coalesce(sum(traded_usd), 0)/1e9, 2) usd_b
        FROM read_parquet('{DEST.as_posix()}') GROUP BY 1 ORDER BY n DESC
    """).fetchall():
        log.info(f"  {r[0]:8} {r[1]:>12,}  ${r[2]:>8.2f}B")

    log.info("\nposition system (where balances live):")
    for r in con.sql(f"""
        SELECT position_system, count(*) n,
               count(*) FILTER (WHERE position_version IS NOT NULL) verified
        FROM read_parquet('{DEST.as_posix()}') GROUP BY 1 ORDER BY n DESC
    """).fetchall():
        log.info(f"  {r[0]:16} {r[1]:>12,}  gamma-verified {r[2]:>10,}")

    log.info("\nfee metadata coverage (Gamma):")
    fee = con.sql(f"""
        SELECT count(*) n,
               count(*) FILTER (WHERE fee_rate IS NOT NULL) with_rate,
               count(*) FILTER (WHERE fee_rate > 0) charged,
               count(*) FILTER (WHERE fee_type IS NOT NULL) with_type
        FROM read_parquet('{DEST.as_posix()}') WHERE NOT is_zero_volume
    """).fetchall()[0]
    log.info(f"  traded markets {fee[0]:>12,}  fee_schedule {fee[1]:>12,}  "
          f"non-zero {fee[2]:>12,}  fee_type {fee[3]:>12,}")
    log.info("\ncategory (tradable, non-void):")
    for r in con.sql(f"""
        SELECT category, count(*) n, round(coalesce(sum(traded_usd),0)/1e9,2) usd_b
        FROM read_parquet('{DEST.as_posix()}')
        WHERE NOT is_zero_volume AND NOT coalesce(is_void,false)
        GROUP BY 1 ORDER BY n DESC
    """).fetchall():
        log.info(f"  {r[0]:12} {r[1]:>10,}  ${r[2]:>8.2f}B")

    log.info("\nopen universe at 12 sample dates:")
    log.info("  (volume shown is the market's *lifetime* total, for scale only —")
    log.info("   it is NOT a point-in-time figure and must not be used as a feature)")
    con.execute(f"CREATE VIEW pit AS SELECT * FROM read_parquet('{DEST.as_posix()}')")
    dates = [
        "2024-01-01", "2024-04-01", "2024-07-01", "2024-10-01",
        "2025-01-01", "2025-04-01", "2025-07-01", "2025-10-01",
        "2026-01-01", "2026-04-01", "2026-06-01", "2026-08-10",
    ]
    for d in dates:
        r = con.sql(f"""
            SELECT count(*) n, round(coalesce(sum(traded_usd),0)/1e6,1) usd_m
            FROM pit
            WHERE created_at <= TIMESTAMPTZ '{d}'
              AND first_fill_ts IS NOT NULL
              AND to_timestamp(first_fill_ts) <= TIMESTAMPTZ '{d}'
              AND (end_date IS NULL OR end_date > TIMESTAMPTZ '{d}')
        """).fetchall()[0]
        log.info(f"  {d}  {r[0]:>8,} markets   ${r[1]:>12,.1f}M lifetime")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
