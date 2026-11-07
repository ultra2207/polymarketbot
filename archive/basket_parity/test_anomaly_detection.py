"""Test state machine on 1 file for complementary parity anomalies."""
import time
import duckdb
from polymarketbot.logs import get_logger

logger = get_logger("test_anomaly_detection")

def test():
    con = duckdb.connect()
    f = 'data/tape/clob_events/date=2026-11-04/hour=00/part-20261104T005217-20261104T00-00.parquet'
    
    t0 = time.time()
    query = f"""
        SELECT 
            recv_ts_ns,
            venue_ts_ms,
            condition_id,
            asset_id,
            best_bid,
            best_ask
        FROM read_parquet('{f}')
        WHERE event_type = 'best_bid_ask'
          AND condition_id IS NOT NULL
          AND asset_id IS NOT NULL
        ORDER BY recv_ts_ns ASC
    """
    df = con.execute(query).df()
    t_read = time.time() - t0
    logger.info(f"Loaded {len(df)} rows in {t_read:.3f}s")
    
    # State tracking: condition_id -> {asset_id: (best_bid, best_ask)}
    # Active anomalies: condition_id -> {start_recv_ns, start_venue_ms, ask1, ask2, ...}
    t0 = time.time()
    books = {}
    active_anomalies = {}
    completed_anomalies = []
    
    for row in df.itertuples(index=False):
        cid = row.condition_id
        aid = row.asset_id
        ask = row.best_ask
        bid = row.best_bid
        recv_ns = row.recv_ts_ns
        venue_ms = row.venue_ts_ms
        
        cbook = books.setdefault(cid, {})
        cbook[aid] = (bid, ask)
        
        # Check parity if exactly 2 assets are known
        if len(cbook) == 2:
            aids = list(cbook.keys())
            ask0 = cbook[aids[0]][1]
            ask1 = cbook[aids[1]][1]
            
            if ask0 is not None and ask1 is not None and ask0 > 0 and ask1 > 0:
                ask_sum = ask0 + ask1
                if ask_sum < 0.9999: # Parity anomaly
                    if cid not in active_anomalies:
                        active_anomalies[cid] = {
                            "condition_id": cid,
                            "start_recv_ns": recv_ns,
                            "start_venue_ms": venue_ms,
                            "ask0": ask0,
                            "ask1": ask1,
                            "ask_sum": ask_sum,
                            "spread": 1.0 - ask_sum
                        }
                else:
                    if cid in active_anomalies:
                        anom = active_anomalies.pop(cid)
                        anom["end_recv_ns"] = recv_ns
                        anom["end_venue_ms"] = venue_ms
                        anom["duration_recv_ms"] = (recv_ns - anom["start_recv_ns"]) / 1e6
                        anom["duration_venue_ms"] = (venue_ms - anom["start_venue_ms"]) if (venue_ms and anom["start_venue_ms"]) else None
                        completed_anomalies.append(anom)
    
    t_proc = time.time() - t0
    logger.info(f"Processed in {t_proc:.3f}s. Found {len(completed_anomalies)} completed anomalies, {len(active_anomalies)} still active.")
    if completed_anomalies:
        import pandas as pd
        adf = pd.DataFrame(completed_anomalies)
        logger.info(f"Sample anomalies:\n{adf[['ask_sum', 'spread', 'duration_recv_ms', 'duration_venue_ms']].head(10)}")
        logger.info(f"Spread stats:\n{adf['spread'].describe()}")
        logger.info(f"Duration recv ms stats:\n{adf['duration_recv_ms'].describe()}")

if __name__ == "__main__":
    test()
