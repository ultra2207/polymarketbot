"""Inspect the 26 multi-outcome observations with sum < 1.000."""
import sys
sys.path.insert(0, ".")
import duckdb
import pandas as pd
from scripts.audit_domain3_l2_tape import load_universe_metadata
from collections import defaultdict

c_map, ev_map = load_universe_metadata()
multi_events = {ev: cids for ev, cids in ev_map.items() if len(cids) >= 3}
cid_to_event = {cid: ev for ev, cids in multi_events.items() for cid in cids}

con = duckdb.connect()
f = 'data/tape/clob_events/date=2026-11-04/hour=00/*.parquet'

sql = f"""
    SELECT 
        recv_ts_ns,
        venue_ts_ms,
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

event_books = defaultdict(dict)
sub1_events = []

for r in df.itertuples(index=False):
    ev = cid_to_event.get(r.condition_id)
    if ev:
        tokens = c_map.get(r.condition_id, {}).get("clob_tokens") or []
        if tokens and r.asset_id == tokens[0]: # YES token
            event_books[ev][r.condition_id] = (r.best_ask, r.recv_ts_ns, r.venue_ts_ms)
            if len(event_books[ev]) == len(multi_events[ev]):
                s = sum(v[0] for v in event_books[ev].values())
                if s < 1.000:
                    sub1_events.append({
                        "event_id": ev,
                        "legs": len(event_books[ev]),
                        "ask_sum": round(s, 4),
                        "recv_ts_ns": r.recv_ts_ns,
                        "venue_ts_ms": r.venue_ts_ms,
                        "legs_detail": {c: v[0] for c, v in event_books[ev].items()}
                    })

df_sub = pd.DataFrame(sub1_events)
print("Sub-1.00 multi-outcome events found:", len(df_sub))
if len(df_sub) > 0:
    print(df_sub[['event_id', 'legs', 'ask_sum', 'venue_ts_ms']].head(10))
    sample = sub1_events[0]
    ev_info = c_map.get(list(sample["legs_detail"].keys())[0], {})
    print("\nSample Event:", sample["event_id"], "Title:", ev_info.get("event_title"))
    print("Legs:", sample["legs_detail"])
    
    # Calculate duration of these sub-1.00 episodes
    # Find how long the sum stayed < 1.00
    first_ts = df_sub['recv_ts_ns'].min()
    last_ts = df_sub['recv_ts_ns'].max()
    print(f"\nTime span between first and last sub-1.00 observation: {(last_ts - first_ts)/1e6:.2f} ms")
