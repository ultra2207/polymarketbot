import duckdb
from polymarketbot.logs import get_logger
log = get_logger(__name__)

con = duckdb.connect()

for path in ['data/derived/uma_events.parquet', 'data/derived/uma/uma_gamma_join.parquet', 'data/derived/uma/uma_raw.parquet']:
    log.info('=== ' + path + ' ===')
    try:
        df = con.execute(f"DESCRIBE SELECT * FROM '{path}'").df()
        log.info(df[['column_name', 'column_type']])
        log.info('{!s} {!s}', 'Row count:', con.execute(f"SELECT COUNT(*) FROM '{path}'").fetchone()[0])
        log.info('Sample:')
        log.info(con.execute(f"SELECT * FROM '{path}' LIMIT 1").df().to_dict(orient='records'))
    except Exception as e:
        log.info('{!s} {!s}', 'Error:', e)
