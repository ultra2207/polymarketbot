import duckdb

con = duckdb.connect()

for path in ['data/derived/uma_events.parquet', 'data/derived/uma/uma_gamma_join.parquet', 'data/derived/uma/uma_raw.parquet']:
    print('=== ' + path + ' ===')
    try:
        df = con.execute(f"DESCRIBE SELECT * FROM '{path}'").df()
        print(df[['column_name', 'column_type']])
        print('Row count:', con.execute(f"SELECT COUNT(*) FROM '{path}'").fetchone()[0])
        print('Sample:')
        print(con.execute(f"SELECT * FROM '{path}' LIMIT 1").df().to_dict(orient='records'))
    except Exception as e:
        print('Error:', e)
