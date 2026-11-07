import duckdb
from polymarketbot.logs import get_logger

logger = get_logger("inspect_reversal")

con = duckdb.connect()
cid = "0x26d14ca2724455adac1c0ba0374cd7eaee6404855730a49be3fe4e7563e4518c"
df = con.execute("SELECT * FROM 'data/derived/uma/universe_map.parquet' WHERE condition_id = ?", [cid]).df()
print("Universe map row:")
print(df.to_dict('records'))

# Inspect resolution
res_df = con.execute("SELECT * FROM read_parquet(['data/derived/uma/res_nov5.parquet', 'data/derived/uma/res_nov6.parquet']) WHERE topic1 = ?", [cid]).df()
print("Resolution rows:")
print(res_df.to_dict('records'))
