"""Trace Jump 6 reaction."""
import duckdb
from polymarketbot.logs import get_logger
log = get_logger(__name__)

con = duckdb.connect()
con.execute("SET threads=2")

token = '112554888495466442543547906736420692852179589019408913268537259635055663664935'

ts_start = 1793894003000
ts_end = 1793894015000

log.info("=== BINANCE BTC-USDT TICKS IN WINDOW ===")
bn_ticks = con.execute(f"""
    SELECT venue_ts_ms, (bid + ask) / 2.0 as mid, bid, ask
    FROM read_parquet('data/tape/binance_bbo/date=2026-11-05/hour=15/*.parquet')
    WHERE symbol = 'BTCUSDT' AND venue_ts_ms BETWEEN {ts_start} AND {ts_end}
    ORDER BY venue_ts_ms
""").df()
log.info(f"Total Binance ticks: {len(bn_ticks)}")
log.info(bn_ticks.iloc[::max(1, len(bn_ticks)//8)].to_string())

log.info("\n=== POLYMARKET QUOTE UPDATES FOR BTC $82k IN SAME WINDOW ===")
pm_quotes = con.execute(f"""
    WITH raw_e AS (
        SELECT venue_ts_ms, recv_ts_ns, price_changes
        FROM read_parquet('data/tape/clob_events/date=2026-11-05/hour=15/*.parquet')
        WHERE event_type = 'price_change'
          AND venue_ts_ms BETWEEN {ts_start} AND {ts_end}
    ), u AS (
        SELECT r.venue_ts_ms, r.recv_ts_ns, c.price, c.size, c.side, c.best_bid, c.best_ask
        FROM raw_e r, unnest(r.price_changes) as t(c)
        WHERE c.asset_id = '{token}'
    )
    SELECT * FROM u
    ORDER BY venue_ts_ms, recv_ts_ns
""").df()
log.info(f"Total Polymarket quote updates: {len(pm_quotes)}")
if len(pm_quotes) > 0:
    log.info(pm_quotes.iloc[::max(1, len(pm_quotes)//10)].to_string())

log.info("\n=== POLYMARKET TRADES IN SAME WINDOW ===")
pm_trades = con.execute(f"""
    SELECT venue_ts_ms, recv_ts_ns, side, price, size
    FROM read_parquet('data/tape/clob_trades/date=2026-11-05/hour=15/*.parquet')
    WHERE asset_id = '{token}'
      AND venue_ts_ms BETWEEN {ts_start} AND {ts_end}
""").df()
log.info(f"Total Polymarket trades: {len(pm_trades)}")
