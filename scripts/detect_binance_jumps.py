"""Detect sudden price jumps in Binance BTC-USDT BBO.

Task 1:
- Query Binance BTC-USDT BBO from data/tape/binance_bbo for 2026-11-05 and 2026-11-06.
- Identify sudden price jumps (>25 to 50 bps within <= 3 seconds).
- Record exact timestamps, initial price, peak/trough price, duration, return in bps.
"""

import json
from pathlib import Path
import duckdb
import pandas as pd
from polymarketbot.logs import get_logger

log = get_logger("detect_binance_jumps")

def main():
    log.info("Starting detect_binance_jumps")
    log.info("Expected cost: scanning ~1.06M rows of binance_bbo parquet across 2026-11-05 and 2026-11-06.")
    
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET temp_directory='.tmp/duckdb_spill'")
    
    # 1. Query BTCUSDT ticks with mid-price
    # Note: venue_ts_ms is exchange timestamp in ms, recv_ts_ns is collector timestamp
    query = """
    WITH raw_bbo AS (
        SELECT 
            venue_ts_ms,
            recv_ts_ns,
            (bid + ask) / 2.0 AS mid,
            bid,
            ask
        FROM read_parquet([
            'data/tape/binance_bbo/date=2026-11-05/*/*.parquet',
            'data/tape/binance_bbo/date=2026-11-06/*/*.parquet'
        ])
        WHERE symbol = 'BTCUSDT'
        ORDER BY venue_ts_ms, recv_ts_ns
    )
    SELECT * FROM raw_bbo
    """
    
    df = con.execute(query).df()
    log.info(f"Loaded {len(df):,} BTCUSDT ticks from Binance BBO.")
    
    # Drop duplicates on venue_ts_ms if any, keep first
    df = df.drop_duplicates(subset=["venue_ts_ms"]).reset_index(drop=True)
    log.info(f"Unique venue_ts_ms ticks: {len(df):,}")
    
    # Let's search for rolling 1s, 2s, 3s jumps
    # Since ticks are irregular (roughly 100ms or on update), we can use asof/searchsorted or rolling window on venue_ts_ms
    # Using numpy searchsorted for fast O(N log N) or O(N) sliding window
    ts = df["venue_ts_ms"].values
    mids = df["mid"].values
    n = len(ts)
    
    jumps = []
    
    # Windows to check: 1000ms, 2000ms, 3000ms, 5000ms
    # We want jumps with |pct_change| >= 25 bps (0.0025) within <= 3000ms
    # To avoid duplicate overlapping detections of the same event, we'll find peak return in rolling 3s
    
    import numpy as np
    
    # For each tick i, look back to find min/max in [ts[i] - 3000, ts[i]]
    # Or look forward: for tick i, find price at i + dt (dt <= 3000ms)
    
    log.info("Calculating forward max/min price changes within 3 seconds...")
    # Right index where ts[j] <= ts[i] + 3000
    right_indices = np.searchsorted(ts, ts + 3000, side='right') - 1
    
    # Also check 5-second window to capture slightly longer surges
    right_indices_5s = np.searchsorted(ts, ts + 5000, side='right') - 1
    
    # We can compute return between i and right_indices[i]
    ret_3s = (mids[right_indices] - mids) / mids * 10000.0 # bps
    
    # Also let's find the max excursion (max mid and min mid) within the 3s window
    # To do this efficiently, let's sample or use rolling max/min
    # Let's look at ret_3s >= 20 bps or <= -20 bps
    candidate_indices = np.where(np.abs(ret_3s) >= 20.0)[0]
    log.info(f"Candidate ticks with >= 20 bps move in 3s: {len(candidate_indices):,}")
    
    jump_events = []
    # Cluster contiguous candidate indices into distinct jump events
    if len(candidate_indices) > 0:
        clusters = []
        curr_cluster = [candidate_indices[0]]
        for idx in candidate_indices[1:]:
            if idx == curr_cluster[-1] + 1 or (ts[idx] - ts[curr_cluster[-1]] <= 10000):
                curr_cluster.append(idx)
            else:
                clusters.append(curr_cluster)
                curr_cluster = [idx]
        clusters.append(curr_cluster)
        
        log.info(f"Identified {len(clusters)} distinct jump clusters.")
        
        for cluster in clusters:
            # For this cluster, find the start and the maximum displacement within 3-5 seconds
            start_idx = cluster[0]
            start_ts = ts[start_idx]
            start_price = mids[start_idx]
            
            # Look up to 5s after the last point in the cluster or 10s from start
            end_limit_idx = np.searchsorted(ts, ts[cluster[-1]] + 5000, side='right') - 1
            end_limit_idx = min(end_limit_idx, n - 1)
            
            window_slice_mids = mids[start_idx : end_limit_idx + 1]
            window_slice_ts = ts[start_idx : end_limit_idx + 1]
            
            # Find max upward and downward move from start_price
            max_p_idx = np.argmax(window_slice_mids)
            min_p_idx = np.argmin(window_slice_mids)
            
            max_up_bps = (window_slice_mids[max_p_idx] - start_price) / start_price * 10000.0
            max_down_bps = (window_slice_mids[min_p_idx] - start_price) / start_price * 10000.0
            
            if max_up_bps >= abs(max_down_bps):
                peak_idx = max_p_idx
                jump_bps = max_up_bps
                direction = "UP"
            else:
                peak_idx = min_p_idx
                jump_bps = max_down_bps
                direction = "DOWN"
                
            peak_ts = window_slice_ts[peak_idx]
            peak_price = window_slice_mids[peak_idx]
            duration_ms = peak_ts - start_ts
            
            jump_events.append({
                "start_venue_ts_ms": int(start_ts),
                "peak_venue_ts_ms": int(peak_ts),
                "start_utc": pd.to_datetime(start_ts, unit='ms', utc=True).isoformat(),
                "peak_utc": pd.to_datetime(peak_ts, unit='ms', utc=True).isoformat(),
                "start_price": float(start_price),
                "peak_price": float(peak_price),
                "abs_move_usd": float(peak_price - start_price),
                "jump_bps": float(jump_bps),
                "duration_ms": int(duration_ms),
                "direction": direction,
                "recv_ts_ns": int(df["recv_ts_ns"].values[start_idx])
            })
            
    # Filter to events with |jump_bps| >= 25.0
    significant_jumps = [j for j in jump_events if abs(j["jump_bps"]) >= 25.0]
    significant_jumps.sort(key=lambda x: abs(x["jump_bps"]), reverse=True)
    
    log.info(f"Significant jumps (>= 25 bps): {len(significant_jumps)}")
    
    # Save to json and parquet
    out_path = Path("data/derived/binance_btc_jumps.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(significant_jumps, f, indent=2)
    log.info(f"Saved {len(significant_jumps)} jumps to {out_path}")
    
    # Print top 15 jumps
    log.info("Top 15 Big Jumps on Binance BTC-USDT:")
    for i, j in enumerate(significant_jumps[:15], 1):
        log.info(
            f"#{i}: {j['start_utc']} -> {j['direction']} {abs(j['jump_bps']):.1f} bps "
            f"(${j['start_price']:.1f} -> ${j['peak_price']:.1f}, "
            f"diff: ${j['abs_move_usd']:+.1f}) in {j['duration_ms']} ms"
        )

if __name__ == "__main__":
    main()
