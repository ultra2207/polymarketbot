"""Check clock_offset stream statistics."""
import duckdb
from polymarketbot.logs import get_logger
log = get_logger(__name__)

con = duckdb.connect()
df = con.execute("""
    SELECT 
        stream,
        host,
        count(*) as samples,
        min(rtt_ns) / 1e6 as min_rtt_ms,
        avg(rtt_ns) / 1e6 as avg_rtt_ms,
        min(offset_ns) / 1e6 as min_offset_ms,
        avg(offset_ns) / 1e6 as avg_offset_ms,
        max(offset_ns) / 1e6 as max_offset_ms
    FROM read_parquet('data/tape/clock_offset/date=2026-11-05/hour=15/*.parquet')
    GROUP BY stream, host
""").df()
log.info(df.to_string())
