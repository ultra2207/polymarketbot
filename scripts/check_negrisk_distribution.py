"""Calculate distribution of sum(BestAsk_i) across multi-outcome events."""
import sys
sys.path.insert(0, ".")
import duckdb
import pandas as pd
from scripts.audit_domain3_l2_tape import load_universe_metadata
from collections import defaultdict

c_map, ev_map = load_universe_metadata()
multi_events = {ev: cids for ev, cids in ev_map.items() if len(cids) >= 3}
cid_to_event = {cid: ev for ev, cids in multi_events.items() for cid in cids}

print(f"Total multi-outcome events with >=3 legs: {len(multi_events)}")

con = duckdb.connect()
f = 'data/tape/clob_events/date=2026-11-04/hour=00/*.parquet'

sql = f"""
    SELECT 
        recv_ts_ns,
        lower(condition_id) as condition_id,
        asset_id,
        best_ask
    FROM read_parquet('{f}')
    WHERE event_type = 'best_bid_ask'
      AND condition_id IS NOT NULL
      AND best_ask IS NOT NULL
      AND best_ask > 0
    ORDER BY recv_ts_ns ASC
"""
df = con.execute(sql).df()
print(f"Loaded {len(df)} BBO rows")

event_books = defaultdict(dict)
sums = []

for r in df.itertuples(index=False):
    ev = cid_to_event.get(r.condition_id)
    if ev:
        tokens = c_map.get(r.condition_id, {}).get("clob_tokens") or []
        if tokens and r.asset_id == tokens[0]: # YES token
            event_books[ev][r.condition_id] = r.best_ask
            if len(event_books[ev]) == len(multi_events[ev]):
                s = sum(event_books[ev].values())
                sums.append({"event_id": ev, "legs": len(event_books[ev]), "ask_sum": s})

if sums:
    sdf = pd.DataFrame(sums)
    print("\nMulti-outcome sum(Ask_YES_i) distribution:")
    print(sdf['ask_sum'].describe())
    print("\nPercentiles:")
    print(sdf['ask_sum'].quantile([0.001, 0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.99]))
    print(f"\nNumber of observations < 1.000: {(sdf['ask_sum'] < 1.000).sum()} out of {len(sdf)}")
    print(f"Number of observations < 1.020: {(sdf['ask_sum'] < 1.020).sum()} out of {len(sdf)}")
    print(f"Number of observations < 1.050: {(sdf['ask_sum'] < 1.050).sum()} out of {len(sdf)}")
else:
    print("No complete multi-outcome books observed in this partition.")
