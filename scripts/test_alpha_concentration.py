"""Analyze full mapping across all 70 conditions."""
import duckdb

con = duckdb.connect()

for f in ['kalshi_mapping.parquet', 'kalshi_mapping_v2.parquet', 'kalshi_mapping_active.parquet', 'kalshi_mapping_active_v2.parquet']:
    res = con.execute(f"SELECT count(*), count(distinct polymarket_condition_id) FROM read_parquet('data/derived/{f}')").fetchone()
    print(f"{f:30s} -> rows: {res[0]:4d}, distinct conditions: {res[1]:4d}")

# Join kalshi_trade_lead with kalshi_mapping_v2
query = """
    SELECT 
        m.polymarket_condition_id,
        m.kalshi_ticker,
        m.polymarket_question,
        count(*) as events
    FROM read_parquet('data/derived/kalshi_trade_lead.parquet') t
    JOIN read_parquet('data/derived/kalshi_mapping_v2.parquet') m
      ON t.condition_id = m.polymarket_condition_id
    WHERE t."null" = false
    GROUP BY m.polymarket_condition_id, m.kalshi_ticker, m.polymarket_question
    ORDER BY events DESC
"""
df = con.execute(query).df()
print(f"\nJoined with kalshi_mapping_v2: {len(df)} markets")
for idx, r in df.head(15).iterrows():
    print(f"  #{idx+1:2d} | {str(r['kalshi_ticker']):25s} | Events: {r['events']:5d} | {str(r['polymarket_question'])[:50]}")

total = df['events'].sum()
df['cum_events'] = df['events'].cumsum()
df['cum_pct'] = df['cum_events'] / total * 100

print("\n--- CUMULATIVE CONCENTRATION ---")
for p in [50, 70, 80, 85, 90, 95]:
    n = (df['cum_pct'] <= p).sum() + 1
    actual = df.iloc[n-1]['cum_pct']
    print(f"  Top {n:2d} streams capture {actual:5.1f}% of total cross-venue trade alpha")
