"""Inspect crypto markets present in the tape and manifest."""
import json
from pathlib import Path
import duckdb
from polymarketbot.logs import get_logger

log = get_logger("inspect_crypto_tape")

def main():
    log.info("Starting inspect_crypto_tape")
    
    # 1. Load manifest markets
    manifest_path = Path("data/tape/manifest_current.json")
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest_data = json.load(f)
    markets = manifest_data.get("markets", manifest_data)
    
    token_to_market = {}
    crypto_tokens = {}
    for m in markets:
        q = str(m.get("question", ""))
        cid = str(m.get("condition_id", ""))
        tokens = m.get("clob_tokens", [])
        is_crypto = any(k in q.lower() for k in ["bitcoin", "btc", "eth", "ethereum", "solana", "sol", "crypto", "up or down"])
        for t in tokens:
            token_to_market[str(t)] = (q, cid)
            if is_crypto:
                crypto_tokens[str(t)] = (q, cid)
                
    log.info(f"Loaded {len(token_to_market)} tokens from manifest, {len(crypto_tokens)} are crypto-related")
    
    # Check universe folder for historical manifests
    univ_dir = Path("data/tape/universe")
    if univ_dir.exists():
        univ_files = list(univ_dir.glob("*.json"))
        log.info(f"Found {len(univ_files)} historical universe manifests")
        for uf in univ_files[:5]:
            try:
                with open(uf, "r", encoding="utf-8") as f:
                    udata = json.load(f)
                umarkets = udata.get("markets", udata)
                log.info(f"Universe file {uf.name} has {len(umarkets)} markets")
                for m in umarkets:
                    q = str(m.get("question", ""))
                    if any(k in q.lower() for k in ["btc", "5-minute", "15-minute", "up or down", "bitcoin"]):
                        for t in m.get("clob_tokens", []):
                            token_to_market[str(t)] = (q, str(m.get("condition_id", "")))
                            crypto_tokens[str(t)] = (q, str(m.get("condition_id", "")))
            except Exception as e:
                log.warning(f"Error reading {uf}: {e}")

    # 2. Connect to DuckDB and check clob_trades
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET temp_directory='.tmp/duckdb_spill'")
    
    # Check trades on 2026-11-05 and 2026-11-06
    trade_summary = con.execute("""
        SELECT asset_id, count(*) as trade_count, sum(size) as total_size, min(price) as min_p, max(price) as max_p
        FROM read_parquet('data/tape/clob_trades/date=2026-11-06/*/*.parquet')
        GROUP BY asset_id
    """).fetchall()
    
    log.info(f"Total distinct tokens traded on 2026-11-06: {len(trade_summary)}")
    crypto_trades_found = 0
    for asset_id, count, total_size, min_p, max_p in trade_summary:
        if asset_id in crypto_tokens:
            crypto_trades_found += 1
            q, cid = crypto_tokens[asset_id]
            log.info(f"CRYPTO TRADE on 2026-11-06: Token {asset_id} ({q}) -> {count} trades, size: {total_size:.1f}, price: [{min_p:.3f}, {max_p:.3f}]")
            
    log.info(f"Found {crypto_trades_found} crypto tokens with trades on 2026-11-06")

    # Also check 2026-11-05
    trade_summary_05 = con.execute("""
        SELECT asset_id, count(*) as trade_count, sum(size) as total_size
        FROM read_parquet('data/tape/clob_trades/date=2026-11-05/*/*.parquet')
        GROUP BY asset_id
    """).fetchall()
    log.info(f"Total distinct tokens traded on 2026-11-05: {len(trade_summary_05)}")
    crypto_trades_found_05 = 0
    for asset_id, count, total_size in trade_summary_05:
        if asset_id in crypto_tokens:
            crypto_trades_found_05 += 1
            q, cid = crypto_tokens[asset_id]
            log.info(f"CRYPTO TRADE on 2026-11-05: Token {asset_id} ({q}) -> {count} trades, size: {total_size:.1f}")
            
    log.info(f"Found {crypto_trades_found_05} crypto tokens with trades on 2026-11-05")

if __name__ == "__main__":
    main()
