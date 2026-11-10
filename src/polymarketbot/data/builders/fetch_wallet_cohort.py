"""Build a wallet cohort from the Data API's ranked boards.

This is the cheap path to Phase 6b. Instead of exploding the 60 GB
`users.parquet` to find skilled wallets, start from `/v2/leaderboard` and
`/v2/biggest-winners`, then read one `/v2/user-stats` per wallet.

`user-stats.all_time_pnl` decomposes PnL by source:

    realized_market_pnl  directional trading
    realized_lp_pnl      liquidity rewards
    realized_combo_pnl   combos
    maker_rebate         resting-order rebates
    taker_rebate         taker rebates
    fees_refunded        refunded taker fees

That decomposition is a first-cut archetype signal. It is **current-state and
survivorship-biased**: the boards rank wallets by today's PnL, which is not a
point-in-time feature and must never be used as one. Use it to choose who to
study, not to score skill in a backtest.

Usage
    uv run python -m polymarketbot.data.builders.fetch_wallet_cohort --max-wallets 200
    uv run python -m polymarketbot.data.builders.fetch_wallet_cohort --max-wallets 500 --max-seconds 600
"""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import duckdb

from polymarketbot.dataapi import DataAPIClient
from polymarketbot.wallets import classify_archetype as classify
from polymarketbot.logs import get_logger
log = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[4]

OUT = REPO_ROOT / "data" / "processed"
DEST = OUT / "wallet_cohort.parquet"

PNL_FIELDS = [
    "realized_market_pnl", "realized_lp_pnl", "realized_combo_pnl",
    "unrealized_pnl", "fees_refunded", "maker_rebate", "taker_rebate",
    "reward_income", "yield_income", "referral_income", "wallet_income",
    "position_pnl", "realized_pnl",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-wallets", type=int, default=200)
    ap.add_argument("--leaderboard-pages", type=int, default=10,
                    help="100 wallets per page; the board paginates to 3,000+")
    ap.add_argument("--winners-pages", type=int, default=5,
                    help="biggest_winners stops yielding after ~500 rows")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--max-seconds", type=float, default=600.0)
    args = ap.parse_args()

    client = DataAPIClient(min_interval_s=0.05)
    started = time.time()

    log.info("fetching boards ...", )
    board: dict[str, dict] = {}

    for row in client.leaderboard(limit=100, max_pages=args.leaderboard_pages):
        w = row.get("user_id")
        if not w:
            continue
        board.setdefault(w, {"wallet": w, "sources": set()})
        board[w].update({
            "leaderboard_rank": row.get("rank"),
            "leaderboard_pnl": row.get("pnl"),
            "leaderboard_volume": row.get("volume"),
            "user_name": row.get("user_name"),
        })
        board[w]["sources"].add("leaderboard")

    for row in client.biggest_winners(limit=100, max_pages=args.winners_pages):
        w = row.get("user_id")
        if not w:
            continue
        board.setdefault(w, {"wallet": w, "sources": set()})
        board[w].update({
            "best_win_pnl": max(board[w].get("best_win_pnl") or 0.0, row.get("pnl") or 0.0),
            "best_win_condition": row.get("condition_id"),
            "best_win_resolved_at": row.get("resolved_at"),
        })
        board[w]["sources"].add("biggest_winners")

    wallets = list(board.values())[: args.max_wallets]
    log.info(f"board wallets: {len(board):,}; fetching stats for {len(wallets):,}", )

    def enrich(w: dict) -> dict:
        c = DataAPIClient(min_interval_s=0.05)
        try:
            stats = c.user_stats(w["wallet"])
        except Exception:  # noqa: BLE001
            stats = {}
        pnl = (stats.get("all_time_pnl") or {}) if isinstance(stats, dict) else {}
        out = dict(w)
        out["sources"] = ",".join(sorted(w["sources"]))
        out["trades"] = stats.get("trades") if isinstance(stats, dict) else None
        out["join_date"] = stats.get("join_date") if isinstance(stats, dict) else None
        for f in PNL_FIELDS:
            out[f] = pnl.get(f)
        out["archetype"] = classify(out)
        return out

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(enrich, w): w for w in wallets}
        for fut in as_completed(futures):
            if time.time() - started > args.max_seconds:
                for f in futures:
                    f.cancel()
                log.info("  wall-clock budget reached", )
                break
            rows.append(fut.result())
            if len(rows) % 25 == 0:
                log.info(f"  {len(rows)}/{len(wallets)} wallets, {time.time()-started:.0f}s", )

    if not rows:
        log.info("no rows")
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    jsonl = OUT / "_wallet_cohort.jsonl"
    with jsonl.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, default=str) + "\n")

    con = duckdb.connect()
    con.execute(f"""
        COPY (SELECT * FROM read_json_auto('{jsonl.as_posix()}', format='newline_delimited'))
        TO '{DEST.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    jsonl.unlink(missing_ok=True)

    log.info(f"\nwrote {DEST} ({len(rows):,} wallets)", )
    log.info("\narchetypes:")
    for r in con.sql(f"""
        SELECT archetype, count(*) n, round(sum(realized_pnl),0) pnl
        FROM read_parquet('{DEST.as_posix()}') GROUP BY 1 ORDER BY n DESC
    """).fetchall():
        log.info(f"  {r[0]:20} {r[1]:>4}  ${r[2]:>12,.0f}")

    log.info("\ntop by realized PnL:")
    for r in con.sql(f"""
        SELECT wallet, archetype, leaderboard_rank, round(realized_pnl,0),
               round(realized_market_pnl,0), round(maker_rebate,0), round(realized_lp_pnl,0),
               trades
        FROM read_parquet('{DEST.as_posix()}')
        ORDER BY realized_pnl DESC NULLS LAST LIMIT 10
    """).fetchall():
        log.info(f"  {r[0][:12]} {r[1]:18} rank={str(r[2]):>4} pnl=${r[3]:>10,.0f} "
              f"market=${r[4]:>10,.0f} rebate=${r[5]:>7,.0f} lp=${r[6]:>7,.0f} trades={r[7]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
