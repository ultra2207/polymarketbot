#!/usr/bin/env python
"""Empirical analysis of UMA Settlement Basis and Resolution Discount Harvester.

Analyzes:
1. UMA dispute and proposal events in `data/tape/uma_events`.
2. High-price trades ($0.95 - $0.995) in `data/tape/clob_trades`.
3. Alignment with on-chain resolutions from `data/derived/uma/res_nov*.parquet`.
4. Order book depth and spread during the 2-hour UMA dispute liveness window.
5. Capital velocity, turnover, and expected ROE modeling.
"""

from __future__ import annotations

import json
from pathlib import Path
import duckdb
import pandas as pd
from polymarketbot.logs import get_logger

logger = get_logger("analyze_settlement_discount")

def main():
    logger.info("Initializing DuckDB connection with threads=2...")
    con = duckdb.connect()
    con.execute("SET threads=2;")

    # 1. Decode UMA Proposals & Disputes
    logger.info("Analyzing UMA Events in tape...")
    uma_summary = con.execute("""
        SELECT 
            event, 
            count(*) as count, 
            count(distinct question) as distinct_questions,
            count(distinct condition_id) as distinct_conditions,
            avg(CASE WHEN expiry IS NOT NULL AND recv_ts_ns IS NOT NULL THEN (expiry - recv_ts_ns/1e9)/3600.0 ELSE NULL END) as avg_liveness_hours
        FROM read_parquet('data/tape/uma_events/**/*.parquet')
        GROUP BY 1
    """).df()
    logger.info("UMA events breakdown:\n{}", uma_summary)

    # 2. Decode CTF Resolutions
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

    logger.info("Total resolved conditions decoded: {}", len(res_df))

    # 3. Analyze Trade Flows & Discounts
    logger.info("Analyzing trade flows near resolution...")
    q_trades = """
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
            (r.res_ts_ns - t.venue_ts_ms * 1000000) / 1e9 / 3600.0 as hours_before_res,
            t.size * t.price as vol_usdc,
            u.u_question,
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
        WHERE t.price >= 0.95 AND t.price <= 0.995
    )
    SELECT * FROM trades_joined;
    """
    df_trades = con.execute(q_trades).df()
    logger.info("Total discount trades (0.95-0.995) on resolved markets: {}", len(df_trades))
    logger.info("Total discount volume: ${:,.2f} USDC across {:,.0f} shares", df_trades['vol_usdc'].sum(), df_trades['size'].sum())

    # Window analysis
    df_trades['time_bucket'] = pd.cut(
        df_trades['hours_before_res'],
        bins=[-0.01, 0.5, 1.0, 2.0, 6.0, 24.0, 1000.0],
        labels=['<30m', '30m-1h', '1h-2h', '2h-6h', '6h-24h', '>24h']
    )

    window_stats = df_trades[df_trades['token_payout'] == 1.0].groupby('time_bucket', observed=False).agg(
        trades=('price', 'count'),
        vol_usdc=('vol_usdc', 'sum'),
        avg_price=('price', 'mean'),
        gross_profit_usdc=('vol_usdc', lambda v: (df_trades.loc[v.index, 'size'] - v).sum())
    )
    logger.info("Winning shares discount harvest by time bucket:\n{}", window_stats)

    # 4. Taker side breakdown in final 2 hours
    liveness_trades = df_trades[(df_trades['hours_before_res'] <= 2.0) & (df_trades['token_payout'] == 1.0)]
    side_breakdown = liveness_trades.groupby('side').agg(
        trades=('price', 'count'),
        vol_usdc=('vol_usdc', 'sum'),
        avg_price=('price', 'mean')
    )
    logger.info("Taker side breakdown in 2h UMA liveness window:\n{}", side_breakdown)

    # 5. Order Book Depth in Final 2 Hours
    logger.info("Analyzing order book depth in 2h liveness window...")
    q_book = """
    WITH u AS (
        SELECT condition_id, tokens[1] as tok0, tokens[2] as tok1
        FROM read_parquet('data/derived/uma/universe_map.parquet')
    ),
    quotes AS (
        SELECT 
            q.condition_id,
            q.asset_id,
            q.bb,
            q.ba,
            q.bsz,
            q.asz,
            (r.res_ts_ns - q.venue_ts_ms * 1000000) / 1e9 / 3600.0 as hours_before_res,
            CASE 
                WHEN q.asset_id = u.tok0 AND r.payout0 > 0 THEN 1.0
                WHEN q.asset_id = u.tok1 AND r.payout1 > 0 THEN 1.0
                ELSE 0.0
            END as is_winning_token
        FROM read_parquet('data/derived/uma/rq_2026-11-05.parquet') q
        JOIN decoded_resolutions r ON q.condition_id = r.condition_id
        JOIN u ON q.condition_id = u.condition_id
        WHERE is_winning_token = 1.0
          AND hours_before_res BETWEEN 0 AND 2.0
          AND q.bb >= 0.95
    )
    SELECT 
        count(*) as quote_updates,
        count(distinct condition_id) as conditions,
        round(avg(bb), 4) as avg_best_bid,
        round(avg(ba), 4) as avg_best_ask,
        round(avg(ba - bb), 4) as avg_spread,
        round(avg(bsz), 1) as avg_bid_size,
        round(median(bsz), 1) as median_bid_size,
        round(avg(asz), 1) as avg_ask_size,
        round(median(asz), 1) as median_ask_size
    FROM quotes;
    """
    df_book = con.execute(q_book).df()
    logger.info("Order book metrics in 2h window:\n{}", df_book)

    # Save summary artifact JSON
    out_dir = Path("data/derived/uma")
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = out_dir / "settlement_discount_summary.json"
    
    summary_data = {
        "uma_proposals_in_tape": int(uma_summary[uma_summary['event'] == 'ProposePrice']['count'].iloc[0]),
        "uma_disputes_in_tape": 0,
        "resolved_conditions_in_tape": len(res_df),
        "total_discount_trades_tape": len(df_trades),
        "total_discount_vol_tape_usdc": float(df_trades['vol_usdc'].sum()),
        "liveness_2h_winning_trades": int(len(liveness_trades)),
        "liveness_2h_winning_vol_usdc": float(liveness_trades['vol_usdc'].sum()),
        "liveness_2h_gross_profit_usdc": float((liveness_trades['size'] - liveness_trades['vol_usdc']).sum()),
        "liveness_2h_avg_price": float(liveness_trades['price'].mean()),
        "avg_liveness_duration_hours": 2.0,
        "estimated_annual_apr": float((1.0 - liveness_trades['price'].mean()) / liveness_trades['price'].mean() * (8760 / 2.0) * 100)
    }
    with open(summary_path, "w") as f:
        json.dump(summary_data, f, indent=2)
    logger.info("Saved summary metrics to {}", summary_path)
    logger.info("Execution complete.")

if __name__ == "__main__":
    main()
