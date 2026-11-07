"""Analyze Polymarket CLOB response during massive Binance price jumps."""
import json
from pathlib import Path
import duckdb
import pandas as pd
from polymarketbot.logs import get_logger

log = get_logger("analyze_clob_jump_response")

def main():
    log.info("Starting analyze_clob_jump_response")
    
    # Load jump events
    jump_file = Path("data/derived/jump_analysis_multi.json")
    with open(jump_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    # Load BTC tokens mapping
    with open("data/derived/btc_tokens_map.json", "r", encoding="utf-8") as f:
        btc_tokens = json.load(f)
        
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET temp_directory='.tmp/duckdb_spill'")
    
    # Selected key jump events from 2026-11-05
    events_to_test = [
        {"name": "BTC Drop 30.7bps", "symbol": "BTCUSDT", "ts_ms": 1793890910421, "utc": "2026-11-05 15:01:50.421", "hour": "15", "dir": "DOWN", "bps": 30.7},
        {"name": "BTC Drop 25.3bps & ETH Drop 41.9bps", "symbol": "BTCUSDT", "ts_ms": 1793891166787, "utc": "2026-11-05 15:06:06.787", "hour": "15", "dir": "DOWN", "bps": 25.3},
        {"name": "BTC Drop 25.1bps & SOL Drop 43.5bps", "symbol": "BTCUSDT", "ts_ms": 1793891504831, "utc": "2026-11-05 15:11:44.831", "hour": "15", "dir": "DOWN", "bps": 25.1},
        {"name": "BTC Drop 30.2bps", "symbol": "BTCUSDT", "ts_ms": 1793892369194, "utc": "2026-11-05 15:26:09.194", "hour": "15", "dir": "DOWN", "bps": 30.2},
        {"name": "BTC Drop 25.6bps", "symbol": "BTCUSDT", "ts_ms": 1793893916199, "utc": "2026-11-05 15:51:56.199", "hour": "15", "dir": "DOWN", "bps": 25.6},
        {"name": "BTC Surge UP 28.5bps", "symbol": "BTCUSDT", "ts_ms": 1793894003399, "utc": "2026-11-05 15:53:23.399", "hour": "15", "dir": "UP", "bps": 28.5},
        {"name": "BTC Drop 32.8bps", "symbol": "BTCUSDT", "ts_ms": 1793919691372, "utc": "2026-11-05 23:01:31.372", "hour": "23", "dir": "DOWN", "bps": 32.8},
        {"name": "ETH Crash 50.6bps", "symbol": "ETHUSDT", "ts_ms": 1793920094260, "utc": "2026-11-05 23:08:14.260", "hour": "23", "dir": "DOWN", "bps": 50.6},
    ]
    
    results = []
    
    for ev in events_to_test:
        ts_ms = ev["ts_ms"]
        hour = ev["hour"]
        log.info(f"\n========================================================")
        log.info(f"ANALYZING EVENT: {ev['name']} at {ev['utc']} UTC (ts_ms={ts_ms})")
        
        # 1. Look for ALL trades in clob_trades within [ts_ms - 2000, ts_ms + 15000]
        window_lo_ms = ts_ms - 2000
        window_hi_ms = ts_ms + 15000
        
        trades_query = f"""
            SELECT 
                venue_ts_ms,
                recv_ts_ns,
                asset_id,
                side,
                price,
                size
            FROM read_parquet('data/tape/clob_trades/date=2026-11-05/hour={hour}/*.parquet')
            WHERE venue_ts_ms BETWEEN {window_lo_ms} AND {window_hi_ms}
            ORDER BY venue_ts_ms, recv_ts_ns
        """
        trades_df = con.execute(trades_query).df()
        log.info(f"Total CLOB trades across ALL markets in window [{window_lo_ms}, {window_hi_ms}]: {len(trades_df)}")
        
        crypto_trades = []
        for _, tr in trades_df.iterrows():
            aid = str(tr["asset_id"])
            if aid in btc_tokens:
                crypto_trades.append({
                    "venue_ts_ms": int(tr["venue_ts_ms"]),
                    "recv_ts_ns": int(tr["recv_ts_ns"]),
                    "delta_venue_ms": int(tr["venue_ts_ms"]) - ts_ms,
                    "asset_id": aid,
                    "question": btc_tokens[aid]["question"],
                    "side": tr["side"],
                    "price": float(tr["price"]),
                    "size": float(tr["size"]),
                    "notional_usd": float(tr["price"]) * float(tr["size"])
                })
                
        log.info(f"Crypto/BTC trades in this window: {len(crypto_trades)}")
        for ct in crypto_trades:
            log.info(f"  TRADE: Delta={ct['delta_venue_ms']:+d}ms | {ct['side']} {ct['size']:.1f} @ {ct['price']:.3f} (${ct['notional_usd']:.2f}) | {ct['question']}")

        # 2. Look for quote changes in clob_events for BTC tokens
        # Since clob_events has 15M rows per hour, filter by venue_ts_ms first!
        # Remember AGENTS.md: filter on partition/time key before unnesting
        events_query = f"""
            WITH raw_e AS (
                SELECT 
                    event_type,
                    venue_ts_ms,
                    recv_ts_ns,
                    price_changes
                FROM read_parquet('data/tape/clob_events/date=2026-11-05/hour={hour}/*.parquet')
                WHERE venue_ts_ms BETWEEN {window_lo_ms} AND {window_hi_ms}
                  AND event_type = 'price_change'
            ), unnested AS (
                SELECT 
                    r.venue_ts_ms,
                    r.recv_ts_ns,
                    c.asset_id,
                    c.price,
                    c.size,
                    c.side,
                    c.best_bid,
                    c.best_ask
                FROM raw_e r, unnest(r.price_changes) as t(c)
                WHERE c.asset_id IN ({",".join(f"'{t}'" for t in list(btc_tokens.keys())[:100])})
            )
            SELECT * FROM unnested
            ORDER BY venue_ts_ms, recv_ts_ns
        """
        try:
            clob_ev_df = con.execute(events_query).df()
            log.info(f"BTC quote updates in window: {len(clob_ev_df)}")
            if len(clob_ev_df) > 0:
                first_ev = clob_ev_df.iloc[0]
                delta_ev_ms = int(first_ev["venue_ts_ms"]) - ts_ms
                log.info(f"  First BTC quote update: Delta={delta_ev_ms:+d}ms | Token: {first_ev['asset_id'][:10]}... | Bid: {first_ev['best_bid']} Ask: {first_ev['best_ask']}")
                # Group by asset_id to see response
                for aid, grp in clob_ev_df.groupby("asset_id"):
                    q = btc_tokens.get(aid, {}).get("question", aid)
                    min_delta = grp["venue_ts_ms"].min() - ts_ms
                    log.info(f"  Market: {q[:45]} | Updates: {len(grp)} | First Delta: {min_delta:+d}ms")
        except Exception as exc:
            log.warning(f"Error querying clob_events: {exc}")
            
        results.append({
            "event": ev,
            "total_clob_trades": len(trades_df),
            "crypto_trades": crypto_trades
        })
        
    out_path = Path("data/derived/jump_clob_response_summary.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    log.info(f"\nSaved jump response summary to {out_path}")

if __name__ == "__main__":
    main()
