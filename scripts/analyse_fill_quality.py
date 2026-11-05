"""Are the automated makers skilled, or merely paid?

The question this answers
-------------------------
E7 showed the volume top is broad, automated, small-clip making: 41 % of all volume, $13
median fill, 3-19 s between fills. That describes *how* they trade, not whether they earn
anything from it. The wallet cohort suggested 67.7 % of maker PnL is rebate, which would
mean they are being paid to stand there rather than winning the trade.

This measures the trade itself. For every maker fill, take the market's eventual payout as
the terminal mark and ask: did the maker buy below what the token turned out to be worth?

    maker bought at p   ->  edge = payout - p     (they wanted it; did it pay?)
    maker sold at p     ->  edge = p - payout     (they gave it away; were they right?)

A pure market maker should be roughly flat **before** adverse selection and negative
**after** it. A maker with a strongly positive terminal edge is not providing liquidity -
it is taking a directional view with a resting order. The two look identical in the fills.

Why the terminal mark and not the 5-minute one
----------------------------------------------
Terminal is cheap (one join to labels) and it is the mark that matters for a holding
strategy. The cost is that it cannot separate "picked off at 5 s" from "wrong for a week",
which a later short-horizon version will add. This version sets the direction.

Caveat that makes this a lower bound on skill
---------------------------------------------
A maker who fills, gets adverse-selected, and immediately unwinds at a loss shows the full
terminal loss here. A maker who fills and unwinds profitably shows only the net. So this
measure understates the edge of anyone who actively manages inventory, and overstates the
loss of anyone who does not.

Usage
    uv run scripts/analyse_fill_quality.py --start 2026-04-01 --end 2026-10-01
"""

from __future__ import annotations

import argparse
import sys
from calendar import monthrange
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "src")

from polymarketbot.logs import IST

import duckdb
from rich.console import Console
from rich.progress import (BarColumn, Progress, SpinnerColumn, TaskProgressColumn,
                           TextColumn, TimeElapsedColumn, TimeRemainingColumn)

OUT = Path("data/processed")
TAPE = "data/raw/sii/trades.parquet"


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET threads = 6")
    con.execute("SET memory_limit = '5GB'")
    con.execute("SET preserve_insertion_order = false")
    con.execute("SET temp_directory = '.tmp/duckdb_spill'")
    con.execute("SET TimeZone = 'UTC'")
    return con


