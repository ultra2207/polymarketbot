"""Measure inter-fill latency between Leg 1 and Leg 2 in dual-leg fills."""
import sys
sys.path.insert(0, ".")
import duckdb
import pandas as pd
from scripts.audit_domain3_l2_tape import audit_hour_slice, load_universe_metadata
from dataclasses import asdict
from polymarketbot.logs import get_logger
log = get_logger(__name__)

c_map, ev_map = load_universe_metadata()
anoms, _ = audit_hour_slice('data/tape/clob_events/date=2026-11-04/hour=00', c_map, ev_map)
df = pd.DataFrame([asdict(a) for a in anoms])

con = duckdb.connect()
tr_path = 'data/tape/clob_trades/date=2026-11-04/hour=00/*.parquet'
df_tr = con.execute(f"SELECT venue_ts_ms, recv_ts_ns, lower(condition_id) as condition_id, asset_id, side, price, size, transaction_hash FROM read_parquet('{tr_path}')").df()

dual_latencies = []

for r in df.itertuples():
    if r.start_venue_ms and r.end_venue_ms:
        sub = df_tr[(df_tr['condition_id'] == r.condition_id) & (df_tr['venue_ts_ms'] >= r.start_venue_ms - 100) & (df_tr['venue_ts_ms'] <= r.end_venue_ms + 100)]
        traded_assets = set(sub['asset_id'])
        if len(traded_assets) >= 2:
            # Group by asset
            a0, a1 = list(traded_assets)[:2]
            t0_venue = sub[sub['asset_id'] == a0]['venue_ts_ms'].min()
            t1_venue = sub[sub['asset_id'] == a1]['venue_ts_ms'].min()
            t0_recv = sub[sub['asset_id'] == a0]['recv_ts_ns'].min()
            t1_recv = sub[sub['asset_id'] == a1]['recv_ts_ns'].min()
            
            dual_latencies.append({
                "cid": r.condition_id,
                "delta_venue_ms": abs(t0_venue - t1_venue),
                "delta_recv_ms": abs(t0_recv - t1_recv) / 1e6,
                "spread": r.gross_spread,
                "duration_ms": r.duration_venue_ms
            })

ldf = pd.DataFrame(dual_latencies)
log.info(f"Dual fill count: {len(ldf)}")
log.info("Inter-fill latency stats (delta_venue_ms):")
log.info(ldf['delta_venue_ms'].describe())
log.info("\nInter-fill latency stats (delta_recv_ms):")
log.info(ldf['delta_recv_ms'].describe())
log.info("\nSample records:")
log.info(ldf.head(10))
