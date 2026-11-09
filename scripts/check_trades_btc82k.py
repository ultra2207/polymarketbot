"""Check trades for the high-update BTC $82k market in hour 15."""
import duckdb
from polymarketbot.logs import get_logger
log = get_logger(__name__)

con = duckdb.connect()
con.execute("SET threads=2")

tokens = [
    '112554888495466442543547906736420692852179589019408913268537259635055663664935',
    '63766384375100326468742425255147738274545018462000914589893920075817188460054'
]

tokens_str = ", ".join(f"'{t}'" for t in tokens)

df = con.execute(f"""
    SELECT venue_ts_ms, recv_ts_ns, asset_id, side, price, size
    FROM read_parquet('data/tape/clob_trades/date=2026-11-05/hour=15/*.parquet')
    WHERE asset_id IN ({tokens_str})
    ORDER BY venue_ts_ms
""").df()

log.info(f"Total trades in hour 15 for BTC $82k Nov 6: {len(df)}")
if len(df) > 0:
    log.info(df.to_string())