def months_between(start: str, end: str) -> list[tuple[int, int]]:
    y, m = map(int, start.split("-")[:2])
    ey, em = map(int, end.split("-")[:2])
    out = []
    while (y, m) <= (ey, em):
        lo = int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp())
        hi = int(datetime(y, m, monthrange(y, m)[1], 23, 59, 59,
                          tzinfo=timezone.utc).timestamp()) + 1
        out.append((lo, hi))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-04-01")
    ap.add_argument("--end", default="2026-10-01")
    ap.add_argument("--min-fills", type=int, default=100)
    ap.add_argument("--dest", default=str(OUT))
    args = ap.parse_args()

    con = connect()
    console = Console()

    # token -> realised payout. Both legs of every resolved market.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE token_payout AS
        SELECT token1 AS asset_id, payout_0 AS payout
        FROM read_parquet('data/processed/market_labels.parquet')
        WHERE resolved AND NOT coalesce(is_void, FALSE)
          AND token1 IS NOT NULL AND payout_0 IS NOT NULL
        UNION ALL
        SELECT token2, payout_1
        FROM read_parquet('data/processed/market_labels.parquet')
        WHERE resolved AND NOT coalesce(is_void, FALSE)
          AND token2 IS NOT NULL AND payout_1 IS NOT NULL
    """)
    n_tok = con.sql("SELECT count(*) FROM token_payout").fetchall()[0][0]
    console.print(f"resolved tokens with a payout: {n_tok:,}")

    con.execute("""
        CREATE OR REPLACE TEMP TABLE agg (wallet VARCHAR, fills BIGINT,
            volume DOUBLE, edge_usd DOUBLE, price_sum DOUBLE, n_priced BIGINT)
    """)

    months = months_between(args.start, args.end)
    bar = Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        TextColumn("elapsed"),
        TimeRemainingColumn(),
        TextColumn("left"),
        console=console,
    )
    with bar:
        task = bar.add_task("fill quality", total=len(months))
        for i, (lo, hi) in enumerate(months, 1):
            label = datetime.fromtimestamp(lo, tz=IST).strftime("%Y-%m")
            bar.update(task, description=f"{label} ({i}/{len(months)})")
            con.execute(f"""
                INSERT INTO agg
                SELECT t.maker AS wallet,
                       count(*) AS fills,
                       sum(t.usd_amount) AS volume,
                       sum(CASE WHEN t.maker_direction = 'BUY'
                                THEN (p.payout - t.price) * (t.usd_amount / nullif(t.price, 0))
                                ELSE (t.price - p.payout) * (t.usd_amount / nullif(t.price, 0))
                           END) AS edge_usd,
                       sum(t.price) AS price_sum,
                       count(*) AS n_priced
                FROM read_parquet('{TAPE}') t
                JOIN token_payout p ON p.asset_id = t.asset_id
                WHERE t.timestamp >= {lo} AND t.timestamp < {hi}
                  AND t.maker IS NOT NULL AND t.usd_amount > 0
                  AND t.price > 0 AND t.price < 1
                  AND t.maker_direction IN ('BUY', 'SELL')
                GROUP BY 1
            """)
            bar.update(task, advance=1)
            console.print(f"  {label}: {con.sql('SELECT count(*) FROM agg').fetchall()[0][0]:,} wallets",
                          highlight=False)

    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE w AS
        SELECT wallet, sum(fills) AS fills, sum(volume) AS volume,
               sum(edge_usd) AS edge_usd,
               sum(price_sum) / nullif(sum(n_priced), 0) AS mean_price,
               sum(edge_usd) / nullif(sum(volume), 0) AS edge_per_dollar
        FROM agg GROUP BY 1 HAVING sum(fills) >= {args.min_fills}
    """)

    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    con.execute(f"""
        COPY (SELECT * FROM w ORDER BY volume DESC)
        TO '{(dest / 'maker_fill_quality.parquet').as_posix()}'
        (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    tot = con.sql("""
        SELECT count(*), sum(fills), sum(volume)/1e9, sum(edge_usd)/1e6 FROM w
    """).fetchall()[0]
    console.print(f"\nmakers with >= {args.min_fills} priced fills: {tot[0]:,}")
    console.print(f"fills: {tot[1]:,}   volume: ${tot[2]:.2f} B   terminal edge: ${tot[3]:,.0f} M")

    overall = con.sql("""
        SELECT sum(edge_usd) / nullif(sum(volume), 0) FROM w
    """).fetchall()[0][0]
    console.print(f"edge per dollar of maker volume: {overall:+.5f} "
                  f"({overall*100:+.3f} cents per $1)")

    console.print("\nby volume decile (does size help or hurt?):")
    console.print(f"  {'decile':8} {'makers':>7} {'volume $m':>10} {'edge $m':>9} "
                  f"{'edge/$':>9} {'mean px':>8}")
    for d, nm, v, e, epd, mp in con.sql("""
        WITH r AS (SELECT *, ntile(10) OVER (ORDER BY volume DESC) AS dec FROM w)
        SELECT dec, count(*), sum(volume)/1e6, sum(edge_usd)/1e6,
               sum(edge_usd)/nullif(sum(volume),0), avg(mean_price)
        FROM r GROUP BY 1 ORDER BY 1
    """).fetchall():
        console.print(f"  {d:<8} {nm:>7,} {v:>10,.0f} {e:>9,.1f} {epd:>+9.5f} {mp:>8.3f}",
                      highlight=False)

    pos = con.sql("""
        SELECT count(*) FILTER (WHERE edge_usd > 0), count(*), 
               sum(volume) FILTER (WHERE edge_usd > 0)/1e9
        FROM w
    """).fetchall()[0]
    console.print(f"\nmakers with positive terminal edge: {pos[0]:,} of {pos[1]:,} "
                  f"({100*pos[0]/max(pos[1],1):.1f} %), carrying ${pos[2]:.2f} B of volume")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
