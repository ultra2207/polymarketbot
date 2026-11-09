"""Detailed inspection of trades around Binance jump events."""
import json
from pathlib import Path
from polymarketbot.logs import get_logger
log = get_logger(__name__)

with open("data/derived/jump_clob_response_summary.json", "r", encoding="utf-8") as f:
    results = json.load(f)

log.info(f"Total events analyzed: {len(results)}")
for r in results:
    ev = r["event"]
    log.info(f"\n=======================================================")
    log.info(f"EVENT: {ev['name']} | UTC: {ev['utc']} | Jump: {ev['dir']} {ev['bps']:.1f} bps | Total CLOB trades: {r['total_clob_trades']}")
    crypto_trades = r["crypto_trades"]
    log.info(f"Crypto trades count: {len(crypto_trades)}")
    total_dollar = sum(t["notional_usd"] for t in crypto_trades)
    log.info(f"Total crypto volume in window: ${total_dollar:.2f}")
    for t in crypto_trades:
        log.info(f"  Delta: {t['delta_venue_ms']:+5d} ms | Side: {t['side']:4s} | Price: {t['price']:.3f} | Size: {t['size']:8.1f} | Notional: ${t['notional_usd']:6.2f} | {t['question']}")
