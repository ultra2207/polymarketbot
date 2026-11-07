"""Inspect clock offset bracket and find all active BTC tokens on 2026-11-05."""
import json
from pathlib import Path
import duckdb
from polymarketbot.logs import get_logger
from polymarketbot.netclock import load_samples, offset_bracket, best_sample

log = get_logger("inspect_clock_and_tokens")

def main():
    # 1. Clock offsets
    con = duckdb.connect()
    con.execute("SET threads=2")
    
    clock_files = list(Path("data/tape/clock_offset").glob("date=2026-11-05/*.parquet"))
    log.info(f"Clock offset files for 2026-11-05: {len(clock_files)}")
    if clock_files:
        df_clock = con.execute("""
            SELECT venue, min(rtt_ms) as min_rtt, avg(rtt_ms) as avg_rtt, avg(offset_ms) as avg_offset
            FROM read_parquet('data/tape/clock_offset/date=2026-11-05/*.parquet')
            GROUP BY venue
        """).df()
        log.info("Clock offsets on 2026-11-05:")
        for _, row in df_clock.iterrows():
            log.info(f"  Venue: {row['venue']} | Min RTT: {row['min_rtt']:.1f}ms | Avg RTT: {row['avg_rtt']:.1f}ms | Avg Offset: {row['avg_offset']:.1f}ms")
            
    # 2. Find all BTC and crypto tokens in manifest and universe files
    tokens_map = {}
    
    # Check manifest_current
    with open("data/tape/manifest_current.json", "r", encoding="utf-8") as f:
        mcurr = json.load(f)
    for m in mcurr.get("markets", mcurr):
        q = str(m.get("question", ""))
        cid = str(m.get("condition_id", ""))
        for t in m.get("clob_tokens", []):
            tokens_map[str(t)] = {"question": q, "condition_id": cid}
            
    # Check universe files
    for uf in Path("data/tape/universe").glob("*.json"):
        try:
            with open(uf, "r", encoding="utf-8") as f:
                udata = json.load(f)
            for m in udata.get("markets", udata):
                q = str(m.get("question", ""))
                cid = str(m.get("condition_id", ""))
                for t in m.get("clob_tokens", []):
                    tokens_map[str(t)] = {"question": q, "condition_id": cid}
        except Exception:
            pass
            
    log.info(f"Total known tokens mapped: {len(tokens_map)}")
    
    # Filter to BTC tokens
    btc_tokens = {t: info for t, info in tokens_map.items() if any(k in info["question"].lower() for k in ["btc", "bitcoin"])}
    log.info(f"Total BTC tokens mapped: {len(btc_tokens)}")
    
    # Save btc_tokens to data/derived/btc_tokens_map.json
    out_path = Path("data/derived/btc_tokens_map.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(btc_tokens, f, indent=2)
    log.info(f"Saved {len(btc_tokens)} BTC tokens to {out_path}")
    
    # Sample 5
    for t, info in list(btc_tokens.items())[:5]:
        log.info(f"  Token {t}: {info['question']}")

if __name__ == "__main__":
    main()
