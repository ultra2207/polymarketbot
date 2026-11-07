"""Deep dive into trade matches for anomalies: check single-leg vs two-leg fills."""
import sys
sys.path.insert(0, ".")
import duckdb
import pandas as pd
from scripts.audit_domain3_l2_tape import audit_hour_slice, load_universe_metadata
from dataclasses import asdict

c_map, ev_map = load_universe_metadata()
anoms, _ = audit_hour_slice('data/tape/clob_events/date=2026-11-04/hour=00', c_map, ev_map)
df = pd.DataFrame([asdict(a) for a in anoms])

con = duckdb.connect()
tr_path = 'data/tape/clob_trades/date=2026-11-04/hour=00/*.parquet'
df_tr = con.execute(f"SELECT venue_ts_ms, recv_ts_ns, lower(condition_id) as condition_id, asset_id, side, price, size, transaction_hash FROM read_parquet('{tr_path}')").df()

both_legs_traded = 0
one_leg_traded = 0
trade_details = []

for r in df.itertuples():
    if r.start_venue_ms and r.end_venue_ms:
        sub = df_tr[(df_tr['condition_id'] == r.condition_id) & (df_tr['venue_ts_ms'] >= r.start_venue_ms - 100) & (df_tr['venue_ts_ms'] <= r.end_venue_ms + 100)]
        if len(sub) > 0:
            traded_assets = set(sub['asset_id'])
            if len(traded_assets) >= 2:
                both_legs_traded += 1
                trade_details.append({"type": "both_legs", "cid": r.condition_id, "n_trades": len(sub), "spread": r.gross_spread, "dur_ms": r.duration_venue_ms, "assets": list(traded_assets)})
            else:
                one_leg_traded += 1
                trade_details.append({"type": "one_leg", "cid": r.condition_id, "n_trades": len(sub), "spread": r.gross_spread, "dur_ms": r.duration_venue_ms, "assets": list(traded_assets)})

print(f"Total anomalies matched with trades: {len(trade_details)}")
print(f"Both legs traded (matched complete pair fill): {both_legs_traded}")
print(f"ONLY ONE leg traded (naked single-leg fill / snipe!): {one_leg_traded}")

if trade_details:
    tdf = pd.DataFrame(trade_details)
    print("\nSummary by trade type:")
    print(tdf.groupby("type")[["spread", "dur_ms", "n_trades"]].describe())
