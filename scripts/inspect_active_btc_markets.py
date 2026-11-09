"""Inspect active BTC November 6 barrier markets."""
import json
import duckdb
from polymarketbot.logs import get_logger
log = get_logger(__name__)

with open("data/derived/btc_tokens_map.json", "r", encoding="utf-8") as f:
    btc_map = json.load(f)

target_tokens = {}
for t, info in btc_map.items():
    q = info["question"]
    if "above $7" in q or "above $8" in q or "above $76" in q or "above $78" in q or "above $80" in q:
        target_tokens[t] = q

log.info(f"Found {len(target_tokens)} target tokens:")
for t, q in target_tokens.items():
    log.info(f"  {t}: {q}")

con = duckdb.connect()
con.execute("SET threads=2")

tokens_str = ", ".join(f"'{t}'" for t in target_tokens)
query = f"""
    WITH raw_e AS (
        SELECT venue_ts_ms, price_changes
        FROM read_parquet('data/tape/clob_events/date=2026-11-05/hour=15/*.parquet')
        WHERE event_type = 'price_change'
    ), u AS (
        SELECT r.venue_ts_ms, c.asset_id, c.price, c.size, c.side, c.best_bid, c.best_ask
        FROM raw_e r, unnest(r.price_changes) as t(c)
        WHERE c.asset_id IN ({tokens_str})
    )
    SELECT asset_id, count(*) as updates, avg(best_ask - best_bid) as avg_spread, max(size) as max_size, avg(size) as avg_size
    FROM u
    GROUP BY asset_id
"""
res = con.execute(query).df()
log.info("\nActivity in hour 15 for target tokens:")
for _, row in res.iterrows():
    aid = row["asset_id"]
    q = target_tokens.get(aid, aid)
    log.info(f"Token: {aid[:15]}... ({q[:40]}) -> Updates: {row['updates']}, Avg Spread: {row['avg_spread']:.3f}, Max Size: {row['max_size']}, Avg Size: {row['avg_size']:.1f}")
