"""Inspect order book resting depth on Polymarket BTC contracts."""
import json
from pathlib import Path
import duckdb
from polymarketbot.logs import get_logger

log = get_logger("inspect_btc_depth")

def main():
    log.info("Starting inspect_btc_depth")
    
    with open("data/derived/btc_tokens_map.json", "r", encoding="utf-8") as f:
        btc_tokens = json.load(f)
        
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET temp_directory='.tmp/duckdb_spill'")
    
    # Query book events in hour 15 for BTC tokens
    tokens_sample = list(btc_tokens.keys())[:30]
    
    query = f"""
        SELECT 
            asset_id,
            venue_ts_ms,
            bids,
            asks
        FROM read_parquet('data/tape/clob_events/date=2026-11-05/hour=15/*.parquet')
        WHERE event_type = 'book'
          AND asset_id IN ({",".join(f"'{t}'" for t in tokens_sample)})
        ORDER BY venue_ts_ms
        LIMIT 50
    """
    
    df = con.execute(query).df()
    log.info(f"Loaded {len(df)} book snapshots for BTC tokens")
    
    if len(df) == 0:
        # Check price_change events for best_bid, best_ask and sizes
        log.info("Checking price_changes for sizes...")
        pc_query = f"""
            WITH raw_e AS (
                SELECT venue_ts_ms, price_changes
                FROM read_parquet('data/tape/clob_events/date=2026-11-05/hour=15/*.parquet')
                WHERE event_type = 'price_change'
                LIMIT 50000
            ), u AS (
                SELECT r.venue_ts_ms, c.asset_id, c.price, c.size, c.side, c.best_bid, c.best_ask
                FROM raw_e r, unnest(r.price_changes) as t(c)
                WHERE c.asset_id IN ({",".join(f"'{t}'" for t in tokens_sample)})
            )
            SELECT * FROM u WHERE size > 0 LIMIT 20
        """
        df_pc = con.execute(pc_query).df()
        log.info(f"Sample price change updates with size > 0: {len(df_pc)}")
        for _, row in df_pc.iterrows():
            aid = row["asset_id"]
            q = btc_tokens.get(aid, {}).get("question", aid)
            dollar_val = row["price"] * row["size"]
            log.info(f"  {q[:40]} | Side: {row['side']} | Price: {row['price']:.3f} | Size: {row['size']:.1f} | Depth: ${dollar_val:.2f} | BBO: {row['best_bid']} / {row['best_ask']}")
    else:
        for _, row in df.iloc[:10].iterrows():
            aid = row["asset_id"]
            q = btc_tokens.get(aid, {}).get("question", aid)
            log.info(f"Market: {q[:40]} | Bids: {len(row['bids']) if row['bids'] is not None else 0} | Asks: {len(row['asks']) if row['asks'] is not None else 0}")

if __name__ == "__main__":
    main()
