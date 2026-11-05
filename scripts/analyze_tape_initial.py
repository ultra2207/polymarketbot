#!/usr/bin/env python3
"""Initial, bounded analysis of the production tape.

One pass over each table with aggregate queries only; no row materialisation.
Prints a compact report and writes the same text to
``data/derived/tape_initial_analysis.txt``.

    .venv/Scripts/python.exe scripts/analyze_tape_initial.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

import duckdb

from polymarketbot.logs import get_logger

log = get_logger("analyze_tape_initial")
TAPE = Path("data/tape")
OUT = Path("data/derived/tape_initial_analysis.txt")


def glob(table: str) -> str:
    return str((TAPE / table / "**" / "*.parquet").as_posix())


def q(con, sql: str, title: str, out: list[str]) -> None:
    t0 = time.perf_counter()
    rows = con.execute(sql).fetchall()
    cols = [d[0] for d in con.description]
    out.append(f"\n## {title}")
    out.append(" | ".join(cols))
    for row in rows:
        out.append(" | ".join("" if v is None else str(v) for v in row))
    log.info(f"{title}: {len(rows)} row(s) in {time.perf_counter() - t0:.1f}s")


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    out: list[str] = ["# Tape initial analysis", f"generated {time.strftime('%Y-%m-%d %H:%M:%S')}"]
    con = duckdb.connect()
    con.execute("SET threads = 4")
    con.execute("SET memory_limit = '6GB'")
    con.execute("SET temp_directory = 'data/tmp_duck'")

    tables = sorted(p.name for p in TAPE.iterdir() if p.is_dir() and p.name not in {"_wal", "runs"})
    out.append("\n## Table sizes")
    for name in tables:
        size = sum(f.stat().st_size for f in (TAPE / name).rglob("*.parquet"))
        if size:
            out.append(f"{name}: {size / 1e9:.3f} GB")

    # One row per table: count and window. Tables without recv_ts_ns are skipped.
    for name in tables:
        try:
            q(
                con,
                f"""SELECT count(*) rows,
                           min(to_timestamp(recv_ts_ns / 1e9)) first,
                           max(to_timestamp(recv_ts_ns / 1e9)) last
                    FROM read_parquet('{glob(name)}')""",
                f"{name}: rows and window",
                out,
            )
        except Exception as exc:  # noqa: BLE001 - a table without the column is fine
            out.append(f"\n## {name}: skipped ({type(exc).__name__})")

    q(
        con,
        f"""SELECT count(*) rows,
                   count(DISTINCT condition_id) conditions,
                   count(DISTINCT asset_id) assets,
                   count(DISTINCT event_hash) hashes,
                   count(*) - count(DISTINCT event_hash) duplicate_hash_rows,
                   count(*) FILTER (WHERE event_type = 'book') book_rows,
                   count(*) FILTER (WHERE event_type = 'price_change') price_change_rows,
                   count(*) FILTER (WHERE event_type = 'tick_size_change') tick_rows,
                   median(spread) median_spread,
                   quantile_cont(spread, 0.25) spread_p25,
                   quantile_cont(spread, 0.75) spread_p75
            FROM read_parquet('{glob("clob_events")}')""",
        "clob_events: coverage and duplicates",
        out,
    )
    q(
        con,
        f"""SELECT condition_id, any_value(asset_id) asset, count(*) n,
                   min(to_timestamp(recv_ts_ns / 1e9)) first,
                   max(to_timestamp(recv_ts_ns / 1e9)) last
            FROM read_parquet('{glob("clob_events")}')
            GROUP BY 1 ORDER BY n DESC LIMIT 10""",
        "clob_events: busiest conditions",
        out,
    )
    q(
        con,
        f"""SELECT count(*) rows, count(DISTINCT condition_id) conditions,
                   count(DISTINCT asset_id) assets,
                   sum(price * size) notional_usd,
                   median(price) median_price,
                   quantile_cont(price * size, 0.5) median_trade_usd,
                   max(price * size) max_trade_usd
            FROM read_parquet('{glob("clob_trades")}')""",
        "clob_trades: coverage",
        out,
    )
    q(
        con,
        f"""SELECT condition_id, count(*) n, sum(price * size) notional_usd
            FROM read_parquet('{glob("clob_trades")}')
            GROUP BY 1 ORDER BY n DESC LIMIT 10""",
        "clob_trades: busiest conditions",
        out,
    )
    for name, group in (("binance_bbo", "symbol"), ("binance_depth", "symbol"),
                        ("binance_funding", "symbol"), ("binance_oi", "symbol"),
                        ("kalshi_markets", "ticker"), ("kalshi_trades", "ticker")):
        try:
            q(
                con,
                f"""SELECT count(*) rows, count(DISTINCT {group}) {group}s,
                           min(to_timestamp(recv_ts_ns / 1e9)) first,
                           max(to_timestamp(recv_ts_ns / 1e9)) last
                    FROM read_parquet('{glob(name)}')""",
                f"{name}: coverage",
                out,
            )
        except Exception as exc:  # noqa: BLE001
            out.append(f"\n## {name}: skipped ({type(exc).__name__})")
    q(
        con,
        f"""SELECT count(*) rows, count(DISTINCT block_number) blocks,
                   count(DISTINCT tx_hash) txs,
                   min(block_number) first_block, max(block_number) last_block
            FROM read_parquet('{glob("polygon_logs")}')""",
        "polygon_logs: coverage",
        out,
    )
    q(
        con,
        f"""SELECT count(*) rows, count(DISTINCT source) sources,
                   count(DISTINCT title) titles,
                   max(published_ts_ms) / 1000 AS last_publish_epoch
            FROM read_parquet('{glob("news_items")}')""",
        "news_items: coverage",
        out,
    )
    q(
        con,
        f"""SELECT stream, count(*) rows, count(DISTINCT metric) metrics
            FROM read_parquet('{glob("health_metrics")}')
            GROUP BY 1 ORDER BY 2 DESC""",
        "health_metrics: streams",
        out,
    )
    q(
        con,
        f"""SELECT count(*) rows, count(DISTINCT wallet) wallets,
                   count(DISTINCT condition_id) conditions,
                   sum(usdc_size) usdc
            FROM read_parquet('{glob("whale_activity")}')""",
        "whale_activity: coverage",
        out,
    )

    # Derived scorecard: the market-level analysis already computed from this tape.
    try:
        q(
            con,
            """SELECT count(*) conditions,
                      count(*) FILTER (WHERE trades > 0) traded,
                      count(*) FILTER (WHERE fee_known AND trades > 0) fee_known_traded,
                      median(spread_median) spread_median,
                      median(half_spread_median) half_spread_median,
                      count(*) FILTER (WHERE gate1) gate1_pass,
                      count(*) FILTER (WHERE gate1 AND markout_matched >= 30) gate1_pass_n30,
                      median(markout_5m_median) markout_median
               FROM read_parquet('data/derived/market_scorecard.parquet')""",
            "market_scorecard: market-level summary",
            out,
        )
    except Exception as exc:  # noqa: BLE001
        out.append(f"\n## market_scorecard: skipped ({type(exc).__name__})")

    text = "\n".join(out)
    OUT.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
