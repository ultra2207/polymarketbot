"""Inspect top of book depth in dollars on Polymarket BTC books."""
import json
import duckdb

con = duckdb.connect()
con.execute("SET threads=2")

res = con.execute("""
    SELECT asset_id, venue_ts_ms, bids, asks
    FROM read_parquet('data/tape/clob_events/date=2026-11-05/hour=15/*.parquet')
    WHERE event_type = 'book'
      AND bids IS NOT NULL
    LIMIT 15
""").df()

with open("data/derived/btc_tokens_map.json", "r", encoding="utf-8") as f:
    btc_map = json.load(f)

for _, row in res.iterrows():
    aid = str(row["asset_id"])
    q = btc_map.get(aid, {}).get("question", "Unknown")
    bids = list(row["bids"]) if row["bids"] is not None else []
    asks = list(row["asks"]) if row["asks"] is not None else []
    top_bids = bids[:3]
    top_asks = asks[:3]
    
    bid_depth_usd = sum(float(b["price"]) * float(b["size"]) for b in top_bids)
    ask_depth_usd = sum(float(a["price"]) * float(a["size"]) for a in top_asks)
    
    print(f"\nMarket: {q[:55]}")
    print(f"  Top 3 Bids: {top_bids} -> Depth: ${bid_depth_usd:.2f}")
    print(f"  Top 3 Asks: {top_asks} -> Depth: ${ask_depth_usd:.2f}")
