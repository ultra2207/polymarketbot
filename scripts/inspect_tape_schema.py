"""Inspect schema and sample rows of clob_events and clob_trades."""
import duckdb
from polymarketbot.logs import get_logger

logger = get_logger("inspect_tape_schema")

def inspect():
    con = duckdb.connect()
    
    event_file = "data/tape/clob_events/date=2026-11-04/hour=00/part-20261104T005217-20261104T00-00.parquet"
    logger.info(f"Inspecting clob_events: {event_file}")
    df_events = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{event_file}')").df()
    logger.info(f"clob_events columns:\n{df_events[['column_name', 'column_type']]}")
    
    sample_events = con.execute(f"SELECT * FROM read_parquet('{event_file}') LIMIT 3").df()
    logger.info(f"clob_events sample row 0:\n{sample_events.iloc[0].to_dict()}")
    
    trade_file = "data/tape/clob_trades/date=2026-11-04/hour=00/part-20261104T005217-20261104T00-00.parquet"
    logger.info(f"Inspecting clob_trades: {trade_file}")
    df_trades = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{trade_file}')").df()
    logger.info(f"clob_trades columns:\n{df_trades[['column_name', 'column_type']]}")
    
    sample_trades = con.execute(f"SELECT * FROM read_parquet('{trade_file}') LIMIT 3").df()
    logger.info(f"clob_trades sample row 0:\n{sample_trades.iloc[0].to_dict()}")

if __name__ == "__main__":
    inspect()
