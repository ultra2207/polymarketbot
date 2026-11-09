#!/usr/bin/env python3
"""CLI Knob Optimizer for Domain 2 Settlement Basis & Pre-Resolution Harvester.

Optimizes risk-adjusted annual returns across controllable knobs:
- Entry Timing: max_hours_to_expiry, min_hours_to_expiry
- Diversification: max_single_bet_pct, max_category_pct, max_active_markets, cash_reserve_pct
- Pricing: min_price, max_price, min_net_edge_bps
- Risk Ceiling: max_drawdown_target_pct, variance_penalty_lambda

Usage:
    # Run with default knobs on historical tape
    uv run scripts/optimize_domain2.py --bankroll 10000

    # Auto-tune knobs to maximize Calmar Ratio (ARR / Drawdown)
    uv run scripts/optimize_domain2.py --bankroll 10000 --tune --metric calmar

    # Auto-tune knobs to maximize Sharpe Ratio subject to 10% max DD
    uv run scripts/optimize_domain2.py --bankroll 10000 --tune --metric sharpe --max-dd 0.10
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
import duckdb
import pandas as pd

from polymarketbot.logs import get_logger
from polymarketbot.net import install_doh_fallback
from polymarketbot.trading.domain2.knobs import (
    Domain2Knobs,
    Domain2KnobOptimizer,
    ObjectiveMetric,
)

logger = get_logger("optimize_domain2")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Domain 2 Risk-Adjusted Annual Return Knob Optimizer"
    )
    # Portfolio Bankroll
    parser.add_argument("--bankroll", type=float, default=10000.0, help="Bankroll in USDC (default: 10,000)")
    
    # Mode
    parser.add_argument("--tune", action="store_true", help="Auto-tune knobs over grid search to maximize risk-adjusted return")
    parser.add_argument("--metric", type=str, default="calmar", choices=["calmar", "sharpe", "sortino", "risk_adjusted_arr", "arr"], help="Objective metric to maximize")
    parser.add_argument("--out-config", type=str, default="configs/domain2_optimal_knobs.json", help="Path to save optimal knobs")

    # Controllable Knobs
    parser.add_argument("--max-hours", type=float, default=2.0, help="Max entry hours before resolution (default: 2.0h)")
    parser.add_argument("--min-hours", type=float, default=0.05, help="Min entry hours before resolution (default: 0.05h)")
    parser.add_argument("--min-price", type=float, default=0.970, help="Min contract price (default: 0.970)")
    parser.add_argument("--max-price", type=float, default=0.992, help="Max contract price (default: 0.992)")
    parser.add_argument("--min-edge-bps", type=float, default=30.0, help="Min net edge hurdle in bps (default: 30 bps)")
    parser.add_argument("--max-single-bet", type=float, default=0.025, help="Max single bet allocation pct (default: 0.025 = 2.5 pct)")
    parser.add_argument("--max-category-cap", type=float, default=0.150, help="Max correlated category allocation pct (default: 0.15 = 15 pct)")
    parser.add_argument("--max-markets", type=int, default=15, help="Max active concurrent markets (default: 15)")
    parser.add_argument("--reserve-pct", type=float, default=0.25, help="Liquid cash reserve buffer pct (default: 0.25 = 25 pct)")
    parser.add_argument("--max-dd", type=float, default=0.10, help="Maximum tolerated drawdown ceiling (default: 0.10 = 10 pct)")
    parser.add_argument("--lambda-var", type=float, default=3.0, help="Variance penalty multiplier (default: 3.0)")

    return parser.parse_args()


def load_tape_trades(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Load and join resolved conditions, universe map, and CLOB trades."""
    logger.info("Decoding CTF on-chain condition resolutions...")
    res_df = con.execute("""
        SELECT DISTINCT topic1 as condition_id, min(recv_ts_ns) as res_ts_ns, arbitrary(data) as raw_data
        FROM read_parquet(['data/derived/uma/res_nov5.parquet', 'data/derived/uma/res_nov6.parquet'])
        GROUP BY 1
    """).df()

    p0_list, p1_list, is_void_list = [], [], []
    for raw in res_df['raw_data']:
        b = bytes.fromhex(raw[2:])
        chunks = [int.from_bytes(b[i:i+32], 'big') for i in range(0, len(b), 32)]
        if len(chunks) >= 5:
            p0, p1 = chunks[3], chunks[4]
            p0_list.append(p0)
            p1_list.append(p1)
            is_void_list.append(p0 > 0 and p1 > 0)
        else:
            p0_list.append(None)
            p1_list.append(None)
            is_void_list.append(None)

    res_df['payout0'] = p0_list
    res_df['payout1'] = p1_list
    res_df['is_void'] = is_void_list
    con.register("decoded_resolutions", res_df)

    q = """
    WITH u AS (
        SELECT condition_id, u_question, tokens[1] as tok0, tokens[2] as tok1
        FROM read_parquet('data/derived/uma/universe_map.parquet')
    ),
    trades_joined AS (
        SELECT 
            t.condition_id,
            t.asset_id,
            t.price,
            t.size,
            t.side,
            t.venue_ts_ms,
            r.res_ts_ns,
            (r.res_ts_ns / 1e6 - t.venue_ts_ms) / 1000.0 / 3600.0 as hours_before_res,
            t.size * t.price as vol_usdc,
            u.u_question,
            CASE 
                WHEN lower(u.u_question) LIKE '%bitcoin%' OR lower(u.u_question) LIKE '%btc%' OR lower(u.u_question) LIKE '%ethereum%' OR lower(u.u_question) LIKE '%crypto%' THEN 'crypto'
                WHEN lower(u.u_question) LIKE '%temperature%' OR lower(u.u_question) LIKE '%weather%' THEN 'weather'
                WHEN lower(u.u_question) LIKE '%win%' OR lower(u.u_question) LIKE '%game%' OR lower(u.u_question) LIKE '%cup%' THEN 'sports'
                WHEN lower(u.u_question) LIKE '%election%' OR lower(u.u_question) LIKE '%trump%' OR lower(u.u_question) LIKE '%harris%' OR lower(u.u_question) LIKE '%cabinet%' THEN 'politics'
                ELSE 'economics'
            END as category,
            CASE 
                WHEN t.asset_id = u.tok0 THEN 
                    CASE WHEN r.is_void THEN 0.5 WHEN r.payout0 > 0 THEN 1.0 ELSE 0.0 END
                WHEN t.asset_id = u.tok1 THEN 
                    CASE WHEN r.is_void THEN 0.5 WHEN r.payout1 > 0 THEN 1.0 ELSE 0.0 END
                ELSE NULL
            END as token_payout
        FROM read_parquet('data/tape/clob_trades/**/*.parquet') t
        JOIN decoded_resolutions r ON t.condition_id = r.condition_id
        JOIN u ON t.condition_id = u.condition_id
        WHERE (r.res_ts_ns / 1e6 - t.venue_ts_ms) >= 0 -- strictly pre-resolution
          AND t.price >= 0.85
    )
    SELECT * FROM trades_joined ORDER BY venue_ts_ms ASC;
    """
    df = con.execute(q).df()
    logger.info("Loaded {} candidate trades across {} conditions.", len(df), df['condition_id'].nunique())
    return df


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    install_doh_fallback()

    con = duckdb.connect()
    con.execute("SET threads=2;")

    trades_df = load_tape_trades(con)

    opt = Domain2KnobOptimizer(
        bankroll_usd=args.bankroll,
        objective_metric=args.metric,  # type: ignore
        max_drawdown_ceiling=args.max_dd,
    )

    if args.tune:
        logger.info("\n" + "=" * 90)
        logger.info(f"  DOMAIN 2 KNOB META-OPTIMIZER: TUNING FOR MAXIMUM {args.metric.upper()} RATIO")
        logger.info("=" * 90)
        logger.info(f"  Bankroll:             ${args.bankroll:,.2f} USDC")
        logger.info(f"  Target Metric:        {args.metric.upper()}")
        logger.info(f"  Max Drawdown Ceiling: {args.max_dd * 100:.1f}%\n")

        best_res, all_res = opt.grid_search_tune(
            trades_df=trades_df,
            hours_grid=[0.5, 1.0, 1.5, 2.0, 3.0, 6.0, 10.0],
            single_bet_grid=[0.01, 0.02, 0.025, 0.04, 0.05],
            category_cap_grid=[0.08, 0.12, 0.15, 0.25],
            min_price_grid=[0.93, 0.96, 0.975],
        )

        b = best_res.knobs
        logger.info("\n" + "=" * 90)
        logger.info("  GLOBAL OPTIMAL KNOB SETTINGS (MAXIMUM REALIZED ANNUAL RISK-ADJUSTED RETURN)")
        logger.info("=" * 90)
        logger.info(f"  1. Entry Timing Horizon:       {b.min_hours_to_expiry:.2f}h to {b.max_hours_to_expiry:.2f}h before cutoff")
        logger.info(f"  2. Contract Price Band:        ${b.min_price:.3f} to ${b.max_price:.3f} (Min edge: {b.min_net_edge_bps:.0f} bps)")
        logger.info(f"  3. Max Single Bet Allocation:  {b.max_single_bet_pct * 100:.2f}% (${args.bankroll * b.max_single_bet_pct:,.2f})")
        logger.info(f"  4. Max Category Concentration: {b.max_category_pct * 100:.2f}% (${args.bankroll * b.max_category_pct:,.2f})")
        logger.info(f"  5. Max Active Positions:       {b.max_active_markets} concurrent markets")
        logger.info(f"  6. Cash Reserve Buffer:        {b.cash_reserve_pct * 100:.1f}% (${args.bankroll * b.cash_reserve_pct:,.2f} liquid)")
        logger.info("-" * 90)
        logger.info("  REALIZED PERFORMANCE AT OPTIMAL KNOBS:")
        logger.info(f"  * Realized ARR:                +{best_res.realized_arr_pct:,.2f}% / year")
        logger.info(f"  * Realized APY:                +{best_res.realized_apy_pct:,.2f}% / year (Compounded)")
        logger.info(f"  * Realized Calmar Ratio:       {best_res.calmar_ratio:.2f} (ARR / MaxDD)")
        logger.info(f"  * Realized Sharpe Ratio:       {best_res.sharpe_ratio:.2f}")
        logger.info(f"  * Realized Sortino Ratio:      {best_res.sortino_ratio:.2f}")
        logger.info(f"  * Max Peak-to-Trough Drawdown: {best_res.max_drawdown_pct:.2f}% (${best_res.max_drawdown_usd:,.2f})")
        logger.info(f"  * Capital Turnover:            {best_res.capital_turnover_per_day:.2f} turns / day")
        logger.info(f"  * Reversals:                   {best_res.reversals_count} ({best_res.trades_executed} executed)")
        logger.info("=" * 90)

        # Save optimal config
        out_p = Path(args.out_config)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        with open(out_p, "w") as f:
            json.dump({
                "optimal_knobs": b.to_dict(),
                "performance_summary": asdict(best_res),
            }, f, indent=2)
        logger.info(f"\nSaved optimal knobs configuration to {out_p}")
        return 0

    # Single evaluation mode with user-specified knobs
    knobs = Domain2Knobs(
        min_hours_to_expiry=args.min_hours,
        max_hours_to_expiry=args.max_hours,
        min_price=args.min_price,
        max_price=args.max_price,
        min_net_edge_bps=args.min_edge_bps,
        max_single_bet_pct=args.max_single_bet,
        max_category_pct=args.max_category_cap,
        max_active_markets=args.max_markets,
        cash_reserve_pct=args.reserve_pct,
        max_drawdown_target_pct=args.max_dd,
        variance_penalty_lambda=args.lambda_var,
        objective_metric=args.metric,  # type: ignore
    )

    res = opt.evaluate_knobs(knobs, trades_df)

    logger.info("\n" + "=" * 90)
    logger.info("  DOMAIN 2 KNOB EVALUATION STATEMENT")
    logger.info("=" * 90)
    logger.info(f"  Bankroll:            ${args.bankroll:,.2f} USDC")
    logger.info(f"  Entry Hours:         {knobs.min_hours_to_expiry:.2f}h to {knobs.max_hours_to_expiry:.2f}h")
    logger.info(f"  Price Band:          ${knobs.min_price:.3f} to ${knobs.max_price:.3f}")
    logger.info(f"  Max Single Bet:      {knobs.max_single_bet_pct * 100:.2f}% (${args.bankroll * knobs.max_single_bet_pct:,.2f})")
    logger.info(f"  Max Category Cap:    {knobs.max_category_pct * 100:.2f}% (${args.bankroll * knobs.max_category_pct:,.2f})")
    logger.info(f"  Cash Reserve:        {knobs.cash_reserve_pct * 100:.1f}%")
    logger.info("-" * 90)
    logger.info(f"  Net PnL (2.36 Days): ${res.net_pnl_usd:,.2f} ({res.total_return_pct:+.2f}%)")
    logger.info(f"  Realized ARR:        {res.realized_arr_pct:+.2f}% / year")
    logger.info(f"  Realized APY:        {res.realized_apy_pct:+.2f}% / year")
    logger.info(f"  Max Drawdown:        {res.max_drawdown_pct:.2f}% (${res.max_drawdown_usd:,.2f})")
    logger.info(f"  Calmar Ratio:        {res.calmar_ratio:.2f}")
    logger.info(f"  Sharpe Ratio:        {res.sharpe_ratio:.2f}")
    logger.info(f"  Capital Turnover:    {res.capital_turnover_per_day:.2f} turns / day")
    logger.info(f"  Reversals:           {res.reversals_count} / {res.trades_executed} trades")
    logger.info(f"  Constraint Status:   {'PASSED' if res.is_feasible else 'VIOLATED (' + res.constraint_violation + ')'}")
    logger.info("=" * 90)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
