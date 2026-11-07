"""Empirical jump distribution and multi-symbol jump detection."""
import json
from pathlib import Path
import duckdb
import numpy as np
import pandas as pd
from polymarketbot.logs import get_logger

log = get_logger("jump_distribution")

def analyze_symbol(con, symbol, dates):
    log.info(f"Analyzing {symbol} for dates {dates}...")
    file_patterns = [f"data/tape/binance_bbo/date={d}/*/*.parquet" for d in dates]
    files_str = ", ".join(f"'{p}'" for p in file_patterns)
    
    query = f"""
        SELECT venue_ts_ms, recv_ts_ns, (bid + ask) / 2.0 AS mid
        FROM read_parquet([{files_str}])
        WHERE symbol = '{symbol}'
        ORDER BY venue_ts_ms, recv_ts_ns
    """
    df = con.execute(query).df()
    df = df.drop_duplicates(subset=["venue_ts_ms"]).reset_index(drop=True)
    n = len(df)
    log.info(f"{symbol}: {n:,} unique venue ticks")
    if n == 0:
        return {}, []

    ts = df["venue_ts_ms"].values
    mids = df["mid"].values
    
    # Calculate returns over horizons: 1s, 3s, 5s, 10s, 30s
    horizons = [1000, 3000, 5000, 10000, 30000]
    horizon_stats = {}
    
    for h_ms in horizons:
        r_idx = np.searchsorted(ts, ts + h_ms, side='right') - 1
        r_idx = np.clip(r_idx, 0, n - 1)
        # return in bps
        ret_bps = (mids[r_idx] - mids) / mids * 10000.0
        abs_ret_bps = np.abs(ret_bps)
        
        horizon_stats[f"{h_ms}ms"] = {
            "p50_bps": float(np.percentile(abs_ret_bps, 50)),
            "p90_bps": float(np.percentile(abs_ret_bps, 90)),
            "p99_bps": float(np.percentile(abs_ret_bps, 99)),
            "p99.9_bps": float(np.percentile(abs_ret_bps, 99.9)),
            "max_bps": float(np.max(abs_ret_bps)),
            "count_ge_10bps": int(np.sum(abs_ret_bps >= 10.0)),
            "count_ge_15bps": int(np.sum(abs_ret_bps >= 15.0)),
            "count_ge_20bps": int(np.sum(abs_ret_bps >= 20.0)),
            "count_ge_25bps": int(np.sum(abs_ret_bps >= 25.0)),
            "count_ge_50bps": int(np.sum(abs_ret_bps >= 50.0)),
        }
        log.info(f"{symbol} horizon {h_ms}ms: max={horizon_stats[f'{h_ms}ms']['max_bps']:.1f} bps, "
                 f">=15bps: {horizon_stats[f'{h_ms}ms']['count_ge_15bps']} ticks, "
                 f">=25bps: {horizon_stats[f'{h_ms}ms']['count_ge_25bps']} ticks, "
                 f">=50bps: {horizon_stats[f'{h_ms}ms']['count_ge_50bps']} ticks")
        
    # Extract all distinct jump events for 3s-5s horizon >= 15 bps
    r_idx_5s = np.searchsorted(ts, ts + 5000, side='right') - 1
    ret_5s = (mids[r_idx_5s] - mids) / mids * 10000.0
    cand_15 = np.where(np.abs(ret_5s) >= 15.0)[0]
    
    events = []
    if len(cand_15) > 0:
        clusters = []
        curr = [cand_15[0]]
        for idx in cand_15[1:]:
            if ts[idx] - ts[curr[-1]] <= 5000:
                curr.append(idx)
            else:
                clusters.append(curr)
                curr = [idx]
        clusters.append(curr)
        
        for c in clusters:
            s_idx = c[0]
            s_ts = ts[s_idx]
            s_p = mids[s_idx]
            
            # Look up to 10s from cluster start
            e_idx = min(np.searchsorted(ts, s_ts + 10000, side='right') - 1, n - 1)
            sub_mids = mids[s_idx : e_idx + 1]
            sub_ts = ts[s_idx : e_idx + 1]
            
            max_i = np.argmax(sub_mids)
            min_i = np.argmin(sub_mids)
            up_bps = (sub_mids[max_i] - s_p) / s_p * 10000.0
            dn_bps = (sub_mids[min_i] - s_p) / s_p * 10000.0
            
            if up_bps >= abs(dn_bps):
                p_i = max_i
                jbps = up_bps
                direction = "UP"
            else:
                p_i = min_i
                jbps = dn_bps
                direction = "DOWN"
                
            events.append({
                "symbol": symbol,
                "start_venue_ts_ms": int(s_ts),
                "peak_venue_ts_ms": int(sub_ts[p_i]),
                "start_utc": pd.to_datetime(s_ts, unit='ms', utc=True).isoformat(),
                "peak_utc": pd.to_datetime(sub_ts[p_i], unit='ms', utc=True).isoformat(),
                "start_price": float(s_p),
                "peak_price": float(sub_mids[p_i]),
                "abs_move_usd": float(sub_mids[p_i] - s_p),
                "jump_bps": float(jbps),
                "duration_ms": int(sub_ts[p_i] - s_ts),
                "direction": direction,
                "recv_ts_ns": int(df["recv_ts_ns"].values[s_idx])
            })
            
    events.sort(key=lambda x: abs(x["jump_bps"]), reverse=True)
    return horizon_stats, events

def main():
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET temp_directory='.tmp/duckdb_spill'")
    
    dates = ["2026-11-05", "2026-11-06"]
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    
    all_stats = {}
    all_events = {}
    
    for s in symbols:
        stats, events = analyze_symbol(con, s, dates)
        all_stats[s] = stats
        all_events[s] = events
        log.info(f"{s}: found {len(events)} discrete jump events >= 15 bps")
        for i, ev in enumerate(events[:10], 1):
            log.info(f"  #{i}: {ev['start_utc']} | {ev['direction']} {abs(ev['jump_bps']):.1f} bps | dur: {ev['duration_ms']}ms | ${ev['start_price']:.2f} -> ${ev['peak_price']:.2f}")
            
    out_file = Path("data/derived/jump_analysis_multi.json")
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump({"stats": all_stats, "events": all_events}, f, indent=2)
    log.info(f"Saved complete stats and events to {out_file}")

if __name__ == "__main__":
    main()
