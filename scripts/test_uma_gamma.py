import duckdb

con = duckdb.connect()
df = con.execute("DESCRIBE SELECT * FROM 'data/derived/uma/uma_gamma_join.parquet'").df()
print(df[['column_name', 'column_type']])
sample = con.execute("SELECT conditionId, clobTokenIds, question, ts, expiry, (expiry - ts/1000)/3600.0 as lockup_hours FROM 'data/derived/uma/uma_gamma_join.parquet' LIMIT 5").df()
print(sample)
