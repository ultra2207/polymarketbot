"""Run empirical tape audit across multiple representative hours."""
import sys
sys.path.insert(0, ".")
import json
import time
from pathlib import Path
from dataclasses import asdict
import pandas as pd
from polymarketbot.logs import get_logger
from scripts.audit_domain3_l2_tape import audit_hour_slice, load_universe_metadata

logger = get_logger("run_multi_hour_audit")

HOURS = [
    "data/tape/clob_events/date=2026-11-04/hour=00",
    "data/tape/clob_events/date=2026-11-04/hour=12",
    "data/tape/clob_events/date=2026-11-05/hour=15",
    "data/tape/clob_events/date=2026-11-06/hour=12",
]

def main():
    logger.info("Starting multi-hour empirical L2 tape audit...")
    c_map, ev_map = load_universe_metadata()
    
    all_anomalies = []
    hourly_metrics = []
    
    out_dir = Path("data/derived")
    out_dir.mkdir(parents=True, exist_ok=True)
    
    for h_dir in HOURS:
        if not Path(h_dir).exists():
            logger.warning(f"Path does not exist: {h_dir}")
            continue
            
        logger.info(f"Auditing hour: {h_dir}")
        t0 = time.time()
        anoms, metrics = audit_hour_slice(h_dir, c_map, ev_map)
        elapsed = time.time() - t0
        logger.info(f"Finished {h_dir} in {elapsed:.2f}s: {len(anoms)} anomalies found.")
        
        all_anomalies.extend(anoms)
        hourly_metrics.append(metrics)
        
        # Save partition incremental file
        partition_name = h_dir.replace("/", "_").replace("\\", "_")
        df_part = pd.DataFrame([asdict(a) for a in anoms])
        if not df_part.empty:
            df_part.to_parquet(out_dir / f"anomalies_{partition_name}.parquet", index=False)
    
    logger.info(f"All hours complete. Total anomalies collected: {len(all_anomalies)}")
    df_all = pd.DataFrame([asdict(a) for a in all_anomalies])
    df_all.to_parquet(out_dir / "domain3_tape_audit_anomalies.parquet", index=False)
    
    summary = {
        "hours_audited": len(hourly_metrics),
        "total_anomalies": len(all_anomalies),
        "total_phantom": sum(m["phantom_count"] for m in hourly_metrics),
        "total_genuine": sum(m["genuine_count"] for m in hourly_metrics),
        "gross_spread_mean": float(df_all["gross_spread"].mean()),
        "gross_spread_median": float(df_all["gross_spread"].median()),
        "total_taker_fee_mean": float(df_all["total_taker_fee"].mean()),
        "net_spread_mean": float(df_all["net_spread"].mean()),
        "net_spread_median": float(df_all["net_spread"].median()),
        "profitable_after_fees_count": int((df_all["net_spread"] > 0).sum()),
        "profitable_fraction": float((df_all["net_spread"] > 0).mean()),
        "duration_venue_ms_median": float(df_all["duration_venue_ms"].median()),
        "duration_venue_ms_mean": float(df_all["duration_venue_ms"].mean()),
        "min_depth_shares_median": float(df_all["min_depth_shares"].median()),
        "dollar_depth_median": float(df_all["dollar_depth"].median()),
        "dollar_depth_mean": float(df_all["dollar_depth"].mean()),
        "hourly_breakdown": hourly_metrics,
    }
    
    with open(out_dir / "domain3_tape_audit_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
        
    logger.info("Saved summary to data/derived/domain3_tape_audit_summary.json")
    print(json.dumps(summary, indent=2))

if __name__ == "__main__":
    main()
