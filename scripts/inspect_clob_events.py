"""Inspect schema and event types in clob_events."""
import duckdb
from polymarketbot.logs import get_logger
log = get_logger(__name__)

con = duckdb.connect()
con.execute("SET threads=2")

res = con.execute("""
    SELECT event_type, count(*) as count
    FROM read_parquet('data/tape/clob_events/date=2026-11-05/hour=15/*.parquet')
    GROUP BY event_type
""").df()
log.info("Event types in hour=15:")
log.info(res)

sample = con.execute("""
    SELECT *
    FROM read_parquet('data/tape/clob_events/date=2026-11-05/hour=15/*.parquet')
    LIMIT 3
""").df()
log.info("\nSample columns:")
for col in sample.columns:
    log.info(f"  {col}: {type(sample[col].iloc[0])} -> {sample[col].iloc[0]}")
